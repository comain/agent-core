"""`agent_turn` as a projection, and the boundary that protects legacy mode.

cr_plugin runs this node in production today with no config at all. That is the
constraint everything here is arranged around: `result_mode` is the single
activation switch, and every new capability is inert until it is set. A partial
opt-in — someone setting `session_scope` on an old workflow and getting
sessions, guards and a different result shape — is exactly the failure this
boundary exists to prevent.

In normalized mode the node now does two things and nothing else: project state
and config into an `AgentTurnRequest`, and project the normalized result back
as JSON. The lifecycle it used to own — cancellation, session, guard, cost,
progress, durability — belongs to `execute_agent_turn`, and is tested there.
What is tested here is that the projection is faithful in both directions, and
that the graph path and a direct call produce the *same* ordered trace. Two
copies of that ordering drifting apart is what the projection deletes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.harness.execution import (
    AgentTurnContext,
    AgentTurnRequest,
    HarnessBinding,
    execute_agent_turn,
)
from agent_core.harness.sessions import (
    AgentSessionRef,
    FallbackHarnessSession,
    SessionLocatorScope,
    SessionSnapshot,
)
from agent_core.runtime.progress_sink import ProgressFlushResult, SinkProgressPort
from agent_core.workflow.nodes import MissingContextError, agent_turn

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures/contracts/agent_core_v0_6_11.json").read_text(
        encoding="utf-8"
    )
)


def in_0_7_trace(frozen):
    """The frozen trace with admission moved next to the submission it admits.

    0.6.11 admitted once, before the workspace guard ran and before a session
    existed — a gate answering "may I spend?" while nothing was about to be
    spent. 0.7 admits immediately around each provider submission, so a
    single-turn node produces the same two events one step later. Everything
    else in the trace still has to match the baseline.
    """
    gates = [event for event in frozen if event.startswith("cost_gate:")]
    assert len(gates) == 1, "this transform describes the single-submission case"
    rest = [event for event in frozen if not event.startswith("cost_gate:")]
    at = rest.index("turn")
    return rest[:at] + gates + rest[at:]


def in_0_7(frozen, session_refs):
    """The frozen 0.6.11 result with the one field 0.7 deliberately changes.

    The fixture is not rewritten. It is the baseline Phase 0 froze precisely so
    that a change like this one has to be stated: `session_id` — a single
    conversation, which a fallback chain cannot honestly report — becomes
    `session_refs`. Everything else still has to match byte for byte, so any
    *other* drift in the normalized result fails here rather than reaching a
    consumer.
    """
    expected = dict(frozen)
    assert "session_id" in expected, "the fixture no longer has the field 0.7 removed"
    del expected["session_id"]
    expected["session_refs"] = session_refs
    return expected


class FakeResult:
    """A harness result. `session_id` is load-bearing: `run_harness_node`
    accepts an attempt only when the turn completed *and* carries one."""

    def __init__(
        self,
        type="completed",
        result="done",
        session_id="sess-1",
        session_refs=(),
        **extra,
    ):
        self.type = type
        self.result = result
        self.session_id = session_id
        self.session_refs = tuple(session_refs)
        for key, value in extra.items():
            setattr(self, key, value)


class FakeRunner:
    """A harness. Records what it was asked, so the wiring can be checked.

    Also a session: the fake session factory hands the harness straight back,
    so a `session_scope: phase` node exercises the same object.
    """

    session_id = "sess-1"

    def __init__(self, result=None, *, progress=(), trace=None):
        self.result = result or FakeResult()
        self.calls: list = []
        self.progress = list(progress)
        self.trace = trace
        self.closed = 0

    def snapshot(self):
        return SessionSnapshot(session_id="sess-1")

    def close(self):
        self.closed += 1

    def run_turn(self, **kwargs):
        if self.trace is not None:
            self.trace.append("turn")
        self.calls.append(kwargs)
        if on_progress := kwargs.get("on_progress"):
            for update in self.progress:
                on_progress(update)
        if isinstance(self.result, list):
            return self.result[min(len(self.calls), len(self.result)) - 1]
        return self.result


def state(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("do the thing")
    return {"prompt_file": str(prompt), "repo_path": str(tmp_path)}


NORMALIZED = {"result_mode": "normalized"}


class Ports:
    """One object implementing every port, tracing in the frozen vocabulary."""

    def __init__(self, trace, *, cancelled=False, sink=None, guard_error=None):
        self.trace = trace
        self.cancelled = cancelled
        self.persisted: list = []
        self.costs: list = []
        self.rejected: list = []
        self.opened: list = []
        self.guard_error = guard_error
        self._progress = SinkProgressPort(sink)

    def is_cancelled(self):
        return self.cancelled

    def open_session(self, *, harness, repo_path, model_id):
        self.trace.append("open_session")
        self.opened.append(model_id)
        return harness

    def before(self, request):
        self.trace.append("before_turn")
        return "guard"

    def after(self, request, token):
        self.trace.append(f"after_turn:{token}")
        if self.guard_error is not None:
            raise self.guard_error

    def before_paid_attempt(self, *, operation_id, paid_attempt_ordinal):
        self.trace.append(f"cost_gate:{operation_id}:{paid_attempt_ordinal}")

    def after_paid_attempt(self, *, operation_id, paid_attempt_ordinal, cost):
        self.costs.append(
            {
                "operation_id": operation_id,
                "paid_attempt_ordinal": paid_attempt_ordinal,
                "provider_cost_usd": cost.provider_cost_usd,
            }
        )
        self.trace.append(
            f"on_cost:{operation_id}:{paid_attempt_ordinal}:{cost.provider_cost_usd}"
        )

    def callback(self, *, request, session_id):
        return self._progress.callback(request=request, session_id=session_id)

    def flush(self):
        self.trace.append("flush")
        return self._progress.flush()

    def commit(self, request, execution):
        self.persisted.append(execution.result.as_dict())
        self.trace.append(f"on_result:{execution.result.status}")

    def reject(self, request, execution, *, reason):
        self.rejected.append(reason)
        self.trace.append(f"reject:{reason}")


def typed(harness, ports, **over):
    context = AgentTurnContext(
        binding=HarnessBinding(name="opencode", harness=harness),
        cost=ports,
        cancellation=ports,
        sessions=ports,
        guard=ports,
        progress=ports,
        results=ports,
    )
    for key, value in over.items():
        object.__setattr__(context, key, value)
    return context


# -- the activation boundary -------------------------------------------------

def test_a_config_free_turn_is_unchanged(tmp_path):
    """cr_plugin's production call. It must keep the raw result object and
    today's exact keys."""
    runner = FakeRunner()

    out = agent_turn(state(tmp_path), {}, {"runner": runner})

    assert out["turn_result"] is runner.result
    assert out["turn_status"] == "completed"
    assert out["turn_text"] == "done"


