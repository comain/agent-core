"""Resuming a session that went quiet, rather than starting it again.

`run_until_accepted` has the general shape -- one chance to salvage a
rejection worth salvaging -- but did not know how to salvage an agent
*session*, so each consumer wrote the same procedure: poll, notice a stall,
send one continue prompt into the live session, poll again with a shorter
budget.
"""

from __future__ import annotations

import pytest

from agent_core.harness.recovery import (
    MAX_RESUME_SECONDS,
    MIN_RESUME_SECONDS,
    continue_session,
    is_stall,
    resume_timeout,
    session_recovery,
)
from agent_core.harness.turns import ACCEPTED, run_until_accepted


class FakeClient:
    def __init__(self, second_event=None, raise_on_send=False):
        self.sent = []
        self.polls = []
        self.second_event = second_event or {"type": "completed", "result": "done"}
        self.raise_on_send = raise_on_send

    def send_message(self, session_id, prompt, model_id=None):
        if self.raise_on_send:
            raise RuntimeError("session is gone")
        self.sent.append({"session": session_id, "prompt": prompt, "model_id": model_id})

    def poll_completion(self, session_id, timeout=None, on_update=None, **kw):
        self.polls.append({"timeout": timeout, "kw": kw})
        return self.second_event


# -- classifying the stall -------------------------------------------------

@pytest.mark.parametrize("kind", ["stalled_after_recovery", "stalled_no_progress"])
def test_a_stall_is_recoverable(kind):
    assert is_stall({"type": kind})


@pytest.mark.parametrize("kind", ["completed", "error", "timeout", "rate_limited"])
def test_anything_else_is_not(kind):
    """A bad answer needs a different prompt, not the same one repeated."""
    assert not is_stall({"type": kind})


def test_a_non_event_is_not_a_stall():
    assert not is_stall(None)
    assert not is_stall("stalled_no_progress")


def test_a_neutral_turn_result_can_be_classified_without_converting_it_to_a_dict():
    class Turn:
        type = "stalled_no_progress"

    assert is_stall(Turn())


# -- the resumed budget ----------------------------------------------------

def test_a_tight_budget_is_raised_to_the_floor():
    """Otherwise the continue prompt gets no room to produce anything."""
    assert resume_timeout(10) == MIN_RESUME_SECONDS


def test_a_generous_budget_is_capped():
    """A turn that has shown it is not producing should not cost double."""
    assert resume_timeout(5000) == MAX_RESUME_SECONDS


def test_a_middling_budget_is_kept():
    assert resume_timeout(300) == 300


# -- resuming --------------------------------------------------------------

def test_it_sends_one_prompt_and_polls_again():
    client = FakeClient()

    event = continue_session(client, "ses1", prompt="carry on", timeout=900)

    assert event == {"type": "completed", "result": "done"}
    assert len(client.sent) == 1
    assert client.sent[0]["prompt"] == "carry on"
    assert len(client.polls) == 1


def test_the_resumed_poll_uses_the_bounded_budget():
    client = FakeClient()

    continue_session(client, "ses1", prompt="carry on", timeout=5000)

    assert client.polls[0]["timeout"] == MAX_RESUME_SECONDS


def test_the_model_is_carried_through():
    """Resuming on a different model would start a different conversation."""
    client = FakeClient()

    continue_session(client, "ses1", prompt="x", timeout=900, model_id="token-pool/gpt-5.5")

    assert client.sent[0]["model_id"] == "token-pool/gpt-5.5"


def test_a_hook_runs_before_the_prompt_is_sent():
    """For a consumer that materialises a resume artifact the agent will read."""
    order = []
    client = FakeClient()
    client.send_message = lambda *a, **k: order.append("sent")

    continue_session(
        client, "ses1", prompt="x", timeout=900,
        before_continue=lambda: order.append("artifact"),
    )

    assert order == ["artifact", "sent"]


def test_a_client_that_cannot_resume_returns_nothing():
    """None means recovery did not happen, so the ordinary retry still runs."""
    class Bare:
        pass

    assert continue_session(Bare(), "ses1", prompt="x", timeout=900) is None


def test_a_failed_rescue_returns_nothing_rather_than_raising():
    client = FakeClient(raise_on_send=True)

    assert continue_session(client, "ses1", prompt="x", timeout=900) is None


# -- wired into the loop ---------------------------------------------------

def test_it_plugs_into_run_until_accepted():
    """The whole point: the consumer enables it and supplies a prompt."""
    client = FakeClient(second_event={"type": "completed", "result": "ok"})
    runs = []

    def run(*, attempt, feedback=None):
        runs.append(attempt)
        return {"type": "stalled_no_progress"}

    loop = run_until_accepted(
        run,
        accept=lambda e: None if e.get("type") == "completed" else e.get("type"),
        attempts=3,
        recoverable=is_stall,
        recover=session_recovery(client, session_id="ses1", prompt="carry on", timeout=900),
    )

    assert loop.status == ACCEPTED
    assert loop.result == {"type": "completed", "result": "ok"}
    assert runs == [1], "recovery must not have spent a fresh attempt"


def test_a_stall_that_does_not_recover_falls_through_to_a_retry():
    client = FakeClient(second_event={"type": "stalled_no_progress"})
    runs = []

    def run(*, attempt, feedback=None):
        runs.append(attempt)
        return {"type": "completed"} if attempt == 2 else {"type": "stalled_no_progress"}

    loop = run_until_accepted(
        run,
        accept=lambda e: None if e.get("type") == "completed" else e.get("type"),
        attempts=2,
        recoverable=is_stall,
        recover=session_recovery(client, session_id="ses1", prompt="carry on", timeout=900),
    )

    assert loop.status == ACCEPTED
    assert runs == [1, 2]


def test_a_non_stall_is_never_nudged():
    client = FakeClient()
    run = lambda *, attempt, feedback=None: {"type": "error", "reason": "bad json"}

    run_until_accepted(
        run,
        accept=lambda e: e.get("type") if e.get("type") != "completed" else None,
        attempts=1,
        recoverable=is_stall,
        recover=session_recovery(client, session_id="ses1", prompt="carry on", timeout=900),
    )

    assert client.sent == [], "an error is not a stall"
