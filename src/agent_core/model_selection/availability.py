"""Scoped operational health; importing this module performs no configuration IO."""

import math
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Match ModelHealthTracker without importing its process-global settings/tracker.
_DEFAULT_COOLDOWN_SECONDS = 60
_MODEL_UNAVAILABLE_COOLDOWN_SECONDS = 15 * 60
_TIMEOUT_COOLDOWN_SECONDS = 5 * 60
_NO_OUTPUT_COOLDOWN_SECONDS = 10 * 60
_AUTH_REASON = "provider_auth_failed"


@dataclass(frozen=True)
class CredentialScope:
    """Nonsecret provider identity; increment generation on credential rotation."""

    provider_id: str
    normalized_endpoint: str
    credential_scope_id: str
    credential_generation: str

    def __post_init__(self) -> None:
        for value in (self.provider_id, self.credential_scope_id, self.credential_generation):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("scope identifiers must be nonempty strings")
        try:
            endpoint = urlsplit(self.normalized_endpoint)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                    or endpoint.username is not None or endpoint.password is not None
                    or endpoint.query or endpoint.fragment):
                raise ValueError
            host = endpoint.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            port = endpoint.port
            if port is not None and port != {"http": 80, "https": 443}[endpoint.scheme]:
                host += f":{port}"
            normalized = urlunsplit((endpoint.scheme, host, endpoint.path.rstrip("/"), "", ""))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("endpoint must be an absolute HTTP(S) URL without credentials, query or fragment") from None
        object.__setattr__(self, "normalized_endpoint", normalized)

    def _key(self) -> tuple[str, str, str, str]:
        return (self.provider_id, self.normalized_endpoint,
                self.credential_scope_id, self.credential_generation)


def _cooldown_seconds(reason: str, retry_after_seconds: float | None) -> int:
    if retry_after_seconds is not None and not math.isfinite(retry_after_seconds):
        raise ValueError("retry_after_seconds must be finite")
    if retry_after_seconds and retry_after_seconds > 0:
        return int(retry_after_seconds)
    if reason == "no_output":
        return _NO_OUTPUT_COOLDOWN_SECONDS
    if reason in {"timeout", "Timeout"}:
        return _TIMEOUT_COOLDOWN_SECONDS
    if reason and reason != "rate_limit":
        return _MODEL_UNAVAILABLE_COOLDOWN_SECONDS
    return _DEFAULT_COOLDOWN_SECONDS


class AvailabilityStore:
    """SQLite health keyed by scope and provider-local model ID, never list index.

    ``status`` returns None when healthy, otherwise the legacy tracker-shaped
    dict (model, reason, unhealthy_until). Auth quarantine has no expiry.
    Observation times are Unix seconds, preferably captured when the operation
    starts; stale completions cannot override newer observations. Equal times
    favor failure. Recovery watermarks persist to reject delayed old failures.
    No connection is shared across operations, threads, or processes.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path).absolute()
        self._clock = clock
        missing = []
        directory = self.path.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o2770)
            except FileExistsError:
                pass
            else:
                directory.chmod(0o2770)
        # Set the database mode before SQLite creates sidecars, which inherit it.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            if os.fstat(descriptor).st_mode & 0o777 != 0o660:
                os.fchmod(descriptor, 0o660)
        finally:
            os.close(descriptor)
        with self._transaction(initialize=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported availability schema version")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS availability (
                    provider_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    credential_scope_id TEXT NOT NULL,
                    credential_generation TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('auth', 'model')),
                    model TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    reason TEXT,
                    unhealthy_until REAL,
                    PRIMARY KEY (provider_id, endpoint, credential_scope_id,
                                 credential_generation, kind, model)
                ) WITHOUT ROWID
            """)
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _transaction(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            if initialize:
                self._enable_wal(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._sidecar_permissions()
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _sidecar_permissions(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                path = Path(str(self.path) + suffix)
                if path.stat().st_mode & 0o777 != 0o660:
                    path.chmod(0o660)
            except FileNotFoundError:
                pass

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        # Switching a fresh database to WAL may return BUSY without honoring
        # busy_timeout when another process is initializing the same file.
        deadline = time.monotonic() + 30
        while True:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _observed_at(self, observed_at: float | None) -> float:
        value = self._clock() if observed_at is None else observed_at
        if not math.isfinite(value):
            raise ValueError("observation time must be finite")
        return float(value)

    @staticmethod
    def _model(model: str) -> str:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty provider-local ID")
        return model

    def status(self, scope: CredentialScope, model: str) -> dict[str, object] | None:
        model = self._model(model)
        now = self._observed_at(None)
        with self._transaction() as connection:
            rows = connection.execute("""
                SELECT kind, reason, unhealthy_until FROM availability
                WHERE provider_id=? AND endpoint=? AND credential_scope_id=?
                  AND credential_generation=? AND reason IS NOT NULL
                  AND ((kind='auth' AND model='') OR (kind='model' AND model=?))
                ORDER BY kind
            """, (*scope._key(), model)).fetchall()
        for kind, reason, until in rows:
            if kind == "auth" or now < until:
                return {"model": model, "reason": reason, "unhealthy_until": until}
        return None

    def _record(self, scope: CredentialScope, kind: str, model: str,
                observed_at: float, reason: str | None, until: float | None) -> None:
        with self._transaction() as connection:
            # Keep success tombstones: deleting would let late failures resurrect.
            connection.execute("""
                INSERT INTO availability VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider_id, endpoint, credential_scope_id,
                             credential_generation, kind, model)
                DO UPDATE SET observed_at=excluded.observed_at,
                              reason=excluded.reason,
                              unhealthy_until=excluded.unhealthy_until
                WHERE excluded.observed_at > availability.observed_at
                   OR (excluded.observed_at = availability.observed_at
                       AND excluded.reason IS NOT NULL)
            """, (*scope._key(), kind, model, observed_at, reason, until))

    def record_failure(self, scope: CredentialScope, model: str, reason: str,
                       retry_after_seconds: float | None = None,
                       observed_at: float | None = None) -> None:
        """Persist existing failure classifications; auth applies to the scope."""
        model = self._model(model)
        at = self._observed_at(observed_at)
        if reason == _AUTH_REASON:
            self._record(scope, "auth", "", at, reason, None)
        else:
            until = at + _cooldown_seconds(reason, retry_after_seconds)
            if not math.isfinite(until):
                raise ValueError("cooldown expiry must be finite")
            self._record(scope, "model", model, at, reason or "model_unavailable", until)

    def record_success(self, scope: CredentialScope, model: str,
                       observed_at: float | None = None) -> None:
        """Clear only this model's older transient failure, never auth quarantine."""
        self._record(scope, "model", self._model(model),
                     self._observed_at(observed_at), None, None)

    def clear_auth_quarantine(self, scope: CredentialScope,
                              observed_at: float | None = None) -> None:
        """Explicit recovery after authenticated inventory refresh/operator action."""
        self._record(scope, "auth", "", self._observed_at(observed_at), None, None)
