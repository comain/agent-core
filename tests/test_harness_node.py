"""A harness node, exercised as each of the four shapes that exist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.harness.node import NodeOutcome, NullRecorder, run_harness_node
from agent_core.harness.records import TurnRecord


class Turn:
    def __init__(self, type="completed", result='{"findings": []}', session_id="ses1",
                 tokens=None, cost_usd=0.5, model_id="token-pool/gpt-5.5", error=None):
        self.type = type
        self.result = result
        self.session_id = session_id
        self.tokens = tokens if tokens is not None else {"input": 10, "output": 5, "total": 15}
        self.cost_usd = cost_usd
        self.model_id = model_id
        self.error = error
        self.raw_log_path = "/tmp/raw.jsonl"


class Runner:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.prompts = []
        self.calls = 0

    def run_turn(self, *, prompt_file=None, message=None, repo_path, **kw):
        self.calls += 1
        self.prompts.append(Path(prompt_file).name if prompt_file is not None else message)
        self.last_kwargs = {"prompt_file": prompt_file, "message": message, **kw}
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Recorder:
    """A product's bookkeeping, reduced to a transcript."""

    def __init__(self):
        self.events = []
        self.next_handle = 0

    def start(self, *, node, attempt, max_attempts, model_id):
        self.next_handle += 1
        self.events.append(("start", node, attempt, model_id))
        return self.next_handle

    def finish(self, handle, record: TurnRecord):
        self.events.append(("finish", handle, record.status, record.error))

    def retry_scheduled(self, handle, *, attempt, max_attempts, error):
        self.events.append(("retry", handle, attempt, error))

    def gave_up(self, handle, *, attempt, error):
        self.events.append(("gave_up", handle, attempt, error))

    def skipped(self, handle, *, attempt, error):
        self.events.append(("skipped", handle, attempt, error))


def fixed_prompt(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("review this", encoding="utf-8")
    return lambda attempt, feedback: path


# -- the reviewer shape ----------------------------------------------------------


def test_a_string_prompt_is_sent_as_message(tmp_path):
    runner = Runner(Turn())
    outcome = run_harness_node(
        runner,
        name="plan",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: "plan this",
        parse=json.loads,
    )
    assert outcome.accepted
    assert runner.last_kwargs["message"] == "plan this"
    assert runner.last_kwargs["prompt_file"] is None


def test_a_reviewer_node_records_a_run_and_returns_the_parsed_answer(tmp_path):
    recorder = Recorder()
    runner = Runner(Turn(result='{"findings": [{"title": "bug"}]}'))

    outcome = run_harness_node(
        runner, name="correctness", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=json.loads, recorder=recorder, default_model="token-pool/gpt-5.5",
    )

    assert outcome.accepted
    assert outcome.payload["findings"] == [{"title": "bug"}]
    assert recorder.events == [
        ("start", "correctness", 1, "token-pool/gpt-5.5"),
        ("finish", 1, "success", None),
    ]


def test_the_recorded_row_carries_usage_in_stored_column_names(tmp_path):
    recorder = Recorder()
    runner = Runner(Turn(tokens={"input": 100, "output": 20, "total": 120, "cache": {"read": 5}}))

    outcome = run_harness_node(
        runner, name="correctness", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=json.loads, recorder=recorder,
    )

    assert outcome.record.usage["input_tokens"] == 100
    assert outcome.record.usage["total_tokens"] == 120
    assert outcome.record.usage["cache_read_tokens"] == 5


def test_a_turn_without_a_session_id_is_not_a_success(tmp_path):
    """There is nothing to attribute the cost or the transcript to."""
    recorder = Recorder()
    runner = Runner(Turn(session_id=None))

    outcome = run_harness_node(
        runner, name="correctness", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        recorder=recorder,
    )

    assert not outcome.accepted
    assert outcome.record.status == "failed"
    assert ("gave_up", 1, 1, "missing session id") in recorder.events


def test_every_attempt_is_opened_and_closed(tmp_path):
    """An attempt row left open is a run that looks like it is still going."""
    recorder = Recorder()
    runner = Runner(Turn(result="not json"), Turn(result="not json"), Turn())

    run_harness_node(
        runner, name="correctness", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=json.loads, attempts=3, recorder=recorder,
    )

    starts = [e for e in recorder.events if e[0] == "start"]
    finishes = [e for e in recorder.events if e[0] == "finish"]
    assert len(starts) == len(finishes) == 3


# -- the feedback shape ----------------------------------------------------------


def test_a_required_key_missing_from_the_answer_rejects_the_attempt(tmp_path):
    def parse(text):
        payload = json.loads(text)
        if "resolved" not in payload:
            raise ValueError("no 'resolved' key")
        return payload

    runner = Runner(Turn(result='{"reply": "ok"}'), Turn(result='{"resolved": true}'))
    outcome = run_harness_node(
        runner, name="feedback", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=parse, attempts=2,
    )

    assert outcome.accepted
    assert outcome.payload == {"resolved": True}
    assert outcome.attempts == 2


# -- the judge shape -------------------------------------------------------------


def test_giving_up_is_reported_once_with_the_last_reason(tmp_path):
    recorder = Recorder()
    runner = Runner(Turn(result="not json"))

    outcome = run_harness_node(
        runner, name="cr_judge", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=json.loads, attempts=2, recorder=recorder,
    )

    assert outcome.status == "rejected"
    gave_up = [e for e in recorder.events if e[0] == "gave_up"]
    assert len(gave_up) == 1
    assert "unusable answer" in gave_up[0][3]


# -- the repair shape ------------------------------------------------------------


def test_a_repair_node_builds_each_prompt_from_the_last_failure(tmp_path):
    """The point of a repair step: tell the model what broke."""
    written = []

    def prompt(attempt, feedback):
        path = tmp_path / f"repair-{attempt}.md"
        path.write_text(f"fix this: {feedback}", encoding="utf-8")
        written.append(feedback)
        return path

    compiles_on = 3

    def accept(result, payload):
        return None if int(payload["attempt"]) >= compiles_on else "compile error: missing symbol"

    runner = Runner(*[Turn(result=json.dumps({"attempt": n})) for n in (1, 2, 3)])
    outcome = run_harness_node(
        runner, name="repair", repo_path=tmp_path, prompt=prompt,
        parse=json.loads, accept=accept, attempts=4,
    )

    assert outcome.accepted
    assert written == [None, "compile error: missing symbol", "compile error: missing symbol"]
    assert runner.prompts == ["repair-1.md", "repair-2.md", "repair-3.md"]


def test_an_external_check_that_never_passes_gives_up(tmp_path):
    runner = Runner(Turn(result="{}"), Turn(result="{}"))
    outcome = run_harness_node(
        runner, name="repair", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=json.loads, accept=lambda result, payload: "still broken", attempts=2,
    )
    assert outcome.status == "rejected"
    assert outcome.error == "still broken"


def test_a_stalled_harness_node_resumes_through_the_same_neutral_runner(tmp_path):
    recovery = tmp_path / "continue.md"
    recovery.write_text("continue from the current session", encoding="utf-8")
    runner = Runner(
        Turn(type="stalled_no_progress", result=""),
        Turn(result='{"continued": true}'),
    )

    outcome = run_harness_node(
        runner,
        name="generate",
        repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path),
        parse=json.loads,
        recovery_prompt=lambda result, reason: recovery,
        timeout_seconds=900,
    )

    assert outcome.accepted
    assert outcome.payload == {"continued": True}
    assert outcome.attempts == 1
    assert runner.prompts == ["prompt.md", "continue.md"]


