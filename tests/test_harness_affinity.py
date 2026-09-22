"""Tests for session affinity and the harness test double."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_core.harness import FakeOpenCodeProcess, SessionAffinity, TurnResult


# -- the fake ------------------------------------------------------------------


def test_fake_returns_scripted_answers_in_order():
    proc = FakeOpenCodeProcess(["one", "two"])
    assert proc.run_turn("a").result == "one"
    assert proc.run_turn("b").result == "two"


def test_fake_repeats_its_last_entry():
    """A test that cares about the first turn need not enumerate the rest."""
    proc = FakeOpenCodeProcess(["only"])
    assert [proc.run_turn("x").result for _ in range(3)] == ["only"] * 3


def test_fake_records_what_it_was_asked():
    proc = FakeOpenCodeProcess()
    proc.run_turn("design something", repo_path="/repo", model_id="p/m")
    assert proc.call_count == 1
    assert proc.last_call["message"] == "design something"
    assert proc.last_call["repo_path"] == "/repo"
    assert proc.prompts == ["design something"]


def test_fake_can_raise():
    proc = FakeOpenCodeProcess([RuntimeError("provider exploded")])
    with pytest.raises(RuntimeError, match="provider exploded"):
        proc.run_turn("x")


def test_fake_can_return_a_prepared_result():
    prepared = TurnResult(type="rate_limited", fallback_eligible=True)
    proc = FakeOpenCodeProcess([prepared])
    assert proc.run_turn("x") is prepared


def test_empty_script_yields_completed_turns():
    assert FakeOpenCodeProcess().run_turn("x").type == "completed"


def test_fake_echoes_the_session_it_was_given():
    """Otherwise a constant session id would mask affinity behaviour."""
    proc = FakeOpenCodeProcess(["ok"])
    assert proc.run_turn("x", session_id="ses-7").session_id == "ses-7"


# -- affinity ------------------------------------------------------------------


def test_first_turn_starts_a_session():
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="hello", repo_path="/r")
    assert proc.calls[0]["session_id"] is None
    assert aff.session_id("key") == "ses-1"
    assert aff.turn_count("key") == 1


def test_second_turn_continues_the_same_session():
    """The whole point: a later turn should remember the earlier one."""
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="propose", repo_path="/r")
    aff.run(proc, "key", message="revise", repo_path="/r")
    assert proc.calls[1]["session_id"] == "ses-1"
    assert aff.turn_count("key") == 2


def test_isolated_workspace_starts_fresh_with_self_contained_message():
    """A session must not retain paths into a discarded turn workspace."""
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    proc.isolate_attempts = True

    aff.run(
        proc,
        "key",
        message="write the page",
        bootstrap_message="full page context",
        repo_path="/r",
        model_id="p/m1",
    )
    aff.run(
        proc,
        "key",
        message="repair the staged candidate",
        bootstrap_message="full page context",
        repo_path="/r",
    )

    assert proc.calls[1]["session_id"] is None
    assert proc.calls[1]["model_id"] == "p/m1"
    assert proc.calls[1]["message"] == (
        "full page context\n\n"
        "Current continuation instruction:\n"
        "repair the staged candidate"
    )


def test_continue_without_model_id_pins_the_bound_model():
    """Otherwise last-success from another page can continue this session
    under a different model."""
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="propose", repo_path="/r", model_id="p/m1")
    aff.run(proc, "key", message="revise", repo_path="/r")
    assert proc.calls[1]["session_id"] == "ses-1"
    assert proc.calls[1]["model_id"] == "p/m1"


def test_same_model_continues_the_session():
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="propose", repo_path="/r", model_id="p/m1")
    aff.run(proc, "key", message="revise", repo_path="/r", model_id="p/m1")
    assert proc.calls[1]["session_id"] == "ses-1"
    assert aff.model_id("key") == "p/m1"


def test_model_mismatch_starts_fresh_with_bootstrap():
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="propose", repo_path="/r", model_id="p/m1")
    aff.run(
        proc,
        "key",
        message="revise",
        repo_path="/r",
        model_id="p/m2",
        bootstrap_message="full bootstrap",
    )
    assert proc.calls[1]["session_id"] is None
    assert proc.calls[1]["message"] == "full bootstrap"


def test_different_keys_get_different_sessions():
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "task-1", message="x", repo_path="/r")
    aff.run(proc, "task-2", message="y", repo_path="/r")
    assert proc.calls[1]["session_id"] is None, "an unrelated key must start fresh"


def test_reset_forces_a_new_session():
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="x", repo_path="/r")
    aff.reset("key")
    assert aff.has_session("key") is False
    aff.run(proc, "key", message="y", repo_path="/r")
    assert proc.calls[1]["session_id"] is None


def test_max_turns_rotates_the_session():
    """Context grows every turn; an unbounded session eventually costs more."""
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity(max_turns=2)
    for _ in range(3):
        aff.run(proc, "key", message="x", repo_path="/r")
    assert [c["session_id"] for c in proc.calls] == [None, "ses-1", None]


def test_explicit_session_id_wins():
    """An explicit choice must not be silently overridden by bookkeeping."""
    proc, aff = FakeOpenCodeProcess(["a"], session_id="ses-1"), SessionAffinity()
    aff.run(proc, "key", message="x", repo_path="/r")
    aff.run(proc, "key", message="y", repo_path="/r", session_id="forced")
    assert proc.calls[1]["session_id"] == "forced"


def test_a_turn_without_a_session_id_does_not_discard_the_binding():
    """One malformed response should not throw away a working conversation."""
    aff = SessionAffinity()
    good = FakeOpenCodeProcess([TurnResult(type="completed", session_id="ses-1")])
    aff.run(good, "key", message="x", repo_path="/r")

    blank = FakeOpenCodeProcess([TurnResult(type="error", session_id=None)])
    aff.run(blank, "key", message="y", repo_path="/r")
    assert aff.session_id("key") == "ses-1"


def test_concurrent_turns_on_one_key_do_not_each_start_a_session():
    aff = SessionAffinity()
    proc = FakeOpenCodeProcess([TurnResult(type="completed", session_id="ses-1")])
    aff.run(proc, "key", message="seed", repo_path="/r")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: aff.run(proc, "key", message=str(i), repo_path="/r"), range(8)))

    assert all(c["session_id"] == "ses-1" for c in proc.calls[1:])
    assert aff.turn_count("key") == 9
