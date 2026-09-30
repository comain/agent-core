"""Persistent discovery availability, independent of the manual global tracker."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agent_core.model_selection.availability import AvailabilityStore, CredentialScope


@pytest.fixture
def scope():
    return CredentialScope("pool", "https://pool.example/v1", "worker", "1")


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def store(tmp_path, clock):
    return AvailabilityStore(tmp_path / "health" / "availability.sqlite", clock=lambda: clock[0])


def test_scope_is_frozen_and_normalizes_endpoint(scope):
    assert replace(scope, normalized_endpoint="HTTPS://POOL.EXAMPLE:443/v1/") == scope
    with pytest.raises(FrozenInstanceError):
        scope.provider_id = "other"


@pytest.mark.parametrize("field", ["provider_id", "credential_scope_id", "credential_generation"])
@pytest.mark.parametrize("value", ["", " ", None])
def test_scope_rejects_empty_identity(scope, field, value):
    with pytest.raises(ValueError, match="^scope identifiers must be nonempty strings$"):
        replace(scope, **{field: value})


def test_ipv6_and_nondefault_port_are_retained(scope):
    assert replace(scope, normalized_endpoint="http://[::1]:8080/v1/").normalized_endpoint == "http://[::1]:8080/v1"


@pytest.mark.parametrize("retry", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_retry_is_rejected(store, scope, retry):
    with pytest.raises(ValueError, match="^retry_after_seconds must be finite$"):
        store.record_failure(scope, "a", "timeout", retry_after_seconds=retry)
    assert store.status(scope, "a") is None


@pytest.mark.parametrize("model", ["", " ", None])
def test_invalid_model_cannot_access_health(store, scope, model):
    with pytest.raises(ValueError, match="^model must be a nonempty provider-local ID$"):
        store.status(scope, model)


@pytest.mark.parametrize("endpoint", ["", "relative/path", "https://user:secret@pool.example", "https://pool.example?key=secret", "https://pool.example/#secret"])
def test_scope_rejects_secret_bearing_or_invalid_endpoints(scope, endpoint):
    with pytest.raises(ValueError):
        replace(scope, normalized_endpoint=endpoint)


@pytest.mark.parametrize("reason,retry,seconds,stored_reason", [
    ("rate_limit", None, 60, "rate_limit"),
    ("timeout", None, 300, "timeout"),
    ("Timeout", None, 300, "Timeout"),
    ("no_output", None, 600, "no_output"),
    ("model_unavailable", None, 900, "model_unavailable"),
    ("other_existing_reason", None, 900, "other_existing_reason"),
    ("", None, 60, "model_unavailable"),
    ("timeout", 12, 12, "timeout"),
    ("rate_limit", 0, 60, "rate_limit"),
    ("rate_limit", -1, 60, "rate_limit"),
])
def test_legacy_cooldowns_expire_by_identity(store, scope, clock, reason, retry, seconds, stored_reason):
    assert store.status(scope, "model-a") is None
    store.record_failure(scope, "model-a", reason, retry)
    assert store.status(scope, "model-b") is None
    assert store.status(scope, "model-a") == {
        "model": "model-a", "reason": stored_reason, "unhealthy_until": 1000 + seconds,
    }
    clock[0] += seconds
    assert store.status(scope, "model-a") is None


@pytest.mark.parametrize("field,value", [
    ("provider_id", "other"), ("normalized_endpoint", "https://other.example/v1"),
    ("credential_scope_id", "another-worker"), ("credential_generation", "2"),
])
def test_scope_isolation_and_rotation(store, scope, field, value):
    store.record_failure(scope, "model-a", "provider_auth_failed")
    other = replace(scope, **{field: value})
    assert store.status(other, "model-a") is None
    store.clear_auth_quarantine(other)
    assert store.status(scope, "new-model")["reason"] == "provider_auth_failed"


def test_restart_and_scope_auth_recovery_preserve_transient_state(store, scope, clock):
    store.record_failure(scope, "model-a", "rate_limit")
    store.record_failure(scope, "model-b", "provider_auth_failed", 1)
    restarted = AvailabilityStore(store.path, clock=lambda: clock[0])
    clock[0] += 2
    restarted.record_success(scope, "model-b")
    assert restarted.status(scope, "new-model") == {
        "model": "new-model", "reason": "provider_auth_failed", "unhealthy_until": None,
    }
    restarted.clear_auth_quarantine(scope)
    assert restarted.status(scope, "new-model") is None
    assert restarted.status(scope, "model-a")["reason"] == "rate_limit"
    restarted.record_success(scope, "model-a")
    assert restarted.status(scope, "model-a") is None


def test_success_clears_only_its_model(store, scope):
    for model in ("model-a", "model-b"):
        store.record_failure(scope, model, "timeout", observed_at=900)
    store.record_success(scope, "model-a", observed_at=901)
    assert store.status(scope, "model-a") is None
    assert store.status(scope, "model-b") is not None


@pytest.mark.parametrize("auth", [False, True])
def test_conditional_updates_keep_newest_observation_including_recovery(store, scope, auth):
    reason = "provider_auth_failed" if auth else "timeout"
    def recover(at):
        if auth:
            store.clear_auth_quarantine(scope, observed_at=at)
        else:
            store.record_success(scope, "model-a", observed_at=at)
    store.record_failure(scope, "model-a", reason, observed_at=950)
    recover(940)
    assert store.status(scope, "model-a") is not None
    store.record_failure(scope, "model-a", reason, observed_at=930)
    recover(951)
    assert store.status(scope, "model-a") is None
    store.record_failure(scope, "model-a", reason, observed_at=949)
    assert store.status(scope, "model-a") is None
    store.record_failure(scope, "model-a", reason, observed_at=952)
    recover(952)  # Equal timestamps cannot prove that recovery is newer.
    assert store.status(scope, "model-a") is not None


def test_expiry_read_does_not_remove_newer_success_watermark(store, scope, clock):
    store.record_failure(scope, "model-a", "rate_limit", observed_at=900)
    assert store.status(scope, "model-a") is None
    store.record_success(scope, "model-a", observed_at=990)
    store.record_failure(scope, "model-a", "timeout", observed_at=980)
    assert store.status(scope, "model-a") is None


def test_concurrent_instances_keep_scoped_updates_and_newest_failure(tmp_path, scope):
    path = tmp_path / "shared" / "availability.sqlite"
    def write(i):
        instance = AvailabilityStore(path, clock=lambda: 1000)
        instance.record_failure(scope, "shared-model", "timeout", observed_at=900 + i)
        instance.record_failure(scope, f"model-{i}", "rate_limit", observed_at=990)
        instance.record_success(scope, "shared-model", observed_at=899)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(24)))
    instance = AvailabilityStore(path, clock=lambda: 1000)
    assert instance.status(scope, "shared-model")["unhealthy_until"] == 1223
    assert all(instance.status(scope, f"model-{i}") for i in range(24))


def test_database_and_live_sidecars_are_group_writable(store, scope):
    assert store.path.parent.stat().st_mode & 0o7777 == 0o2770
    with sqlite3.connect(store.path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM availability").fetchall()
        store.record_failure(scope, "model-a", "rate_limit")
        files = [store.path, Path(str(store.path) + "-wal"), Path(str(store.path) + "-shm")]
        assert all(path.exists() for path in files)
        assert all(path.stat().st_mode & 0o777 == 0o660 for path in files)


def test_sidecar_permission_repair_covers_all_sqlite_file_types(store):
    paths = [Path(str(store.path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    for path in paths:
        path.touch()
        path.chmod(0o600)
    store._sidecar_permissions()
    assert [path.stat().st_mode & 0o777 for path in paths] == [0o660] * 3


def test_shared_group_reader_does_not_chmod_correctly_owned_files(store, scope, monkeypatch):
    def forbidden(*args, **kwargs):
        raise PermissionError("only the owner can chmod")
    with sqlite3.connect(store.path) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM availability").fetchall()
        monkeypatch.setattr(os, "fchmod", forbidden)
        monkeypatch.setattr(Path, "chmod", forbidden)
        worker = AvailabilityStore(store.path, clock=lambda: 1000)
        worker.record_failure(scope, "model-a", "rate_limit")
        assert worker.status(scope, "model-a") is not None


def test_wal_initialization_retries_sqlite_busy(tmp_path, monkeypatch):
    connect = sqlite3.connect
    attempts = []
    class BusyOnce(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL":
                attempts.append(sql)
                if len(attempts) == 1:
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return super().execute(sql, *args)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=BusyOnce))
    AvailabilityStore(tmp_path / "availability.sqlite")
    assert len(attempts) == 2


def test_another_process_observes_and_updates_persisted_health(store, scope):
    store.record_failure(scope, "model-a", "rate_limit", observed_at=990)
    code = (
        "from agent_core.model_selection.availability import AvailabilityStore, CredentialScope; "
        "import sys; store = AvailabilityStore(sys.argv[1], clock=lambda: 1000); "
        "scope = CredentialScope('pool', 'https://pool.example/v1', 'worker', '1'); "
        "assert store.status(scope, 'model-a')['unhealthy_until'] == 1050; "
        "store.record_failure(scope, 'model-b', 'provider_auth_failed')"
    )
    result = subprocess.run([sys.executable, "-c", code, str(store.path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert store.status(scope, "new-model")["reason"] == "provider_auth_failed"


@pytest.mark.parametrize("at", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_timestamps_are_rejected(store, scope, at):
    with pytest.raises(ValueError):
        store.record_failure(scope, "model-a", "timeout", observed_at=at)
    with pytest.raises(ValueError):
        store.record_success(scope, "model-a", observed_at=at)
    with pytest.raises(ValueError):
        store.clear_auth_quarantine(scope, observed_at=at)


def test_unknown_schema_is_rejected(tmp_path):
    path = tmp_path / "unknown.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="schema"):
        AvailabilityStore(path)


def test_import_does_not_load_global_harness_settings_or_touch_disk(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import agent_core.model_selection.availability; "
         "assert 'agent_core.harness.tiered_router' not in sys.modules; "
         "assert 'agent_core.config' not in sys.modules"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
