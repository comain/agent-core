"""Atomic, permission-restricted operational catalog; never a per-task snapshot."""

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .configuration import SelectionConfig


class CatalogUnavailable(ValueError):
    """No trustworthy current catalog; refresh before opening an agent call."""


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _ensure_mode(path: Path | int, mode: int) -> None:
    current = os.fstat(path) if isinstance(path, int) else path.stat()
    if stat.S_IMODE(current.st_mode) == mode:
        return
    try:
        if isinstance(path, int):
            os.fchmod(path, mode)
        else:
            os.chmod(path, mode)
    except PermissionError:
        raise CatalogUnavailable(
            f"catalog permissions require owner repair (expected mode {mode:04o})"
        ) from None


def atomic_json(path: Path, value: object) -> None:
    """Publish fully serialized data atomically with shared-group-only access."""
    encoded = _encode(value)
    path.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
    _ensure_mode(path.parent, 0o2770)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".catalog-")
    try:
        with os.fdopen(fd, "wb") as stream:
            _ensure_mode(stream.fileno(), 0o660)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CatalogCache:
    def __init__(self, config: SelectionConfig):
        self.config = config
        self.path = config.cache_root / "catalog.json"
        # The digest binds a catalog to the scope it was fetched under: endpoints
        # and credential generations. Pricing discounts are a ranking knob applied
        # after loading, so changing one must not invalidate a good catalog.
        self.scope_digest = _digest([p.model_dump(mode="json", exclude={"pricing_discount"})
                                     for p in config.providers])

    @contextmanager
    def refresh_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o2770, parents=True, exist_ok=True)
        _ensure_mode(self.path.parent, 0o2770)
        with (self.path.parent / "refresh.lock").open("a") as stream:
            _ensure_mode(stream.fileno(), 0o660)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CatalogUnavailable("catalog refresh already running") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def publish(self, *, inventory: dict, benchmarks: dict, pricing: dict | None = None,
                now: float | None = None) -> None:
        payload = {"schema_version": 1, "scope_digest": self.scope_digest,
                   "fetched_at": time.time() if now is None else now,
                   "inventory": inventory, "benchmarks": benchmarks,
                   "pricing": pricing if pricing is not None else {"records": []}}
        payload["content_digest"] = _digest(payload)
        atomic_json(self.path, payload)

    def load(self, *, now: float | None = None) -> dict:
        try:
            if self.path.stat().st_size > 21 * 1024 * 1024:
                raise CatalogUnavailable("catalog exceeds size limit")
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            raise CatalogUnavailable("catalog missing; run model selection refresh") from None
        except (OSError, ValueError):
            raise CatalogUnavailable("catalog unreadable; run model selection refresh") from None
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise CatalogUnavailable("catalog schema invalid")
        if data.get("scope_digest") != self.scope_digest:
            raise CatalogUnavailable("catalog scope changed; refresh required")
        checksum = data.pop("content_digest", None)
        try:
            valid = checksum == _digest(data)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise CatalogUnavailable("catalog digest invalid")
        fetched_at = data.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            raise CatalogUnavailable("catalog timestamp invalid")
        age = (time.time() if now is None else now) - fetched_at
        if age < 0:
            raise CatalogUnavailable("catalog clock moved backwards")
        if age > self.config.max_age_seconds:
            raise CatalogUnavailable("catalog stale; refresh required")
        data["content_digest"] = checksum
        return data
