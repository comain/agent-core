"""Tests for SSE delivery and the harness->event bridge."""

from __future__ import annotations

import json

import pytest

from agent_core.runtime import (
    HarnessEventBridge,
    RuntimeProgressPublisher,
    RuntimeStore,
    format_frame,
    stream_task_events,
)
from agent_core.harness import TurnProgress


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "rt.db")
    s.init()
    return s


class FakeClock:
    """Deterministic time so streaming behaviour is testable without waiting."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.sleeps += 1


# -- wire format ---------------------------------------------------------------


def test_frame_format():
    frame = format_frame(event_id=7, event="progress", data={"a": 1})
    assert frame == 'id: 7\nevent: progress\ndata: {"a": 1}\n\n'


def test_multiline_payload_is_split_across_data_lines():
    """A raw newline inside a data field truncates the frame at the newline."""
    frame = format_frame(event_id=1, event="e", data="line one\nline two")
    assert "data: line one\ndata: line two\n\n" in frame
    assert frame.count("data:") == 2


def test_frame_without_id_or_event():
    assert format_frame(event_id=None, event=None, data="x") == "data: x\n\n"


# -- streaming -----------------------------------------------------------------


def _collect(gen, limit=50):
    out = []
    for i, frame in enumerate(gen):
        out.append(frame)
        if i + 1 >= limit:
            break
    return out


def test_stream_emits_existing_events_then_terminates(store):
    store.append_event(task_ref="t", event_type="progress", message="one")
    store.append_event(task_ref="t", event_type="progress", message="two")
    store.append_event(task_ref="t", event_type="task_completed", message="done")

    clock = FakeClock()
    frames = list(stream_task_events(store, task_ref="t", _sleep=clock.sleep, _now=clock.now))

    assert len(frames) == 3
    assert "one" in frames[0] and "two" in frames[1]
    assert "event: task_completed" in frames[2]


def test_stream_resumes_from_after_id(store):
    ids = [store.append_event(task_ref="t", event_type="progress", message=str(i)) for i in range(4)]
    store.append_event(task_ref="t", event_type="task_completed", message="done")

    clock = FakeClock()
    frames = list(stream_task_events(store, task_ref="t", after_id=ids[1], _sleep=clock.sleep, _now=clock.now))
    # Only events after the cursor: messages "2", "3", then the terminal event.
    assert len(frames) == 3
    messages = [json.loads(f.split("data: ", 1)[1].strip())["message"] for f in frames]
    assert messages == ["2", "3", "done"]


def test_frames_carry_the_id_used_for_resumption(store):
    eid = store.append_event(task_ref="t", event_type="task_completed", message="done")
    clock = FakeClock()
    frame = next(iter(stream_task_events(store, task_ref="t", _sleep=clock.sleep, _now=clock.now)))
    assert frame.startswith(f"id: {eid}\n")


def test_stream_is_isolated_per_task(store):
    store.append_event(task_ref="a", event_type="progress", message="for-a")
    store.append_event(task_ref="b", event_type="progress", message="for-b")
    store.append_event(task_ref="a", event_type="task_completed", message="done")
    clock = FakeClock()
    frames = list(stream_task_events(store, task_ref="a", _sleep=clock.sleep, _now=clock.now))
    assert not any("for-b" in f for f in frames)


def test_keepalive_sent_when_idle(store):
    """Idle proxies reap connections; a comment frame keeps them open."""
    clock = FakeClock()
    gen = stream_task_events(
        store, task_ref="t", poll_interval=5.0, keepalive_interval=10.0,
        max_duration=60.0, _sleep=clock.sleep, _now=clock.now,
    )
    frames = _collect(gen, limit=3)
    assert any(f.startswith(": keepalive") for f in frames)


def test_stream_stops_at_max_duration(store):
    clock = FakeClock()
    frames = list(stream_task_events(
        store, task_ref="t", poll_interval=1.0, keepalive_interval=1000.0,
        max_duration=3.0, _sleep=clock.sleep, _now=clock.now,
    ))
    assert frames and "stream_timeout" in frames[-1]


def test_full_batch_is_drained_without_sleeping(store):
    """Otherwise a burst is delivered at one batch per poll interval."""
    for i in range(5):
        store.append_event(task_ref="t", event_type="progress", message=str(i))
    store.append_event(task_ref="t", event_type="task_completed", message="done")

    clock = FakeClock()
    frames = list(stream_task_events(
        store, task_ref="t", batch_size=2, _sleep=clock.sleep, _now=clock.now,
    ))
    assert len(frames) == 6
    assert clock.sleeps == 0


def test_failure_terminates_the_stream(store):
    store.append_event(task_ref="t", event_type="task_failed", message="boom", severity="error")
    clock = FakeClock()
    frames = list(stream_task_events(store, task_ref="t", _sleep=clock.sleep, _now=clock.now))
    assert len(frames) == 1 and "task_failed" in frames[0]


# -- harness bridge ------------------------------------------------------------


def test_bridge_records_tool_level_events(store):
    bridge = HarnessEventBridge(store, task_ref="t", stage="review")
    bridge.handle_event({
        "type": "tool_use",
        "part": {"tool": "bash", "state": {"status": "completed", "title": "run tests"}},
    })
    rows = store.events_since(task_ref="t")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "agent_tool_use"
    assert "tool[bash] completed - run tests" in rows[0]["message"]
    assert rows[0]["stage"] == "review"


def test_bridge_marks_errors_with_severity(store):
    HarnessEventBridge(store, task_ref="t").handle_event(
        {"type": "error", "error": {"data": {"message": "provider exploded"}}}
    )
    row = store.events_since(task_ref="t")[0]
    assert row["severity"] == "error" and "provider exploded" in row["message"]


def test_bridge_skips_events_with_no_progress_value(store):
    """Not every OpenCode event is worth a log line."""
    assert HarnessEventBridge(store, task_ref="t").handle_event({"type": "unknown_thing"}) is None
    assert store.events_since(task_ref="t") == []


def test_bridge_handles_raw_lines(store):
    bridge = HarnessEventBridge(store, task_ref="t")
    bridge.handle_line(json.dumps({"type": "text", "part": {"type": "text", "text": "hello there"}}))
    assert "text: hello there" in store.events_since(task_ref="t")[0]["message"]


def test_bridge_tolerates_malformed_lines(store):
    assert HarnessEventBridge(store, task_ref="t").handle_line("not json at all") is None
    assert store.events_since(task_ref="t") == []


def test_bridge_output_is_immediately_streamable(store):
    """The bridge and the stream must agree on the same log."""
    bridge = HarnessEventBridge(store, task_ref="t")
    bridge.handle_event({"type": "step_start", "part": {}})
    bridge.handle_event({"type": "tool_use", "part": {"tool": "grep", "state": {"status": "started"}}})
    store.append_event(task_ref="t", event_type="task_completed", message="done")

    clock = FakeClock()
    frames = list(stream_task_events(store, task_ref="t", _sleep=clock.sleep, _now=clock.now))
    assert len(frames) == 3
    assert "event: agent_step_start" in frames[0]
    assert "tool[grep] started" in frames[1]


def test_public_progress_publisher_keeps_only_safe_correlated_status(store):
    publisher = RuntimeProgressPublisher(
        store,
        task_ref="t",
        stage="reviewer:security",
        context={"reviewer": "security"},
    )

    event_id = publisher.publish(
        TurnProgress(
            kind="tool",
            message="tool[bash] completed - cat /private/token",
            tool="bash",
            status="completed",
        )
    )

    assert event_id is not None
    row = store.events_since(task_ref="t")[0]
    assert row["event_type"] == "agent_progress"
    assert row["stage"] == "reviewer:security"
    assert row["message"] == "Ran repository checks"
    assert json.loads(row["payload_json"]) == {
        "reviewer": "security",
        "kind": "tool",
        "activity": "repository_checks",
        "status": "completed",
    }
    assert "token" not in row["message"]


@pytest.mark.parametrize(
    ("tool", "expected_activity", "expected_message"),
    [
        ("read", "code_review", "Read project context"),
        ("grep", "code_review", "Searched the repository"),
        ("bash", "repository_checks", "Ran repository checks"),
        ("webfetch", "reference_research", "Consulted a reference"),
        ("apply_patch", "workspace_update", "Updated workspace files"),
    ],
)
def test_public_progress_projects_tools_to_human_activities(
    store, tool, expected_activity, expected_message
):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(
            kind="tool",
            message=f"tool[{tool}] completed - private native detail",
            tool=tool,
            status="completed",
        )
    )

    row = store.events_since(task_ref="t")[0]
    assert row["message"] == expected_message
    assert json.loads(row["payload_json"]) == {
        "kind": "tool",
        "activity": expected_activity,
        "status": "completed",
    }
    assert "private native detail" not in row["message"]


def test_public_progress_keeps_safe_tool_context_without_absolute_paths(store):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(
            kind="tool",
            message="private native detail",
            tool="read",
            status="completed",
            detail=(
                "/opt/app/product/.agent/workspaces/123/"
                "src/review/authentication.py"
            ),
        )
    )

    row = store.events_since(task_ref="t")[0]
    payload = json.loads(row["payload_json"])
    assert row["message"] == "Read project context"
    assert payload["detail"] == "src/review/authentication.py"
    assert "/opt/app" not in row["payload_json"]


@pytest.mark.parametrize(
    ("tool", "detail", "expected"),
    [
        ("bash", "pytest tests/runtime", "Test suite"),
        ("shell", "git diff --stat", "Git inspection"),
        ("exec", "rg RuntimeProgressPublisher src", "Repository search"),
        ("terminal", "make lint", None),
        ("read", "private/generated/report.txt", "report.txt"),
        (
            "read",
            "949bfccf0123456789abcdef01234567/opt/app/worktree/AGENTS.md",
            "AGENTS.md",
        ),
        ("read", "Inspecting the retry lifecycle", "Inspecting the retry lifecycle"),
    ],
)
def test_public_progress_reduces_tool_titles_to_safe_context(
    store, tool, detail, expected
):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(
            kind="tool",
            message="private native detail",
            tool=tool,
            status="completed",
            detail=detail,
        )
    )

    payload = json.loads(store.events_since(task_ref="t")[0]["payload_json"])
    assert payload.get("detail") == expected


@pytest.mark.parametrize(
    ("kind", "expected_message"),
    [
        ("reasoning", "Analyzing the change"),
        ("text", "Agent update"),
    ],
)
def test_public_progress_keeps_bounded_sanitized_agent_summaries(
    store, kind, expected_message
):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(
            kind=kind,
            message="private native detail",
            detail=(
                "Checking retry behavior in /opt/app/private/retry.py with "
                "token=sk-super-secret before reporting the result"
            ),
        )
    )

    row = store.events_since(task_ref="t")[0]
    payload = json.loads(row["payload_json"])
    assert row["message"] == expected_message
    assert payload["detail"] == (
        "Checking retry behavior in [path] with token=[redacted] "
        "before reporting the result"
    )
    assert "sk-super-secret" not in row["payload_json"]


@pytest.mark.parametrize("detail", ["{\"findings\": []}", "```python", "cat /private/token"])
def test_public_progress_drops_structured_or_command_like_agent_summaries(store, detail):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(kind="reasoning", message="private", detail=detail)
    )

    row = store.events_since(task_ref="t")[0]
    assert row["message"] == "Analyzing the change"
    assert "detail" not in json.loads(row["payload_json"])


def test_public_progress_drops_agent_protocol_step_noise(store):
    publisher = RuntimeProgressPublisher(store, task_ref="t")

    assert publisher.publish(
        TurnProgress(kind="step", message="step: started", status="started")
    ) is None
    assert publisher.publish(
        TurnProgress(kind="step", message="step: finished", status="finished")
    ) is None
    assert store.events_since(task_ref="t") == []


@pytest.mark.parametrize(
    ("kind", "message", "activity", "status", "severity"),
    [
        ("connection", "Connecting to the agent runtime", "connection", "running", "info"),
        ("waiting", "Waiting for an agent operation", "waiting", "waiting", "info"),
        ("rate_limit", "Waiting for model capacity", "capacity_wait", "waiting", "warning"),
    ],
)
def test_public_progress_projects_runtime_wait_states(
    store, kind, message, activity, status, severity
):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(kind=kind, message="private provider detail")
    )

    row = store.events_since(task_ref="t")[0]
    assert row["message"] == message
    assert row["severity"] == severity
    assert json.loads(row["payload_json"]) == {
        "kind": kind,
        "activity": activity,
        "status": status,
    }


def test_public_progress_normalizes_unknown_tool_and_status(store):
    RuntimeProgressPublisher(store, task_ref="t").publish(
        TurnProgress(kind="tool", message="private", tool=None, status="provider-specific")
    )

    row = store.events_since(task_ref="t")[0]
    assert row["message"] == "Working through the review"
    assert json.loads(row["payload_json"]) == {
        "kind": "tool",
        "activity": "agent_work",
        "status": "updated",
    }


@pytest.mark.parametrize(
    ("kind", "message"),
    [("text", "Agent update"), ("reasoning", "Analyzing the change")],
)
def test_public_progress_does_not_derive_a_summary_from_the_trusted_log_message(
    store, kind, message
):
    publisher = RuntimeProgressPublisher(store, task_ref="t")

    assert publisher.publish(TurnProgress(kind=kind, message=f"{kind}: private source"))
    row = store.events_since(task_ref="t")[0]
    assert row["message"] == message
    assert "detail" not in json.loads(row["payload_json"])
    assert "private source" not in row["payload_json"]


def test_public_progress_publisher_redacts_raw_errors_and_unknown_updates(store):
    publisher = RuntimeProgressPublisher(store, task_ref="t")

    publisher.publish(TurnProgress(kind="error", message="error: token sk-secret failed"))
    assert publisher.publish(TurnProgress(kind="native_private", message="secret")) is None

    row = store.events_since(task_ref="t")[0]
    assert row["message"] == "Agent needs attention"
    assert row["severity"] == "error"
    assert "secret" not in row["message"]


def test_public_progress_publisher_is_a_callback(store):
    publisher = RuntimeProgressPublisher(store, task_ref="t", stage="judge")

    publisher(TurnProgress(kind="tool", message="tool[read] completed", tool="read", status="completed"))

    row = store.events_since(task_ref="t")[0]
    assert row["message"] == "Read project context"
    assert row["stage"] == "judge"


def test_public_progress_publisher_sanitizes_stage_and_context(store):
    publisher = RuntimeProgressPublisher(
        store,
        task_ref="t",
        stage="reviewer:security\nprivate",
        context={
            "reviewer": "security",
            "access_token": "sk-secret",
            "note": "private source text",
            "attempt": 2,
        },
    )

    publisher(TurnProgress(kind="tool", message="tool[read] completed", tool="read", status="completed"))

    row = store.events_since(task_ref="t")[0]
    assert row["stage"] is None
    assert json.loads(row["payload_json"]) == {
        "reviewer": "security",
        "attempt": 2,
        "kind": "tool",
        "activity": "code_review",
        "status": "completed",
    }
