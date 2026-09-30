"""Reap OpenCode child processes when the service goes down.

`OpenCodeProcess` spawns each turn into its own process group so a wedged model
run can be killed without taking the service with it. The consequence is that
those groups **outlive the service** unless something reaps them: a deploy sends
SIGTERM, the service exits, and every in-flight OpenCode process keeps running,
holding a provider connection and burning tokens for a task nobody is waiting
for any more.

Nothing in the upstream harness handles that. This is adapted from the one
consumer that solved it.

    install_shutdown_handlers(graceful=True)   # once, at service start

Two modes:

* default -- the signal reaps children and then re-raises, so the process exits
  as the operator expects.
* ``graceful=True`` -- the *first* signal reaps children and returns, letting a
  task daemon notice :func:`shutdown_requested` and finish its current work
  cleanly. A second signal exits immediately, which is what an impatient
  operator pressing Ctrl-C twice means.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator

logger = logging.getLogger(__name__)

#: object id -> process. See :func:`track` for why not pid.
_active: Dict[int, subprocess.Popen] = {}
_lock = threading.Lock()
_installed = False
_shutting_down = threading.Event()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the process's whole group, falling back to the process itself.

    The group is what matters: OpenCode spawns its own children, and signalling
    only the parent leaves them running.
    """
    pid = getattr(proc, "pid", None)
    if pid is not None:
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError, AttributeError):
        pass


@contextmanager
def track(proc: subprocess.Popen) -> Iterator[subprocess.Popen]:
    """Register a process for shutdown reaping, for the duration of the block."""
    # Keyed on object identity rather than pid: registration happens the instant
    # the process is spawned, and must not depend on an attribute the caller may
    # not expose. The pid is only needed when actually signalling.
    key = id(proc)
    with _lock:
        _active[key] = proc
    try:
        yield proc
    finally:
        with _lock:
            _active.pop(key, None)


def active_process_count() -> int:
    with _lock:
        return len(_active)


def terminate_active_processes(grace_seconds: float = 2.0) -> int:
    """SIGTERM every tracked group, then SIGKILL whatever is still alive.

    Returns how many processes were signalled. The grace period exists so a
    model turn gets a chance to flush its output; it is short because the
    service is already going down.
    """
    with _lock:
        processes = list(_active.values())
    if not processes:
        return 0

    for proc in processes:
        _signal_group(proc, signal.SIGTERM)

    deadline = time.monotonic() + max(0.0, grace_seconds)
    remaining = processes
    while remaining and time.monotonic() < deadline:
        remaining = [p for p in remaining if p.poll() is None]
        if remaining:
            time.sleep(0.05)

    for proc in remaining:
        logger.warning("opencode process %s ignored SIGTERM; killing", proc.pid)
        _signal_group(proc, signal.SIGKILL)
    return len(processes)


def shutdown_requested() -> bool:
    """Whether a shutdown signal has been seen.

    A task daemon polls this to stop claiming new work while finishing what it
    already holds.
    """
    return _shutting_down.is_set()


def install_shutdown_handlers(*, graceful: bool = False, grace_seconds: float = 2.0) -> None:
    """Install SIGTERM/SIGINT handlers. Idempotent."""
    global _installed
    if _installed:
        return

    def _handler(signum: int, _frame: Any) -> None:
        already = _shutting_down.is_set()
        _shutting_down.set()
        count = terminate_active_processes(grace_seconds)
        if count:
            logger.info("shutdown: signalled %d opencode process(es)", count)
        if graceful and not already:
            # Let the caller drain. A second signal takes the branch below.
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    _installed = True


def reset_for_tests() -> None:
    """Clear registry and state. Tests only."""
    global _installed
    with _lock:
        _active.clear()
    _shutting_down.clear()
    _installed = False
