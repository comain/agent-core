"""Tests for shutdown reaping of OpenCode child processes."""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest

from agent_core.harness import shutdown as sd


@pytest.fixture(autouse=True)
def clean():
    sd.reset_for_tests()
    yield
    sd.reset_for_tests()


def _sleeper():
    """A real child in its own process group, like a turn."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )


# -- tracking ------------------------------------------------------------------


def test_tracking_is_scoped_to_the_block():
    proc = _sleeper()
    try:
        assert sd.active_process_count() == 0
        with sd.track(proc):
            assert sd.active_process_count() == 1
        assert sd.active_process_count() == 0
    finally:
        proc.kill()


def test_tracking_is_released_even_when_the_turn_raises():
    proc = _sleeper()
    try:
        with pytest.raises(RuntimeError):
            with sd.track(proc):
                raise RuntimeError("turn failed")
        assert sd.active_process_count() == 0
    finally:
        proc.kill()


def test_tracking_does_not_require_a_pid_attribute():
    """Registration happens the instant a process is spawned.

    It must not depend on an attribute a caller (or a test double) may not
    expose -- the pid is only needed when actually signalling.
    """
    class NoPid:
        pass

    with sd.track(NoPid()):
        assert sd.active_process_count() == 1


# -- reaping -------------------------------------------------------------------


def test_terminate_kills_tracked_processes():
    proc = _sleeper()
    with sd.track(proc):
        assert proc.poll() is None
        assert sd.terminate_active_processes(grace_seconds=2.0) == 1
        deadline = time.monotonic() + 3
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert proc.poll() is not None, "process should be dead"


def test_terminate_with_nothing_tracked_is_a_noop():
    assert sd.terminate_active_processes() == 0


def test_untracked_process_is_left_alone():
    """Only processes this service spawned are reaped."""
    proc = _sleeper()
    try:
        sd.terminate_active_processes()
        assert proc.poll() is None
    finally:
        proc.kill()


def test_sigkill_follows_a_process_that_ignores_sigterm():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
        start_new_session=True,
    )
    with sd.track(proc):
        sd.terminate_active_processes(grace_seconds=0.3)
    deadline = time.monotonic() + 3
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert proc.poll() is not None, "SIGTERM was ignored; SIGKILL should have followed"


# -- handlers ------------------------------------------------------------------


def test_shutdown_requested_reflects_state():
    assert sd.shutdown_requested() is False
    sd._shutting_down.set()
    assert sd.shutdown_requested() is True


def test_install_is_idempotent():
    sd.install_shutdown_handlers()
    first = signal.getsignal(signal.SIGTERM)
    sd.install_shutdown_handlers()
    assert signal.getsignal(signal.SIGTERM) is first


def test_graceful_first_signal_reaps_but_does_not_exit():
    """The daemon gets a chance to drain rather than being killed mid-task."""
    sd.install_shutdown_handlers(graceful=True, grace_seconds=0.1)
    handler = signal.getsignal(signal.SIGTERM)
    proc = _sleeper()
    with sd.track(proc):
        handler(signal.SIGTERM, None)          # must not raise
        assert sd.shutdown_requested() is True
    deadline = time.monotonic() + 3
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert proc.poll() is not None, "children are reaped even in graceful mode"


def test_graceful_second_signal_exits():
    """Two Ctrl-Cs mean the operator is done waiting."""
    sd.install_shutdown_handlers(graceful=True, grace_seconds=0.1)
    handler = signal.getsignal(signal.SIGTERM)
    handler(signal.SIGTERM, None)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)


def test_non_graceful_signal_exits_immediately():
    sd.install_shutdown_handlers(graceful=False, grace_seconds=0.1)
    handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)


def test_sigint_raises_keyboard_interrupt():
    sd.install_shutdown_handlers(graceful=False, grace_seconds=0.1)
    handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