def test_legacy_mode_ignores_new_options_rather_than_half_applying_them(tmp_path):
    """A partial opt-in is the dangerous state: sessions and guards silently
    engaging on a workflow that was never designed for them."""
    runner = FakeRunner()

    out = agent_turn(
        state(tmp_path),
        {"session_scope": "phase", "attempts": 5},
        {"runner": runner},
    )

    assert len(runner.calls) == 1, "attempts were applied in legacy mode"
    assert out["turn_result"] is runner.result


def test_legacy_mode_still_honours_its_own_existing_keys(tmp_path):
    runner = FakeRunner()

    out = agent_turn(state(tmp_path), {"output_key": "review", "model_id": "m1"},
                     {"runner": runner})

    assert out["review"] is runner.result
    assert runner.calls[0]["model_id"] == "m1"


def test_legacy_mode_needs_no_turn_context(tmp_path):
    """It is the path that exists precisely because a product configured
    nothing."""
    assert agent_turn(state(tmp_path), {}, {"runner": FakeRunner()})["turn_status"] == (
        "completed"
    )


# -- what crosses the boundary -----------------------------------------------

def test_normalized_mode_returns_a_json_safe_mapping(tmp_path):
    trace: list = []
    ports = Ports(trace)

    out = agent_turn(state(tmp_path), NORMALIZED, {"turn_context": typed(FakeRunner(), ports)})

    assert isinstance(out["turn_result"], dict)
    json.dumps(out["turn_result"])
    assert out["turn_result"]["status"] == "completed"


def test_normalized_mode_never_puts_the_provider_object_on_state(tmp_path):
    """State is checkpointed. A provider object there fails to serialize — or
    worse, succeeds while holding a prompt."""
    trace: list = []
    runner = FakeRunner()

    out = agent_turn(
        state(tmp_path), NORMALIZED, {"turn_context": typed(runner, Ports(trace))}
    )

    assert out["turn_result"] is not runner.result
    assert all(isinstance(value, (str, dict)) for value in out.values())


