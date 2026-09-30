import json
import os
import stat

import pytest
from test_model_configuration import write_config

from agent_core.model_selection.cache import CatalogCache, CatalogUnavailable
from agent_core.model_selection.configuration import load_selection_config


def cache(tmp_path):
    config = load_selection_config(write_config(tmp_path), environ={})
    return CatalogCache(config)


def test_publish_load_and_staleness(tmp_path):
    c = cache(tmp_path)
    with pytest.raises(CatalogUnavailable, match="missing"):
        c.load(now=100)
    c.publish(inventory={"pool": ["model"]}, benchmarks={"data": []}, now=100)
    assert c.load(now=101)["inventory"] == {"pool": ["model"]}
    with pytest.raises(CatalogUnavailable, match="stale"):
        c.load(now=100 + 8 * 86400)
    with pytest.raises(CatalogUnavailable, match="clock"):
        c.load(now=99)


def test_invalid_or_different_scope_cannot_load(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={"data": []}, now=100)
    payload = json.loads(c.path.read_text())
    payload["scope_digest"] = "another-scope"
    c.path.write_text(json.dumps(payload))
    with pytest.raises(CatalogUnavailable, match="scope"):
        c.load(now=101)


def test_failed_publication_preserves_old_cache(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={"pool": ["a"]}, benchmarks={"data": []}, now=100)
    with pytest.raises(ValueError):
        c.publish(inventory={}, benchmarks={"invalid": float("nan")}, now=101)
    assert c.load(now=102)["inventory"] == {"pool": ["a"]}


def test_cache_permissions_and_digest_validation(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={"data": []}, now=100)
    assert c.path.stat().st_mode & 0o777 == 0o660
    assert c.path.parent.stat().st_mode & 0o7777 == 0o2770
    value = json.loads(c.path.read_text())
    value["inventory"] = {"pool": ["injected"]}
    c.path.write_text(json.dumps(value))
    with pytest.raises(CatalogUnavailable, match="digest"):
        c.load(now=101)


def test_compliant_shared_modes_need_no_owner_chmod(tmp_path, monkeypatch):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={"data": []}, now=100)
    with c.refresh_lock():
        pass
    lock_inode = (c.path.parent / "refresh.lock").stat().st_ino
    fchmod = os.fchmod

    def forbid_directory_chmod(*args):
        raise PermissionError("shared directory belongs to another service identity")

    def chmod_only_new_temporary_file(fd, mode):
        current = os.fstat(fd)
        if current.st_ino == lock_inode or stat.S_IMODE(current.st_mode) == mode:
            raise PermissionError("compliant shared file belongs to another service identity")
        fchmod(fd, mode)

    monkeypatch.setattr(os, "chmod", forbid_directory_chmod)
    monkeypatch.setattr(os, "fchmod", chmod_only_new_temporary_file)
    with c.refresh_lock():
        c.publish(inventory={"pool": ["updated"]}, benchmarks={"data": []}, now=101)
    assert c.load(now=102)["inventory"] == {"pool": ["updated"]}
    assert stat.S_IMODE(c.path.stat().st_mode) == 0o660
    assert stat.S_IMODE(c.path.parent.stat().st_mode) == 0o2770


@pytest.mark.parametrize("operation", ["publish", "lock"])
def test_incompatible_nonowner_directory_mode_fails_clearly(tmp_path, monkeypatch, operation):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={"data": []}, now=100)
    original = c.path.read_bytes()
    c.path.parent.chmod(0o770)

    def forbid_chmod(*args):
        raise PermissionError("not owner")

    monkeypatch.setattr(os, "chmod", forbid_chmod)
    with pytest.raises(CatalogUnavailable, match="permissions.*owner.*2770"):
        if operation == "publish":
            c.publish(inventory={}, benchmarks={"data": []}, now=101)
        else:
            with c.refresh_lock():
                pytest.fail("incompatible permissions must not acquire the lock")
    assert c.path.read_bytes() == original


def test_incompatible_nonowner_lock_mode_fails_clearly(tmp_path, monkeypatch):
    c = cache(tmp_path)
    with c.refresh_lock():
        pass
    lock = c.path.parent / "refresh.lock"
    lock.chmod(0o640)

    def forbid_fchmod(*args):
        raise PermissionError("not owner")

    monkeypatch.setattr(os, "fchmod", forbid_fchmod)
    with pytest.raises(CatalogUnavailable, match="permissions.*owner.*0660"), c.refresh_lock():
        pytest.fail("incompatible permissions must not acquire the lock")
    assert stat.S_IMODE(lock.stat().st_mode) == 0o640


