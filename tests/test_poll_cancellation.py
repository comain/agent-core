"""Stopping a turn that is already running.

Without this the poll loop ends only on completion, a stall, or expiry -- so a
stop requested by an operator waits out the whole timeout, up to fifteen
minutes of a turn nobody wants any more and is still paying for.

`is_cancelled` is the contract the rest of the package already uses, in git
operations and in the process runner; this brings the session poll in line.
"""

from __future__ import annotations

from agent_core.harness.fallback import poll_completion_with_task_guard


class SlowClient:
    """A session that never finishes on its own."""

    def __init__(self):
        self.polls = 0

    def poll_completion(self, session_id, timeout=600, is_cancelled=None, **kw):
        # Stand in for the real loop: check the predicate each pass.
        for _ in range(100):
            self.polls += 1
            if is_cancelled is not None and is_cancelled():
                return {"type": "cancelled", "result": "", "reason": "task cancelled by operator"}
        return {"type": "timeout", "result": ""}


def test_a_cancelled_turn_stops_promptly():
    client = SlowClient()

    event = poll_completion_with_task_guard(
        client, "ses1", timeout_seconds=900, phase="generate",
        is_cancelled=lambda: True,
    )

    assert event["type"] == "cancelled"
    assert client.polls == 1, "it should stop on the first check, not run the loop out"


def test_no_predicate_leaves_behaviour_unchanged():
    client = SlowClient()

    event = poll_completion_with_task_guard(
        client, "ses1", timeout_seconds=900, phase="generate",
    )

    assert event["type"] == "timeout"


def test_a_predicate_that_says_no_does_not_interrupt():
    client = SlowClient()

    event = poll_completion_with_task_guard(
        client, "ses1", timeout_seconds=900, phase="generate",
        is_cancelled=lambda: False,
    )

    assert event["type"] == "timeout"


def test_the_workspace_guard_still_runs_after_a_cancellation():
    """A cancelled turn may have left the checkout dirty -- which is exactly
    when a consumer wants to be told."""
    client = SlowClient()
    calls = []

    poll_completion_with_task_guard(
        client, "ses1", timeout_seconds=900, phase="generate",
        guard_state={"task_id": 1},
        batch=["pkg.A"],
        guard_before=lambda state, targets, phase: calls.append("before") or "snap",
        guard_after=lambda state, snap: calls.append("after"),
        is_cancelled=lambda: True,
    )

    assert calls == ["before", "after"]


def test_the_real_client_signature_accepts_it():
    """Guards against the helper threading a keyword the client will reject."""
    import inspect

    from agent_core.harness.client import OpenCodeClient

    assert "is_cancelled" in inspect.signature(OpenCodeClient.poll_completion).parameters


def test_no_predicate_leaves_the_run_turn_call_untouched():
    """`is_cancelled=None` means "no predicate"; it should not appear at all.

    Passing it anyway would be equivalent for the process and would change the
    call every existing caller and test double sees.
    """
    from agent_core.harness.client import OpenCodeClient

    seen = {}

    class Process:
        def run_turn(self, message, **kwargs):
            seen.update(kwargs)
            from agent_core.harness.process import TurnResult

            return TurnResult(type="completed", result="ok")

    client = OpenCodeClient.__new__(OpenCodeClient)
    client._process = Process()
    client._repo_path = "."
    from agent_core.config import current_config

    client._config = current_config()

    class State:
        pending_message = "hi"
        pending_model_id = None
        model_id = None
        pending_variant = None
        variant = None
        opencode_session_id = "s"
        accumulated_tokens = {}
        patch_count = 0
        turn_texts = []
        latest_result = None

    client._sessions = {"ses1": State()}

    client.poll_completion("ses1", timeout=10)

    assert "is_cancelled" not in seen


def test_a_client_without_the_parameter_still_works():
    """Every client written before this existed, and every test double.

    Threading `is_cancelled=None` would be equivalent for a client that
    supports it and a TypeError for one that does not.
    """
    class OldClient:
        def poll_completion(self, session_id, timeout=600):
            return {"type": "completed", "result": "ok"}

    event = poll_completion_with_task_guard(
        OldClient(), "ses1", timeout_seconds=900, phase="generate",
    )

    assert event["type"] == "completed"


def test_a_client_without_the_parameter_still_works_under_the_guard():
    class OldClient:
        def poll_completion(self, session_id, timeout=600):
            return {"type": "completed", "result": "ok"}

    event = poll_completion_with_task_guard(
        OldClient(), "ses1", timeout_seconds=900, phase="generate",
        guard_state={"task_id": 1}, batch=["pkg.A"],
        guard_before=lambda *a: "snap", guard_after=lambda *a: None,
    )

    assert event["type"] == "completed"


def test_a_cancellation_predicate_does_not_break_an_old_client():
    class OldClient:
        def poll_completion(self, session_id, timeout=600):
            return {"type": "completed", "result": "ok"}

    event = poll_completion_with_task_guard(
        OldClient(),
        "ses1",
        timeout_seconds=900,
        phase="generate",
        is_cancelled=lambda: False,
    )

    assert event["type"] == "completed"