def test_the_callback_map_lifecycle_is_gone(tmp_path):
    """0.7 removes it. A product still passing the old keys gets an error
    naming what to pass instead, not a turn that silently runs with no guard,
    no cost accounting and no durability."""
    trace: list = []

    with pytest.raises(MissingContextError, match="turn_context"):
        agent_turn(
            state(tmp_path),
            NORMALIZED,
            {
                "runner": FakeRunner(),
                "before_turn": lambda s, c: "token",
                "after_turn": lambda s, c, t: None,
                "on_result": lambda s, c, r: trace.append("persisted"),
            },
        )

    assert trace == []


def test_config_and_state_project_into_the_request(tmp_path):
    trace: list = []
    ports = Ports(trace)
    runner = FakeRunner()

    out = agent_turn(
        {**state(tmp_path), "operation_id": "op-9", "chosen": "big-model"},
        {
            **NORMALIZED,
            "label": "review",
            "output_key": "review_result",
            "model_id": {"from_state": "chosen"},
            "timeout_seconds": 90,
            "session_scope": "phase",
        },
        {"turn_context": typed(runner, ports)},
    )

    assert runner.calls[0]["model_id"] == "big-model"
    assert runner.calls[0]["timeout_seconds"] == 90
    assert ports.opened == ["big-model"]
    assert ports.costs[0]["operation_id"] == "op-9"
    assert "review_result" in out


def test_a_missing_state_key_resolves_to_nothing_rather_than_a_literal(tmp_path):
    """The bug this prevents: sending the model a literal `{'from_state': …}`."""
    trace: list = []
    runner = FakeRunner()

    agent_turn(
        state(tmp_path),
        {**NORMALIZED, "model_id": {"from_state": "absent"}},
        {"turn_context": typed(runner, Ports(trace))},
    )

    assert runner.calls[0]["model_id"] is None


def test_the_graph_and_a_direct_call_produce_the_same_trace(tmp_path):
    """The acceptance criterion for making the node a projection: there is one
    ordering, and both entry points get it. Two copies is how the graph path
    and the products' direct paths drifted in the first place."""
    graph_trace: list = []
    graph_ports = Ports(graph_trace)
    agent_turn(
        {**state(tmp_path), "operation_id": "op-1"},
        {**NORMALIZED, "label": "generate"},
        {"turn_context": typed(FakeRunner(trace=graph_trace), graph_ports)},
    )

    direct_trace: list = []
    direct_ports = Ports(direct_trace)
    prompt = tmp_path / "prompt.txt"
    execute_agent_turn(
        AgentTurnRequest(
            name="generate",
            repo_path=tmp_path,
            prompt=lambda attempt, feedback: prompt,
            operation_id="op-1",
        ),
        typed(FakeRunner(trace=direct_trace), direct_ports),
    )

    assert graph_trace == direct_trace
    assert _without_timing(graph_ports.persisted) == _without_timing(direct_ports.persisted)


def _without_timing(persisted):
    """Wall-clock is the one field two runs cannot share."""
    return [{k: v for k, v in row.items() if k != "elapsed_seconds"} for row in persisted]


# -- frozen 0.6.11 contract --------------------------------------------------

class _TraceSink:
    def __init__(self, trace):
        self.trace = trace

    def publish(self, event):
        self.trace.append("progress")

    def flush(self, *, timeout_seconds=5):
        return ProgressFlushResult()

    def record_failure(self, *, stage):
        self.trace.append(f"progress_failure:{stage}")


@pytest.mark.parametrize(
    ("scenario", "result"),
    [
        ("success", FakeResult()),
        ("failure", FakeResult(type="error", result="")),
    ],
)
def test_turn_results_and_traces_match_the_0_6_11_fixture(
    tmp_path, monkeypatch, scenario, result
):
    ticks = iter((10.0, 12.5))
    monkeypatch.setattr("agent_core.harness.execution.time.monotonic", lambda: next(ticks))
    trace: list = []
    ports = Ports(trace, sink=_TraceSink(trace))

    out = agent_turn(
        {**state(tmp_path), "operation_id": "op-1"},
        NORMALIZED,
        {"turn_context": typed(FakeRunner(result=result, trace=trace), ports)},
    )

    expected = in_0_7(CONTRACT["turn_results"][scenario], [])
    assert out["turn_result"] == expected
    assert ports.persisted == [expected]
    assert trace == in_0_7_trace(CONTRACT["turn_traces"][scenario])


