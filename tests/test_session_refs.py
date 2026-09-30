"""Which conversation a turn happened in, when there was more than one.

A provider fallback chain opens a conversation per candidate and reports one
outcome. Until now that outcome carried a single `session_id` — necessarily the
last candidate's — so the earlier conversations, which were opened, paid for and
abandoned, could not be named by anything a product stored. These are the rules
that make the plural record trustworthy: the order is the record, duplicates are
ordinary, and a locator that only means something inside a running process says
so rather than being filed next to one that does not.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from agent_core.harness import (
    AgentSessionRef,
    SessionLocatorScope,
    merge_session_refs,
)
from agent_core.harness.turn_result import AgentTurnResult


# -- the reference ---------------------------------------------------------

def test_a_ref_serializes_to_the_neutral_shape():
    ref = AgentSessionRef("opencode", "ses_01H", SessionLocatorScope.DURABLE)

    assert ref.as_dict() == {
        "harness": "opencode",
        "locator": "ses_01H",
        "scope": "durable",
    }
    assert json.loads(json.dumps(ref.as_dict())) == ref.as_dict()


def test_a_ref_round_trips_through_json():
    """It gets checkpointed, so it has to come back as the type it went in as —
    a scope that returns as a bare string compares unequal to every scope in
    the code, and the mismatch surfaces nowhere near the checkpoint."""
    ref = AgentSessionRef("opencode", "abc", SessionLocatorScope.PROCESS)

    assert AgentSessionRef.from_dict(json.loads(json.dumps(ref.as_dict()))) == ref


def test_a_scope_given_as_a_string_is_coerced():
    assert AgentSessionRef("opencode", "abc", "process").scope is (
        SessionLocatorScope.PROCESS
    )


def test_the_scope_says_whether_a_later_process_can_use_the_locator():
    """The distinction this type exists for: a diagnostic tool asked to explain
    yesterday's run must not be handed a locator that only existed in
    yesterday's memory."""
    assert AgentSessionRef("opencode", "a").durable is True
    assert AgentSessionRef("opencode", "a", SessionLocatorScope.PROCESS).durable is False


@pytest.mark.parametrize(
    "harness, locator",
    [
        ("OpenCode", "abc"),        # a class name, not a neutral name
        ("open code", "abc"),
        ("", "abc"),
        ("opencode", ""),
        ("opencode", "has space"),  # an identifier, never prose
        ("opencode", "line\nbreak"),
        ("opencode", "nul\x00byte"),
    ],
)
def test_a_ref_validates_its_syntax(harness, locator):
    with pytest.raises(ValueError):
        AgentSessionRef(harness, locator)


def test_a_ref_is_not_checked_against_the_live_registry():
    """These are read back long after the process that made them. A durable
    record that cannot be constructed because a harness has since been
    unregistered is a record lost exactly when it is needed."""
    assert AgentSessionRef("neverregistered", "abc").harness == "neverregistered"


# -- merging ---------------------------------------------------------------

def test_refs_keep_the_order_they_were_used_in():
    first = AgentSessionRef("opencode", "first")
    second = AgentSessionRef("opencode", "second")

    assert merge_session_refs([first], [second]) == (first, second)
    assert merge_session_refs([second], [first]) == (second, first)


def test_repeating_a_ref_is_ordinary_and_collapses():
    """A retry and an in-session recovery both happen inside the conversation
    that is already listed."""
    ref = AgentSessionRef("opencode", "same")

    assert merge_session_refs([ref], [ref], [ref]) == (ref,)


def test_the_same_locator_in_two_scopes_is_two_refs():
    """They resolve differently, so collapsing them would claim a durable
    locator exists where only a process-scoped one does."""
    durable = AgentSessionRef("opencode", "x")
    ephemeral = AgentSessionRef("opencode", "x", SessionLocatorScope.PROCESS)

    assert merge_session_refs([durable, ephemeral]) == (durable, ephemeral)


def test_merging_accepts_the_serialized_form():
    """State comes back from a checkpoint as plain dicts."""
    ref = AgentSessionRef("opencode", "x")

    assert merge_session_refs([ref.as_dict()]) == (ref,)


# -- the 0.7 shape ---------------------------------------------------------

def test_the_singular_session_projection_is_gone():
    """0.7 removes it rather than keeping both: two fields disagreeing about
    which conversation produced an answer is worse than one that is plural."""
    assert not hasattr(AgentTurnResult(status="completed"), "session_id")
    assert "session_refs" in AgentTurnResult.__dataclass_fields__


WITHOUT_LANGGRAPH = textwrap.dedent(
    """
    import sys

    class Blocked:
        def find_module(self, name, path=None):
            return self if name == "langgraph" or name.startswith("langgraph.") else None

        def load_module(self, name):
            raise ImportError("langgraph is not installed")

        def find_spec(self, name, path=None, target=None):
            if name == "langgraph" or name.startswith("langgraph."):
                raise ImportError("langgraph is not installed")
            return None

    sys.meta_path.insert(0, Blocked())

    from agent_core.harness import AgentSessionRef, merge_session_refs
    from agent_core.harness.turn_result import AgentTurnResult, normalize_turn_outcome

    assert "langgraph" not in sys.modules
    # The stronger half of the claim: importing the neutral result must not
    # drag in the workflow package, which is what would need the extra.
    assert "agent_core.workflow" not in sys.modules
    print("ok")
    """
)


def test_turn_results_are_usable_without_the_langgraph_extra():
    """The reason the normalized result moved out of `workflow`: a product that
    runs turns directly needs the neutral answer too, and should not have to
    install a graph engine to get it."""
    done = subprocess.run(
        [sys.executable, "-c", WITHOUT_LANGGRAPH],
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