def test_owner_can_repair_incompatible_directory_and_lock_modes(tmp_path):
    c = cache(tmp_path)
    with c.refresh_lock():
        pass
    lock = c.path.parent / "refresh.lock"
    c.path.parent.chmod(0o700)
    lock.chmod(0o600)
    with c.refresh_lock():
        assert stat.S_IMODE(c.path.parent.stat().st_mode) == 0o2770
        assert stat.S_IMODE(lock.stat().st_mode) == 0o660


@pytest.mark.parametrize("value", [[], {"schema_version": 2}])
def test_invalid_cache_schema(tmp_path, value):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    c.path.write_text(json.dumps(value))
    with pytest.raises(CatalogUnavailable, match="^catalog schema invalid$"):
        c.load(now=100)


def test_corrupt_and_nonfinite_cache_are_rejected(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    payload = json.loads(c.path.read_text())
    c.path.write_text("not json")
    with pytest.raises(CatalogUnavailable, match="^catalog unreadable; run model selection refresh$"):
        c.load(now=100)
    payload["fetched_at"] = float("nan")
    c.path.write_text(json.dumps(payload))
    with pytest.raises(CatalogUnavailable, match="^catalog digest invalid$"):
        c.load(now=100)


@pytest.mark.parametrize("timestamp", [True, "100", None])
def test_signed_but_invalid_timestamp(tmp_path, timestamp):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    from agent_core.model_selection.cache import _digest
    payload = json.loads(c.path.read_text())
    payload.pop("content_digest")
    payload["fetched_at"] = timestamp
    payload["content_digest"] = _digest(payload)
    c.path.write_text(json.dumps(payload))
    with pytest.raises(CatalogUnavailable, match="^catalog timestamp invalid$"):
        c.load(now=100)


def test_refresh_lock_contention_and_release(tmp_path):
    c = cache(tmp_path)
    with c.refresh_lock():
        with pytest.raises(CatalogUnavailable, match="^catalog refresh already running$"), c.refresh_lock():
            pytest.fail("second refresh acquired the lock")
    with c.refresh_lock():
        pass


def test_failed_atomic_replace_removes_temporary_file(tmp_path, monkeypatch):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    original = c.path.read_bytes()
    def fail_replace(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk failure"):
        c.publish(inventory={"pool": ["a"]}, benchmarks={}, now=101)
    assert c.path.read_bytes() == original
    assert not list(c.path.parent.glob(".catalog-*"))


def test_exact_cache_age_is_valid(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    assert c.load(now=100 + c.config.max_age_seconds)["fetched_at"] == 100


def test_cache_roundtrip_at_publication_time_keeps_digest(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    original = json.loads(c.path.read_text())
    assert c.load(now=100) == original
    assert isinstance(c.scope_digest, str) and len(c.scope_digest) == 64
    assert original["scope_digest"] == c.scope_digest


def test_rotated_credential_generation_invalidates_cache(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    provider = c.config.providers[0].model_copy(update={"credential_generation": "2"})
    rotated = CatalogCache(c.config.model_copy(update={"providers": (provider,)}))
    with pytest.raises(CatalogUnavailable, match="catalog scope changed"):
        rotated.load(now=101)


def test_cache_size_limit(tmp_path):
    c = cache(tmp_path)
    c.publish(inventory={}, benchmarks={}, now=100)
    original = c.path.read_text()
    limit = 21 * 1024 * 1024
    c.path.write_text(original + " " * (limit - len(original)))
    assert c.load(now=100)["fetched_at"] == 100
    with c.path.open("a") as stream:
        stream.write(" ")
    with pytest.raises(CatalogUnavailable, match="catalog unreadable"):
        c.load(now=100)


def test_pricing_discount_change_keeps_a_valid_catalog(tmp_path):
    """A discount is applied after loading, so it must not strand workers."""
    config = load_selection_config(write_config(tmp_path), environ={})
    CatalogCache(config).publish(inventory={"pool": ["a"]}, benchmarks={"records": []})
    raw = json.loads((tmp_path / "selection.json").read_text())
    raw["providers"][0]["pricing_discount"] = 0
    (tmp_path / "selection.json").write_text(json.dumps(raw))
    discounted = load_selection_config(tmp_path / "selection.json", environ={})
    assert CatalogCache(discounted).load()["inventory"] == {"pool": ["a"]}
    raw["providers"][0]["credential_generation"] = "2"
    (tmp_path / "selection.json").write_text(json.dumps(raw))
    rotated = load_selection_config(tmp_path / "selection.json", environ={})
    with pytest.raises(CatalogUnavailable, match="scope changed"):
        CatalogCache(rotated).load()