def test_cancelled_turn_result_and_trace_match_the_0_6_11_fixture(tmp_path):
    trace: list = []
    ports = Ports(trace, cancelled=True)

    out = agent_turn(
        {**state(tmp_path), "operation_id": "op-1"},
        NORMALIZED,
        {"turn_context": typed(FakeRunner(trace=trace), ports)},
    )

    expected = in_0_7(CONTRACT["turn_results"]["cancelled"], [])
    assert out["turn_result"] == expected
    assert ports.persisted == [expected]
    assert trace == CONTRACT["turn_traces"]["cancelled"]


def test_guard_rejection_trace_matches_the_0_6_11_fixture(tmp_path, monkeypatch):
    ticks = iter((10.0, 12.5))
    monkeypatch.setattr("agent_core.harness.execution.time.monotonic", lambda: next(ticks))
    trace: list = []
    ports = Ports(
        trace,
        sink=_TraceSink(trace),
        guard_error=RuntimeError("unsafe workspace"),
    )

    with pytest.raises(RuntimeError, match="unsafe workspace"):
        agent_turn(
            {**state(tmp_path), "operation_id": "op-1"},
            NORMALIZED,
            {"turn_context": typed(FakeRunner(trace=trace), ports)},
        )

    assert ports.persisted == []
    # 0.6.11 had nowhere to report a rejection; the attempt and session audit
    # went to no port at all. `reject` is the addition, and it is the last
    # thing that happens.
    assert trace == in_0_7_trace(CONTRACT["turn_traces"]["guard_rejected"]) + [
        "reject:unsafe workspace"
    ]


def test_fallback_refs_cost_and_trace_match_the_0_6_11_fixture(tmp_path, monkeypatch):
    ticks = iter((10.0, 12.5))
    monkeypatch.setattr("agent_core.harness.execution.time.monotonic", lambda: next(ticks))
    trace: list = []

    class CandidateSession:
        def __init__(self, name, *, fallback, cost):
            self.name = name
            self.session_id = f"session-{name}"
            self.ref = AgentSessionRef(
                "opencode", self.session_id, SessionLocatorScope.PROCESS
            )
            self.fallback = fallback
            self.cost = cost

        def run_turn(self, **kwargs):
            trace.append(f"candidate_turn:{self.name}")
            return FakeResult(
                type="error" if self.fallback else "completed",
                result="" if self.fallback else "done",
                session_id=self.session_id,
                fallback_eligible=self.fallback,
                fallback_reason="rate_limit" if self.fallback else None,
                session_refs=(self.ref,),
                cost_usd=self.cost,
            )

        def snapshot(self):
            trace.append(f"candidate_snapshot:{self.name}")
            return SessionSnapshot(
                session_id=self.session_id,
                provider_cost_usd=self.cost,
                patch_count=1,
                session_refs=(self.ref,),
            )

        def close(self):
            trace.append(f"candidate_close:{self.name}")

    candidates = iter(
        (
            CandidateSession("first", fallback=True, cost=0.10),
            CandidateSession("second", fallback=False, cost=0.25),
        )
    )

    def open_candidate(model):
        trace.append(f"candidate_open:{model}")
        return next(candidates)

    fallback = FallbackHarnessSession(open_candidate, models=("first", "second"))

    class SessionPorts(Ports):
        def open_session(self, *, harness, repo_path, model_id):
            self.trace.append("open_fallback_session")
            return fallback

    ports = SessionPorts(trace, sink=_TraceSink(trace))

    out = agent_turn(
        {**state(tmp_path), "operation_id": "op-fallback"},
        {**NORMALIZED, "session_scope": "phase"},
        {"turn_context": typed(FakeRunner(trace=trace), ports)},
    )

    # Two things 0.6.11 could not report, both visible in the frozen contract:
    # the first candidate was opened, paid for and abandoned but only
    # `session-second` was retained, and two submissions were admitted and
    # charged as one attempt whose total arrived after both had been paid.
    assert CONTRACT["fallback_turn"]["retained_session_ids"] == ["session-second"]
    assert CONTRACT["fallback_turn"]["paid_attempts"] == [
        {"operation_id": "op-fallback", "paid_attempt_ordinal": 1, "provider_cost_usd": 0.35}
    ]

    assert out["turn_result"] == in_0_7(
        CONTRACT["fallback_turn"]["result"],
        [
            {"harness": "opencode", "locator": "session-first", "scope": "process"},
            {"harness": "opencode", "locator": "session-second", "scope": "process"},
        ],
    )
    assert [ref["locator"] for ref in out["turn_result"]["session_refs"]] == [
        "session-first",
        "session-second",
    ], "the order candidates were tried in is the record of what happened"

    assert ports.costs == [
        {"operation_id": "op-fallback", "paid_attempt_ordinal": 1, "provider_cost_usd": 0.10},
        {"operation_id": "op-fallback", "paid_attempt_ordinal": 2, "provider_cost_usd": 0.25},
    ]
    assert trace == [
        "open_fallback_session",
        "before_turn",
        "candidate_open:first",
        "cost_gate:op-fallback:1",
        "candidate_turn:first",
        "on_cost:op-fallback:1:0.1",
        "candidate_snapshot:first",
        "candidate_close:first",
        "candidate_open:second",
        "cost_gate:op-fallback:2",
        "candidate_turn:second",
        "on_cost:op-fallback:2:0.25",
        "candidate_snapshot:second",
        "after_turn:guard",
        "flush",
        "candidate_snapshot:second",
        "candidate_close:second",
        "on_result:completed",
    ]


