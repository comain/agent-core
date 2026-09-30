"""Shared task daemon.

All four consumers grew the same loop -- claim a task under a lease, keep the
lease alive on a background thread while it runs, record liveness, and poll
again -- with product-specific claim queries and workflows bolted on. This is
that loop, with the product-specific parts injected.

The daemon deliberately owns **no** task table. Claiming is a callback because
the four products key work differently (TEXT, INTEGER, parent/child), the same
reason the runtime schema refers to work by an opaque ``task_ref``.

## Why suspension is a first-class outcome

A run that stops at a human gate has not finished and has not failed. If the
daemon cannot tell the difference it will either treat a suspended task as
crashed and reclaim it -- re-running work a human is still reviewing -- or hold
the lease for the whole wait, which is exactly the worker-pinning the gate
design exists to avoid.

So :class:`TaskOutcome` distinguishes the three, and ``SUSPENDED`` releases the
worker without marking failure.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional, Sequence

from agent_core.harness.shutdown import shutdown_requested
from agent_core.runtime.store import RuntimeStore

logger = logging.getLogger(__name__)


class TaskOutcome(str, Enum):
    """What happened to a claimed task."""

    COMPLETED = "completed"
    SUSPENDED = "suspended"  # waiting on a human; not finished, not failed
    FAILED = "failed"


@dataclass
class DaemonConfig:
    poll_interval_seconds: float = 5.0
    lease_seconds: int = 300
    #: How often a maintenance hook runs, in ticks. Maintenance is usually much
    #: cheaper than a task but still wasteful every poll.
    maintenance_every_ticks: int = 1
    idle_heartbeat: bool = True

    @property
    def lease_renew_interval(self) -> float:
        """Renew well inside the lease so one slow cycle does not lose it.

        A third of the lease gives two chances to renew before expiry; clamped
        so a very short lease does not spin and a very long one still renews
        often enough to be a useful liveness signal.
        """
        return max(0.2, min(60.0, max(1.0, float(self.lease_seconds)) / 3.0))


@dataclass
class DaemonPorts:
    """The product-specific parts.

    ``claim`` returns an opaque task reference or ``None``. ``execute`` runs the
    task and reports what happened; raising is treated as
    :attr:`TaskOutcome.FAILED` and never stops the loop.
    """

    claim: Callable[..., Optional[str]]
    execute: Callable[[str], Any]
    renew_lease: Optional[Callable[..., bool]] = None
    release: Optional[Callable[[str, TaskOutcome], None]] = None
    maintenance: Sequence[Callable[[], None]] = field(default_factory=tuple)

    #: Where liveness and failure notices go. Both default to the RuntimeStore
    #: when one is supplied, but a product that already has its own heartbeat
    #: table and event log can adopt this loop without also adopting the
    #: canonical schema -- which is the per-capability adoption ADR-004 promises.
    heartbeat: Optional[Callable[..., None]] = None
    record_event: Optional[Callable[..., None]] = None


#: How much of a failure to carry into the event. Enough to act on, not a
#: stack trace: the log has that.
_SUMMARY_CHARS = 400


def _lines(exc: BaseException) -> list:
    return [line.strip() for line in str(exc).splitlines() if line.strip()]


def _why(exc: BaseException) -> str:
    """One line saying what went wrong, for a person reading the task page.

    "task execution raised" is true of every failure and useful for none. A
    task that stopped because its branch does not exist should say so where it
    is read, rather than only in a log file on the host -- which is one ssh
    and one grep away from the person who can fix it.

    Tools put their summary first and their cause last (`git clone failed
    (128): ...` then `fatal: Remote branch ... not found`), so both are kept
    when they differ.
    """
    lines = _lines(exc)
    if not lines:
        return f"task execution raised: {type(exc).__name__}"
    summary = lines[0]
    cause = lines[-1]
    text = summary if cause == summary else f"{summary} -- {cause}"
    return f"task execution raised: {text}"[:_SUMMARY_CHARS]



class TaskDaemon:
    def __init__(
        self,
        store: Optional[RuntimeStore],
        ports: DaemonPorts,
        *,
        config: Optional[DaemonConfig] = None,
        daemon_id: Optional[str] = None,
        _sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store
        self.ports = ports
        self.config = config or DaemonConfig()
        self.daemon_id = daemon_id or f"{socket.gethostname()}:{os.getpid()}"
        self._sleep = _sleep
        self._tick = 0
        self._stopping = threading.Event()

    # -- liveness ----------------------------------------------------------

    def _beat(self, *, status: str, task_ref: Optional[str], message: str) -> None:
        sink = self.ports.heartbeat or (self.store.heartbeat if self.store else None)
        if sink is None:
            return
        try:
            sink(
                runner_id=self.daemon_id,
                task_ref=task_ref,
                status=status,
                message=message,
                pid=os.getpid(),
                hostname=socket.gethostname(),
            )
        except Exception:  # noqa: BLE001 - liveness must never break the loop
            logger.exception("heartbeat failed daemon_id=%s", self.daemon_id)

    def _event(self, *, task_ref: str, event_type: str, severity: str, message: str) -> None:
        sink = self.ports.record_event or (self.store.append_event if self.store else None)
        if sink is None:
            return
        try:
            sink(task_ref=task_ref, event_type=event_type, severity=severity, message=message)
        except Exception:  # noqa: BLE001
            logger.exception("event record failed task_ref=%s", task_ref)

    def _lease_renewer(self, task_ref: str) -> tuple[threading.Event, Optional[threading.Thread]]:
        """Keep the lease alive on a background thread while the task runs."""
        stop = threading.Event()
        if self.ports.renew_lease is None:
            return stop, None

        def loop() -> None:
            while not stop.is_set():
                try:
                    renewed = self.ports.renew_lease(
                        task_ref, daemon_id=self.daemon_id, lease_seconds=self.config.lease_seconds
                    )
                    if renewed:
                        self._beat(status="RUNNING", task_ref=task_ref, message="task running")
                    else:
                        # Another worker may have taken over, or the task moved
                        # out of running. Loud, because continuing to work on a
                        # task we no longer hold produces duplicate effects.
                        logger.warning(
                            "lease renewal refused task_ref=%s daemon_id=%s", task_ref, self.daemon_id
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("lease renewal failed task_ref=%s", task_ref)
                stop.wait(self.config.lease_renew_interval)

        thread = threading.Thread(target=loop, name=f"lease-{task_ref}", daemon=True)
        thread.start()
        return stop, thread

    # -- one tick ----------------------------------------------------------

    def run_maintenance(self) -> None:
        """Run periodic hooks. One failing hook must not stop the others."""
        for hook in self.ports.maintenance:
            try:
                hook()
            except Exception:  # noqa: BLE001
                logger.exception("maintenance hook failed: %r", getattr(hook, "__name__", hook))

    def once(self) -> Optional[TaskOutcome]:
        """Claim and run at most one task. Returns None when nothing was queued."""
        try:
            task_ref = self.ports.claim(daemon_id=self.daemon_id, lease_seconds=self.config.lease_seconds)
        except Exception:  # noqa: BLE001
            logger.exception("claim failed daemon_id=%s", self.daemon_id)
            return None

        if task_ref is None:
            if self.config.idle_heartbeat:
                self._beat(status="IDLE", task_ref=None, message="no queued task")
            return None

        self._beat(status="RUNNING", task_ref=task_ref, message="task acquired")
        stop, thread = self._lease_renewer(task_ref)
        outcome = TaskOutcome.FAILED
        try:
            result = self.ports.execute(task_ref)
            # Tolerate a callback that reports nothing: the common case is a
            # workflow that either returns or raises.
            outcome = TaskOutcome(result) if result is not None else TaskOutcome.COMPLETED
        except Exception as exc:  # noqa: BLE001 - one bad task must not stop the daemon
            logger.exception("task execution failed task_ref=%s", task_ref)
            self._event(
                task_ref=task_ref,
                event_type="task_failed",
                severity="error",
                message=_why(exc),
            )
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=2.0)

        if self.ports.release is not None:
            try:
                self.ports.release(task_ref, outcome)
            except Exception:  # noqa: BLE001
                logger.exception("release failed task_ref=%s outcome=%s", task_ref, outcome)

        self._beat(
            status="IDLE",
            task_ref=None,
            message=f"task {outcome.value}",
        )
        return outcome

    # -- the loop ----------------------------------------------------------

    def stop(self) -> None:
        """Ask the loop to exit after the current tick."""
        self._stopping.set()

    def run_forever(self, *, max_ticks: Optional[int] = None) -> int:
        """Poll until stopped. Returns the number of ticks executed.

        ``max_ticks`` bounds the loop for tests and one-shot runs; without it
        this runs until :meth:`stop` is called.
        """
        ticks = 0
        # A shutdown signal stops the loop claiming new work. Whatever is
        # already running finishes; the signal handler has already reaped any
        # child processes.
        while not self._stopping.is_set() and not shutdown_requested():
            if max_ticks is not None and ticks >= max_ticks:
                break
            ticks += 1
            self._tick += 1

            if self.ports.maintenance and self._tick % max(1, self.config.maintenance_every_ticks) == 0:
                self.run_maintenance()

            outcome = self.once()

            # Only idle when there was nothing to do. Sleeping after a completed
            # task would leave a full queue draining at one task per interval.
            if outcome is None and not self._stopping.is_set():
                self._sleep(max(0.05, float(self.config.poll_interval_seconds)))
        return ticks
