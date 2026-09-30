"""One projection of agent activity into something safe to show a person.

`runtime/sse.py` grew this for its event stream. Workflow nodes now need the
same thing for phase progress, and a second implementation of a *redaction*
boundary is the worst kind to have two of: the copies drift, and the drift is
invisible until something private is already on a screen.

These tests pin the behaviour that exists today, so the extraction cannot
quietly change it.
"""

from __future__ import annotations

import pytest

import pathlib

from agent_core.runtime.progress import (
    ProgressEnvelope,
    group_progress_events,
    PublicActivity,
    project_public_tool_activity,
    sanitize_public_progress_detail,
)


# -- what must never reach a screen ----------------------------------------

@pytest.mark.parametrize("raw", [
    '{"key": "value"}',
    "[1, 2, 3]",
    "```python",
    "$ export TOKEN=abc",
])
def test_structured_and_command_output_is_refused_not_trimmed(raw):
    """Refused whole: a partially-exposed secret is still exposed."""
    assert sanitize_public_progress_detail(raw) is None


@pytest.mark.parametrize("raw", [
    "cat /etc/passwd", "git push origin main", "rg secret", "curl https://x",
    "python manage.py", "pytest -k auth", "mvn deploy",
])
def test_command_like_lines_are_refused(raw):
    assert sanitize_public_progress_detail(raw) is None


def test_a_url_becomes_a_placeholder():
    assert "[link]" in (sanitize_public_progress_detail("see https://example.com/x") or "")


def test_a_secret_assignment_is_redacted():
    out = sanitize_public_progress_detail("token=abcdef1234567890abcdef") or ""
    assert "abcdef1234567890" not in out
    assert "[redacted]" in out


def test_an_absolute_path_is_replaced():
    out = sanitize_public_progress_detail("failed at /Users/someone/secret/file.py") or ""
    assert "/Users/someone" not in out


def test_a_synopsis_is_bounded():
    assert len(sanitize_public_progress_detail("word " * 200) or "") <= 180


def test_empty_input_yields_nothing():
    assert sanitize_public_progress_detail(None) is None
    assert sanitize_public_progress_detail("   ") is None


# -- the activity projection ------------------------------------------------

def test_a_tool_update_becomes_a_stable_activity():
    activity = project_public_tool_activity("bash", "running", "pytest -q")

    assert isinstance(activity, PublicActivity)
    assert activity.code
    assert activity.message
    assert activity.status


def test_an_unknown_tool_still_projects():
    """A provider adding a tool must not produce an empty or raw activity."""
    activity = project_public_tool_activity("some_new_tool", "weird_status", "x")

    assert activity.code == "agent_work"
    assert activity.status == "updated"


def test_a_test_command_is_summarised_not_echoed():
    assert project_public_tool_activity("bash", "running", "pytest -q tests/").detail == "Test suite"


def test_missing_tool_and_status_are_tolerated():
    activity = project_public_tool_activity(None, None, None)
    assert activity.code and activity.status


# -- the envelope -----------------------------------------------------------
#
# The shape here is not invented: it is what `sse.py`'s publisher already
# writes and what `group_progress_events` already reads. A workflow node needs
# to emit the same thing without importing an SSE publisher, so the envelope
# is that payload, named.


def test_the_envelope_carries_the_fields_the_reader_reads():
    env = ProgressEnvelope(
        kind="tool", message="Ran repository checks",
        activity="repository_checks", status="running", detail="Test suite",
    )
    payload = env.payload()

    assert payload["kind"] == "tool"
    assert payload["activity"] == "repository_checks"
    assert payload["status"] == "running"
    assert payload["detail"] == "Test suite"


def test_the_envelope_is_json_safe():
    """A payload is stored as JSON; a non-serializable context value would
    fail at write time, in the publisher, far from whoever put it there."""
    import json

    env = ProgressEnvelope(
        kind="phase", message="Planning tests",
        context={"session_ref": "unit-7", "attempt": 2, "at": object()},
    )
    restored = json.loads(json.dumps(env.payload()))
    assert restored["session_ref"] == "unit-7"
    assert restored["attempt"] == 2
    assert isinstance(restored["at"], str)


def test_the_envelope_redacts_its_own_detail():
    """The envelope is the boundary; a caller should not have to remember."""
    env = ProgressEnvelope(
        kind="phase", message="ran", detail="token=abcdef1234567890abcdef",
    )
    assert "abcdef1234567890" not in (env.payload().get("detail") or "")


def test_an_absent_detail_is_omitted_not_null():
    """The reader does `payload.get("detail") or ""`; an explicit null would
    round-trip fine, but the stored shape should match what exists today."""
    assert "detail" not in ProgressEnvelope(kind="phase", message="x").payload()


def test_a_refused_detail_is_omitted_rather_than_partially_shown():
    env = ProgressEnvelope(kind="phase", message="x", detail="cat /etc/passwd")
    assert "detail" not in env.payload()


def test_context_cannot_overwrite_the_projection():
    """Context is caller-supplied. If it could set `activity` or `status`, a
    caller could put an unprojected provider string on a person's screen."""
    env = ProgressEnvelope(
        kind="tool", message="m", activity="repository_checks", status="running",
        context={"activity": "raw_provider_tool", "status": "whatever"},
    )
    payload = env.payload()
    assert payload["activity"] == "repository_checks"
    assert payload["status"] == "running"


def test_from_tool_projects_a_provider_update():
    env = ProgressEnvelope.from_tool("bash", "running", "pytest -q tests/")

    assert env.kind == "tool"
    assert env.payload()["detail"] == "Test suite"
    assert env.message == "Ran repository checks"


def test_an_envelope_survives_the_round_trip_to_the_reader():
    """The anti-drift test: what the writer emits is what the reader groups.

    Writer and reader living in one module is the point of the extraction; a
    field renamed on one side must break here rather than in a UI.
    """
    env = ProgressEnvelope.from_tool("bash", "running", "pytest -q")
    events = [{
        "id": 1, "event_type": "agent_progress", "severity": env.severity,
        "stage": "generate", "message": env.message,
        "payload": env.payload(), "created_at": "2026-01-01T00:00:00Z",
    }]

    [tab] = group_progress_events(events)
    [entry] = tab["events"]
    assert entry["activity"] == env.payload()["activity"]
    assert entry["status"] == env.payload()["status"]
    assert entry["detail"] == env.payload()["detail"]
    assert entry["message"] == env.message


def test_an_error_envelope_is_a_warning_or_worse():
    """Severity is what a product filters on to page someone."""
    assert ProgressEnvelope(kind="error", message="x").severity == "error"


def test_sse_uses_the_extracted_projection():
    """One redaction boundary, not two that drift."""
    from agent_core.runtime import sse

    assert sse.sanitize_public_progress_detail is sanitize_public_progress_detail
    assert sse.project_public_tool_activity is project_public_tool_activity


def test_progress_does_not_import_sse():
    """The extraction inverts the dependency: a workflow node emitting phase
    progress must not drag in an event-stream transport."""
    import ast

    source = pathlib.Path("src/agent_core/runtime/progress.py").read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "agent_core.runtime.sse" not in imported
    assert not any(module.split(".")[-1] == "sse" for module in imported)
