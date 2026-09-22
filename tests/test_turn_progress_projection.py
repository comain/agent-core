"""Projecting a harness turn update into a publishable progress event.

`TurnProgress.message`/`detail` are documented as trusted-log material: they
may carry model text, reasoning, commands, provider errors, repository content
or credentials. `AgentProgressEvent` is the other side of that boundary — the
thing a product may store and show. This is where the crossing happens, so
these tests are mostly about what does *not* cross.

Two detail policies exist because the callers differ. `summary` keeps today's
generic public messages and is the default; `public_detail` adds a bounded,
redacted synopsis for a product that already treats its progress view as
report-visible.
"""

from __future__ import annotations

import pytest

from agent_core.harness.registry import TurnProgress
from agent_core.runtime.progress import AgentProgressEvent, project_turn_progress


def project(progress, **kw):
    return project_turn_progress(progress, phase=kw.pop("phase", "generate"), **kw)


# -- the boundary ------------------------------------------------------------

def test_the_default_policy_emits_no_detail_at_all():
    """`summary` is the default precisely because it cannot leak: there is no
    path from provider text to the emitted event."""
    event = project(TurnProgress(kind="reasoning", message="the secret is hunter2"))

    assert event.detail is None
    assert "hunter2" not in event.summary


def test_the_raw_message_never_becomes_the_summary():
    """The summary is chosen from a fixed vocabulary, not copied."""
    event = project(TurnProgress(kind="text", message="/Users/me/.ssh/id_rsa contents"))

    assert "/Users/me" not in event.summary
    assert "id_rsa" not in event.summary


def test_public_detail_is_redacted_not_raw():
    event = project(
        TurnProgress(kind="reasoning", message="m", detail="token=abcdef1234567890abcdef"),
        detail_policy="public_detail",
    )

    assert "abcdef1234567890" not in (event.detail or "")


def test_public_detail_refuses_command_like_content():
    event = project(
        TurnProgress(kind="tool", message="m", tool="bash", detail="cat /etc/shadow"),
        detail_policy="public_detail",
    )

    assert "/etc/shadow" not in (event.detail or "")


def test_public_detail_is_bounded():
    event = project(
        TurnProgress(kind="reasoning", message="m", detail="word " * 500),
        detail_policy="public_detail",
    )

    assert len(event.detail or "") <= 180


def test_an_unknown_policy_falls_back_to_the_safe_one():
    """A typo in a workflow's YAML must fail closed, not open."""
    event = project(
        TurnProgress(kind="reasoning", message="m", detail="secret material"),
        detail_policy="detailed",  # not a real policy
    )

    assert event.detail is None


# -- what does cross ---------------------------------------------------------

def test_a_tool_update_carries_its_projected_activity():
    event = project(
        TurnProgress(kind="tool", message="raw", tool="bash", status="running",
                     detail="pytest -q"),
        detail_policy="public_detail",
    )

    assert event.tool == "bash"
    assert event.status == "running"
    assert event.summary == "Ran repository checks"
    assert event.detail == "Test suite"


def test_the_phase_and_session_are_attached():
    """Progress from parallel units must stay separable, and the phase is what
    a person actually reads."""
    event = project(
        TurnProgress(kind="text", message="m"),
        phase="fix_compile", session_id="sess-9", sequence=4,
    )

    assert (event.phase, event.session_id, event.sequence) == ("fix_compile", "sess-9", 4)


def test_an_error_kind_is_preserved_as_a_kind():
    """The sink reserves slots by critical kind, so the kind must survive."""
    assert project(TurnProgress(kind="error", message="boom")).kind == "error"
    assert project(TurnProgress(kind="rate_limit", message="wait")).kind == "rate_limit"


def test_the_event_is_json_safe():
    import dataclasses, json

    event = project(TurnProgress(kind="tool", message="m", tool="read", status="completed"))
    assert json.loads(json.dumps(dataclasses.asdict(event)))["kind"] == "tool"


def test_the_event_is_frozen():
    """It is a record of a moment; a mutated one reports something that did
    not happen."""
    event = project(TurnProgress(kind="text", message="m"))
    with pytest.raises(Exception):
        event.summary = "changed"  # type: ignore[misc]


def test_transport_noise_is_dropped():
    """Provider step boundaries describe the stream protocol, not work. The
    existing publisher already drops them; this projection is shared with it,
    so it must make the same call."""
    assert project(TurnProgress(kind="step", message="step 3")) is None


def test_an_unrecognized_kind_is_dropped_rather_than_guessed():
    """Fail closed. A kind nobody has mapped has no vetted summary, and the
    raw message is exactly what must not be shown."""
    assert project(TurnProgress(kind="something_new", message="secret")) is None


def test_model_fallback_has_a_safe_operator_visible_projection():
    event = project(
        TurnProgress(
            kind="model_fallback",
            message="model-fallback",
            status="rate_limit",
            detail="a/m1 -> b/m2",
        )
    )

    assert event.summary == "Model fallback: a/m1 -> b/m2 (rate_limit)"
    assert event.kind == "model_fallback"
    assert event.status == "running"


def test_selected_model_crosses_translation_and_public_projection():
    from agent_core.harness.opencode import _translate_progress
    event = project(_translate_progress("model-selected: token-pool/gpt-6-astra effort=low"))
    assert event.kind == "model_selected"
    assert event.summary == "Selected model token-pool/gpt-6-astra (effort: low)"


def test_selected_model_does_not_publish_untrusted_prose():
    event = project(TurnProgress(kind="model_selected", message="secret", detail="bad\nsecret", status="secret"))
    assert event.summary == "Model selection updated"
    assert event.detail is None


def test_a_projected_event_is_an_event():
    assert isinstance(project(TurnProgress(kind="text", message="m")), AgentProgressEvent)