# -- progress ----------------------------------------------------------------

def test_progress_is_projected_and_published(tmp_path):
    """The projection is agent-core's, not a product's: the consumer that got
    the sanitisation subtly wrong would publish a prompt."""
    from agent_core.harness.registry import TurnProgress

    published: list = []

    class Sink:
        def publish(self, event):
            published.append(event)

        def flush(self, *, timeout_seconds=5):
            return ProgressFlushResult(delivered_events=len(published))

        def record_failure(self, *, stage):
            pass

    trace: list = []
    runner = FakeRunner(progress=[TurnProgress(kind="text", message="raw secret")])

    agent_turn(
        state(tmp_path),
        NORMALIZED,
        {"turn_context": typed(runner, Ports(trace, sink=Sink()))},
    )

    assert [e.summary for e in published] == ["Agent update"]
    assert all("secret" not in e.summary for e in published)


def test_a_broken_sink_cannot_fail_the_turn(tmp_path):
    """The callback is the outer no-throw boundary; a harness calls it inline."""
    from agent_core.harness.registry import TurnProgress

    class Broken:
        def publish(self, event):
            raise RuntimeError("the queue is gone")

        def flush(self, *, timeout_seconds=5):
            raise RuntimeError("the queue is gone")

        def record_failure(self, *, stage):
            self.failed = stage

    trace: list = []
    runner = FakeRunner(progress=[TurnProgress(kind="text", message="hello")])

    out = agent_turn(
        state(tmp_path),
        NORMALIZED,
        {"turn_context": typed(runner, Ports(trace, sink=Broken()))},
    )

    assert out["turn_status"] == "completed"


def test_a_flush_failure_becomes_a_diagnostic_not_a_failed_turn(tmp_path):
    class Failing:
        def publish(self, event):
            pass

        def flush(self, *, timeout_seconds=5):
            return ProgressFlushResult(failures={"delivery": 2}, dropped_count=1)

        def record_failure(self, *, stage):
            pass

    trace: list = []
    out = agent_turn(
        state(tmp_path),
        NORMALIZED,
        {"turn_context": typed(FakeRunner(), Ports(trace, sink=Failing()))},
    )

    assert out["turn_status"] == "completed"
    assert out["turn_result"]["diagnostics"]["progress_flush_failed"] is True
    assert out["turn_result"]["diagnostics"]["progress_dropped"] == 1


def test_no_sink_is_not_reported_as_a_progress_fault(tmp_path):
    trace: list = []
    out = agent_turn(
        state(tmp_path), NORMALIZED, {"turn_context": typed(FakeRunner(), Ports(trace))}
    )

    assert "progress_flush_failed" not in out["turn_result"]["diagnostics"]


def test_no_lifecycle_callback_key_survives_in_the_node():
    """A source scan, because a leftover `context.get("on_result")` would keep
    working for whoever still passes it and silently do nothing for everyone
    else — the two-implementations problem this task exists to end."""
    source = Path(
        __import__("agent_core.workflow.nodes", fromlist=["__file__"]).__file__
    ).read_text(encoding="utf-8")

    for key in (
        "before_turn",
        "after_turn",
        "on_cost",
        "cost_gate",
        "on_result",
        "open_session",
        "progress_sink",
        "recorder",
    ):
        assert f'"{key}"' not in source, f"the callback-map key {key!r} is still read"


# -- parse json, message, confined prompt files (T7) -------------------------


