"""What may be said about a session that has already finished.

The rows behind these numbers hold prompts, reasoning and file contents. The
contract exists to make it structurally impossible to quote any of it: fixed
enums, counts, durations, and tool names from a closed vocabulary. The other
half of it is about limits — every one is checked before the thing it protects
is loaded, and a report that could not answer for every session withholds the
one number someone would put in a budget column.
"""

from __future__ import annotations

import pytest

from agent_core.harness.diagnostics import (
    AvailableSessionDiagnostics,
    DiagnosticsLimitExceeded,
    DiagnosticsLimits,
    DiagnosticsReasonCode,
    DiagnosticsStatus,
    ModelUsage,
    SessionDiagnosticsReport,
    TokenUsage,
    UnavailableSessionDiagnostics,
    UnsupportedSessionDiagnostics,
    build_report,
    create_configured_diagnostics_provider,
    diagnose_sessions,
    register_diagnostics_provider,
    unregister_diagnostics_provider,
)
from agent_core.harness.registry import HarnessSpec
from agent_core.harness.sessions import AgentSessionRef, SessionLocatorScope


def ref(locator="ses_1", *, durable=True, harness="opencode"):
    scope = SessionLocatorScope.DURABLE if durable else SessionLocatorScope.PROCESS
    return AgentSessionRef(harness, locator, scope)


def available(session, model="m1", total=10, **over):
    usage = TokenUsage(input_tokens=total, total_tokens=total)
    fields = dict(
        session=session,
        usage=usage,
        usage_by_model=(ModelUsage(model=model, usage=usage),),
    )
    fields.update(over)
    return AvailableSessionDiagnostics(**fields)


class Provider:
    def __init__(self, items):
        self._items = {item.session: item for item in items}
        self.asked: list = []

    def diagnose_sessions(self, refs, *, limits):
        self.asked.append(tuple(refs))
        return build_report([self._items[r] for r in refs])


def test_diagnostics_provider_is_selected_by_neutral_harness_spec():
    observed = []

    def factory(spec, options):
        observed.append((spec.name, dict(spec.options), dict(options)))
        return Provider(())

    register_diagnostics_provider("scripted", factory)
    try:
        provider = create_configured_diagnostics_provider(
            HarnessSpec(name="scripted", options={"region": "test"}),
            database_path="/tmp/provider.db",
        )
    finally:
        unregister_diagnostics_provider("scripted")

    assert isinstance(provider, Provider)
    assert observed == [
        ("scripted", {"region": "test"}, {"database_path": "/tmp/provider.db"})
    ]


def test_missing_diagnostics_capability_is_explicitly_none():
    assert (
        create_configured_diagnostics_provider(HarnessSpec(name="no-diagnostics"))
        is None
    )


# -- limits ----------------------------------------------------------------

def test_limits_may_be_lowered_but_never_raised():
    """The hard maxima are the defaults, so the safe call is the default call."""
    DiagnosticsLimits(max_sessions=4)

    with pytest.raises(ValueError, match="never raised"):
        DiagnosticsLimits(max_sessions=64)


def test_a_request_over_the_limit_is_an_error_not_a_report():
    """A session too large to summarize is a fact about the session and belongs
    in the report; a request for 400 of them is a bug in the caller, and
    answering it partially would hide that."""
    refs = [ref(f"ses_{n}") for n in range(5)]

    with pytest.raises(DiagnosticsLimitExceeded):
        diagnose_sessions(refs, providers={}, limits=DiagnosticsLimits(max_sessions=4))


@pytest.mark.parametrize(
    "field", ["max_sessions", "max_parts", "max_steps", "max_raw_row_bytes"]
)
def test_a_limit_below_one_is_refused(field):
    with pytest.raises(ValueError, match="at least 1"):
        DiagnosticsLimits(**{field: 0})


# -- what cannot be answered ------------------------------------------------

def test_a_process_scoped_locator_is_unsupported_not_missing():
    """It only ever meant something inside a process that has since exited.
    Reporting it as not-found reads like deleted data."""
    report = diagnose_sessions([ref(durable=False)], providers={})

    item = report.items[0]
    assert isinstance(item, UnsupportedSessionDiagnostics)
    assert item.reason_code is DiagnosticsReasonCode.PROCESS_SCOPED_LOCATOR


def test_an_unregistered_harness_is_unsupported():
    """A ref persisted months ago must still be readable after its harness is
    unregistered; the answer is an item, not a failure to load the row."""
    report = diagnose_sessions([ref(harness="retired")], providers={})

    assert report.items[0].reason_code is DiagnosticsReasonCode.HARNESS_UNSUPPORTED


def test_support_is_resolved_when_diagnostics_is_asked_for(): 
    """Not when the ref was built -- that is the whole reason `AgentSessionRef`
    validates syntax only."""
    session = ref()
    provider = Provider([available(session)])

    assert diagnose_sessions([session], providers={"opencode": provider}).complete
    assert diagnose_sessions([session], providers={}).items[0].status is (
        DiagnosticsStatus.UNSUPPORTED
    )


# -- aggregation -----------------------------------------------------------

def test_a_report_that_answered_everything_has_a_total():
    first, second = ref("ses_1"), ref("ses_2")
    provider = Provider([available(first, total=10), available(second, total=5)])

    report = diagnose_sessions([first, second], providers={"opencode": provider})

    assert report.total_usage == TokenUsage(input_tokens=15, total_tokens=15)
    assert report.complete


def test_a_mixed_report_has_no_total_but_keeps_every_item():
    """A total over the subset that resolved looks authoritative and is always
    an undercount. Nothing is hidden: the per-item numbers stay."""
    first, second = ref("ses_1"), ref(durable=False)
    provider = Provider([available(first, total=10)])

    report = diagnose_sessions([first, second], providers={"opencode": provider})

    assert report.total_usage is None
    assert report.usage_by_model == ()
    assert report.items[0].usage.total_tokens == 10, "per-item data was withheld too"


def test_usage_by_model_is_summed_and_sorted():
    """A report read beside yesterday's should differ only where the numbers
    differ."""
    first, second, third = ref("ses_1"), ref("ses_2"), ref("ses_3")
    provider = Provider(
        [
            available(first, model="zeta", total=1),
            available(second, model="alpha", total=2),
            available(third, model="zeta", total=3),
        ]
    )

    report = diagnose_sessions([first, second, third], providers={"opencode": provider})

    assert [entry.model for entry in report.usage_by_model] == ["alpha", "zeta"]
    assert report.usage_by_model[1].usage.total_tokens == 4


def test_the_order_refs_were_given_in_is_preserved_across_harnesses():
    """Order is the record of what was tried, even though the work is grouped
    by harness underneath."""
    a, b, c = ref("ses_a"), ref("ses_b", harness="other"), ref("ses_c")
    providers = {
        "opencode": Provider([available(a), available(c)]),
        "other": Provider([available(b)]),
    }

    report = diagnose_sessions([a, b, c], providers=providers)

    assert [item.session.locator for item in report.items] == ["ses_a", "ses_b", "ses_c"]


def test_truncated_detail_does_not_change_the_totals():
    """The bound was on how much detail comes out, not on what was counted."""
    session = ref()
    provider = Provider([available(session, total=10, truncated=True)])

    report = diagnose_sessions([session], providers={"opencode": provider})

    assert report.truncated is True
    assert report.total_usage == TokenUsage(input_tokens=10, total_tokens=10)


def test_an_empty_report_has_no_total():
    """Nothing asked for is not a zero bill."""
    assert build_report([]) == SessionDiagnosticsReport(items=(), total_usage=None)
