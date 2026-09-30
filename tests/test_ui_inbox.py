"""Tests for approval inbox rendering.

Escaping gets the most attention: gate prompts carry model-generated text and
reviewer comments, and this system exists to review code, so markup in a prompt
is expected input rather than an attack edge case.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_core.runtime import RuntimeStore
from agent_core.ui import (
    gate_to_dict,
    humanize_wait,
    is_stale,
    render_gate,
    render_inbox,
    render_prompt,
)

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "rt.db")
    s.init()
    return s


def _gate(store, **kw):
    kw.setdefault("task_ref", "task-1")
    kw.setdefault("node", "design_review")
    kw.setdefault("kind", "input")
    kw.setdefault("prompt", {"design": "an SSE endpoint"})
    return store.open_gate(**kw)


# -- waiting time --------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=5), "5s"),
        (timedelta(minutes=3), "3m"),
        (timedelta(hours=2, minutes=30), "2h 30m"),
        (timedelta(days=3, hours=4), "3d 4h"),
    ],
)
def test_humanize_wait(delta, expected):
    assert humanize_wait((NOW - delta).isoformat(), now=NOW) == expected


def test_humanize_wait_handles_missing_and_malformed():
    assert humanize_wait(None) == "unknown"
    assert humanize_wait("not a date") == "unknown"


def test_humanize_wait_tolerates_clock_skew():
    assert humanize_wait((NOW + timedelta(minutes=5)).isoformat(), now=NOW) == "just now"


def test_naive_timestamps_are_treated_as_utc():
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert humanize_wait(naive, now=NOW) == "1h 0m"


def test_staleness_threshold():
    assert is_stale((NOW - timedelta(hours=25)).isoformat(), now=NOW) is True
    assert is_stale((NOW - timedelta(hours=2)).isoformat(), now=NOW) is False
    assert is_stale(None) is False


# -- escaping ------------------------------------------------------------------


def test_artifact_markdown_is_escaped_preformatted(store):
    gate = _gate(store, prompt={"artifact_markdown": "<script>alert(1)</script>\n# Title"})
    out = render_gate(gate, now=NOW)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert 'class="artifact"' in out
    assert "<pre" in out


def test_prompt_content_is_escaped(store):
    """A design proposal containing markup is ordinary input here."""
    gate = _gate(store, prompt={"design": "<script>alert('xss')</script>"})
    out = render_gate(gate, now=NOW)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_every_interpolated_field_is_escaped(store):
    gate = _gate(
        store,
        task_ref='task"><img src=x onerror=alert(1)>',
        node="<b>node</b>",
        prompt={"<k>": "<v>"},
    )
    out = render_gate(gate, now=NOW)
    assert "<img" not in out and "<b>node</b>" not in out
    assert "&lt;b&gt;node&lt;/b&gt;" in out


def test_action_url_is_escaped(store):
    gate = _gate(store)
    out = render_gate(gate, action_url='"><script>bad()</script>', now=NOW)
    assert "<script>" not in out


def test_nested_prompt_values_are_escaped(store):
    gate = _gate(store, prompt={"files": ["<script>a</script>", {"x": "<b>"}]})
    out = render_gate(gate, now=NOW)
    assert "<script>" not in out and "<b>" not in out


def test_prompt_renders_without_details():
    assert "no details supplied" in render_prompt({})


def test_bridge_metadata_is_not_repeated_in_the_body():
    """gate_id/kind/node already appear in the header."""
    rendered = render_prompt({"gate_id": "g1", "kind": "input", "node": "n", "design": "real content"})
    assert "real content" in rendered
    assert "g1" not in rendered


# -- gate rendering ------------------------------------------------------------


def test_input_gate_offers_a_comments_field(store):
    """design-review wants comments, not a boolean."""
    out = render_gate(_gate(store, kind="input"), now=NOW)
    assert 'name="comments"' in out
    assert 'value="approve"' in out and 'value="reject"' in out


def test_approve_gate_has_no_comments_field(store):
    out = render_gate(_gate(store, kind="approve"), now=NOW)
    assert 'name="comments"' not in out
    assert 'value="approve"' in out


def test_gate_id_is_submitted_with_the_form(store):
    gate = _gate(store)
    assert f'value="{gate.gate_id}"' in render_gate(gate, now=NOW)


def test_long_wait_is_visually_flagged(store):
    """With no expiry, the queue is the only thing surfacing a stalled review."""
    gate = _gate(store)
    old = gate.__class__(**{**gate.__dict__, "requested_at": (NOW - timedelta(days=2)).isoformat()})
    assert "waited long" in render_gate(old, now=NOW)
    assert "waited long" not in render_gate(gate, now=NOW)


# -- inbox page ----------------------------------------------------------------


def test_inbox_lists_gates_across_tasks(store):
    _gate(store, task_ref="task-a", node="design_review")
    _gate(store, task_ref="task-b", node="ship", kind="approve")
    out = render_inbox(store.pending_gates(), now=NOW)
    assert "task-a" in out and "task-b" in out
    assert "design_review" in out and "ship" in out


def test_empty_inbox_says_so():
    out = render_inbox([], now=NOW)
    assert "Nothing waiting" in out


def test_inbox_shows_a_count(store):
    _gate(store)
    _gate(store, task_ref="task-2")
    assert "(2)" in render_inbox(store.pending_gates(), now=NOW)


def test_inbox_is_a_complete_document(store):
    _gate(store)
    out = render_inbox(store.pending_gates(), now=NOW)
    assert out.startswith("<!doctype html>")
    assert "<meta name=\"viewport\"" in out
    assert out.rstrip().endswith("</html>")


def test_action_url_can_be_supplied_per_gate(store):
    _gate(store)
    out = render_inbox(
        store.pending_gates(),
        action_url_for=lambda g: f"/custom/{g.gate_id}",
        now=NOW,
    )
    assert "/custom/gate-" in out


def test_answered_gates_do_not_appear(store):
    gate = _gate(store)
    _gate(store, task_ref="task-2")
    store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, answered_by="x")
    out = render_inbox(store.pending_gates(), now=NOW)
    assert "task-2" in out and gate.gate_id not in out


# -- JSON view -----------------------------------------------------------------


def test_gate_to_dict_is_json_serialisable(store):
    import json
    payload = gate_to_dict(_gate(store), now=NOW)
    json.dumps(payload)
    assert payload["kind"] == "input"
    assert payload["node"] == "design_review"
    assert "waited" in payload and "stale" in payload


def test_gate_to_dict_does_not_escape(store):
    """JSON consumers must get the real value; escaping is a rendering concern."""
    payload = gate_to_dict(_gate(store, prompt={"design": "<b>x</b>"}), now=NOW)
    assert payload["prompt"]["design"] == "<b>x</b>"