def test_parse_json_uses_extract_json_object_and_projects_payload(tmp_path):
    """json.loads would reject prose around the object; extract_json_object
    keeps the last object, which is what a planner/keyword turn returns."""
    runner = FakeRunner(FakeResult(result='draft {"n": 1}\nfinal {"n": 2}'))

    out = agent_turn(
        state(tmp_path),
        {**NORMALIZED, "parse": "json"},
        {"turn_context": typed(runner, Ports([]))},
    )

    assert out["turn_status"] == "completed"
    assert out["payload"] == {"n": 2}


def test_parse_json_retries_via_existing_attempts(tmp_path):
    """An unusable answer is another attempt, not a hard fail on the first."""
    runner = FakeRunner(
        [FakeResult(result="not json"), FakeResult(result='{"pages": []}')]
    )

    out = agent_turn(
        state(tmp_path),
        {**NORMALIZED, "parse": "json", "attempts": 2},
        {"turn_context": typed(runner, Ports([]))},
    )

    assert len(runner.calls) == 2
    assert out["turn_status"] == "completed"
    assert out["payload"] == {"pages": []}


def test_an_in_memory_message_is_sent_instead_of_a_prompt_file(tmp_path):
    runner = FakeRunner()

    agent_turn(
        {"repo_path": str(tmp_path), "message": "plan this wiki"},
        NORMALIZED,
        {"turn_context": typed(runner, Ports([]))},
    )

    assert runner.calls[0]["message"] == "plan this wiki"
    assert "prompt_file" not in runner.calls[0]


def test_delivery_title_and_pure_are_forwarded_from_config(tmp_path):
    runner = FakeRunner()

    agent_turn(
        {"repo_path": str(tmp_path), "message": "plan"},
        {**NORMALIZED, "delivery": "stdin", "title": "wiki_plan", "pure": False},
        {"turn_context": typed(runner, Ports([]))},
    )

    assert runner.calls[0]["delivery"] == "stdin"
    assert runner.calls[0]["title"] == "wiki_plan"
    assert runner.calls[0]["pure"] is False


def test_a_prompt_file_under_the_wiki_tree_is_refused_before_spawn(tmp_path):
    """Planner prompts must not live in repo_path/docs/spec — that is the wiki."""
    repo = tmp_path / "repo"
    wiki = repo / "docs" / "spec"
    wiki.mkdir(parents=True)
    prompt = wiki / "plan.md"
    prompt.write_text("plan the service")
    runner = FakeRunner()

    with pytest.raises(ValueError, match="docs/spec"):
        agent_turn(
            {"prompt_file": str(prompt), "repo_path": str(repo)},
            NORMALIZED,
            {
                "turn_context": typed(runner, Ports([])),
                "turn_dir": str(tmp_path / "turns"),
            },
        )

    assert runner.calls == []


def test_a_prompt_file_outside_turn_dir_is_refused_before_spawn(tmp_path):
    turn_dir = tmp_path / "turns"
    turn_dir.mkdir()
    prompt = tmp_path / "elsewhere.md"
    prompt.write_text("plan")
    runner = FakeRunner()

    with pytest.raises(ValueError, match="turn_dir"):
        agent_turn(
            {"prompt_file": str(prompt), "repo_path": str(tmp_path / "repo")},
            NORMALIZED,
            {"turn_context": typed(runner, Ports([])), "turn_dir": str(turn_dir)},
        )

    assert runner.calls == []


def test_a_prompt_file_under_turn_dir_is_accepted(tmp_path):
    turn_dir = tmp_path / "turns"
    turn_dir.mkdir()
    prompt = turn_dir / "plan.md"
    prompt.write_text("plan")
    repo = tmp_path / "repo"
    (repo / "docs" / "spec").mkdir(parents=True)
    runner = FakeRunner()

    out = agent_turn(
        {"prompt_file": str(prompt), "repo_path": str(repo)},
        NORMALIZED,
        {"turn_context": typed(runner, Ports([])), "turn_dir": str(turn_dir)},
    )

    assert out["turn_status"] == "completed"
    assert runner.calls[0]["prompt_file"] == prompt.resolve()
    assert "message" not in runner.calls[0]


def test_legacy_mode_still_runs_without_turn_dir(tmp_path):
    """cr_plugin's path: no confinement, no parse, prompt_file as given."""
    runner = FakeRunner()
    prompt = tmp_path / "p.md"
    prompt.write_text("review")

    out = agent_turn(
        {"prompt_file": str(prompt), "repo_path": str(tmp_path)},
        {},
        {"runner": runner},
    )

    assert out["turn_status"] == "completed"
    assert runner.calls[0]["prompt_file"] == prompt