def test_a_node_cannot_mix_the_neutral_recovery_prompt_with_a_custom_recover_hook(tmp_path):
    with pytest.raises(ValueError, match="recovery_prompt"):
        run_harness_node(
            Runner(Turn()),
            name="generate",
            repo_path=tmp_path,
            prompt=fixed_prompt(tmp_path),
            recovery_prompt=fixed_prompt(tmp_path),
            recover=lambda **kwargs: None,
        )


# -- policy ----------------------------------------------------------------------


def test_a_non_retryable_error_stops_immediately_and_closes_the_row(tmp_path):
    recorder = Recorder()
    runner = Runner(RuntimeError("invalid request: context too long"))

    outcome = run_harness_node(
        runner, name="correctness", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        attempts=3, recorder=recorder, retryable=lambda exc: "timeout" in str(exc),
    )

    assert outcome.unreachable
    assert runner.calls == 1
    assert [e[0] for e in recorder.events] == ["start", "finish", "gave_up"]


def test_never_reaching_the_model_is_distinguished_from_a_bad_answer(tmp_path):
    unreachable = run_harness_node(
        Runner(RuntimeError("connection refused")), name="n", repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path), attempts=1,
    )
    bad_answer = run_harness_node(
        Runner(Turn(result="not json")), name="n", repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path), parse=json.loads, attempts=1,
    )

    assert unreachable.unreachable
    assert not bad_answer.unreachable
    assert bad_answer.status == "rejected"


def test_a_caller_with_nothing_to_persist_supplies_no_recorder(tmp_path):
    outcome = run_harness_node(
        Runner(Turn()), name="n", repo_path=tmp_path, prompt=fixed_prompt(tmp_path),
        parse=json.loads,
    )
    assert outcome.accepted


# -- steps the workflow can do without -------------------------------------------


def test_an_optional_step_that_fails_is_skipped_not_failed(tmp_path):
    """An optional reviewer whose answer will not parse is not a broken review.

    The caller should not have to tell "failed and matters" from "failed and
    does not" by reading the error text.
    """
    recorder = Recorder()
    outcome = run_harness_node(
        Runner(Turn(result="not json")), name="performance", repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path), parse=json.loads, recorder=recorder, on_failure="skip",
    )

    assert outcome.skipped
    assert not outcome.accepted
    assert outcome.payload == {}
    assert [e[0] for e in recorder.events] == ["start", "finish", "skipped"]


def test_a_skipped_step_is_reported_to_the_recorder_as_a_skip(tmp_path):
    """A product records the two differently; one is not an incident."""
    recorder = Recorder()
    run_harness_node(
        Runner(RuntimeError("provider down")), name="performance", repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path), recorder=recorder, on_failure="skip",
    )
    assert not [e for e in recorder.events if e[0] == "gave_up"]
    assert [e for e in recorder.events if e[0] == "skipped"]


def test_an_optional_step_that_succeeds_is_still_accepted(tmp_path):
    outcome = run_harness_node(
        Runner(Turn()), name="performance", repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path), parse=json.loads, on_failure="skip",
    )
    assert outcome.accepted
    assert not outcome.skipped


def test_a_required_step_still_reports_failure(tmp_path):
    outcome = run_harness_node(
        Runner(Turn(result="not json")), name="correctness", repo_path=tmp_path,
        prompt=fixed_prompt(tmp_path), parse=json.loads,
    )
    assert outcome.status == "rejected"
    assert not outcome.skipped


def test_an_unknown_failure_policy_is_refused(tmp_path):
    """Silently treating a typo as "fail" would hide an intended skip."""
    with pytest.raises(ValueError, match="on_failure must be"):
        run_harness_node(
            Runner(Turn()), name="n", repo_path=tmp_path,
            prompt=fixed_prompt(tmp_path), on_failure="ignore",
        )
