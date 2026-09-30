"""Reading OpenCode's own record of a finished session.

The fixtures below are built to the schema OpenCode actually uses — `part` and
`message` tables keyed by `session_id`, with the payload as JSON in `data`.
That schema was read off a real database rather than assumed, which is also why
the drift test matters: the two OpenCode databases on a developer's machine
right now already disagree about table names between versions.

The other theme is what must *not* come out. The rows here deliberately contain
a prompt, a secret and file contents, and every test that produces a report
asserts none of it survives the projection.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_core.harness.diagnostics import (
    AvailableSessionDiagnostics,
    DiagnosticSignalCategory,
    DiagnosticsLimitExceeded,
    DiagnosticsLimits,
    DiagnosticsReasonCode,
    SessionStepKind,
)
from agent_core.harness.opencode_diagnostics import OpenCodeSessionDiagnostics
from agent_core.harness.sessions import AgentSessionRef, SessionLocatorScope

SECRET = "sk-live-do-not-leak"
PROMPT = "You are an expert Java developer. The password is hunter2."

SCHEMA = """
CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL
);
CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
);
"""


def database(tmp_path, sessions, *, schema=SCHEMA, name="opencode.db"):
    path = tmp_path / name
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    for session_id, rows in sessions.items():
        for index, (table, payload) in enumerate(rows):
            if table == "message":
                connection.execute(
                    "INSERT INTO message VALUES (?,?,?,?,?)",
                    (f"{session_id}-m{index}", session_id, index, index,
                     json.dumps(payload)),
                )
            else:
                connection.execute(
                    "INSERT INTO part VALUES (?,?,?,?,?,?)",
                    (f"{session_id}-p{index}", "msg", session_id, index, index,
                     json.dumps(payload)),
                )
    connection.commit()
    connection.close()
    return path


def assistant(model="kimi/k2", *, total=100, cost=0.5, created=1000, completed=4000):
    return (
        "message",
        {
            "role": "assistant",
            "modelID": model,
            "providerID": "openrouter",
            "cost": cost,
            "tokens": {
                "total": total,
                "input": 60,
                "output": 20,
                "reasoning": 10,
                "cache": {"read": 5, "write": 5},
            },
            "time": {"created": created, "completed": completed},
        },
    )


def tool(name="read", *, status="completed", start=1000, end=1500):
    return (
        "part",
        {
            "type": "tool",
            "tool": name,
            "callID": "functions.read:0",
            "state": {
                "status": status,
                "input": {"filePath": "/srv/secrets.env"},
                "output": f"<file>{SECRET}</file>",
                "time": {"start": start, "end": end},
            },
        },
    )


def ref(locator="ses_1", *, durable=True):
    scope = SessionLocatorScope.DURABLE if durable else SessionLocatorScope.PROCESS
    return AgentSessionRef("opencode", locator, scope)


def one_session(tmp_path, rows, locator="ses_1"):
    path = database(tmp_path, {locator: rows})
    return OpenCodeSessionDiagnostics(path)


# -- what it reports -------------------------------------------------------

def test_a_finished_session_is_summarized_from_opencodes_own_record(tmp_path):
    provider = one_session(
        tmp_path,
        [
            assistant(total=100, created=1000, completed=4000),
            tool("read"),
            tool("bash", start=2000, end=2750),
            ("part", {"type": "patch", "files": ["/srv/app/Main.java"]}),
            ("part", {"type": "step-finish", "tokens": {"total": 100}}),
        ],
    )

    item = provider.diagnose_sessions([ref()]).items[0]

    assert isinstance(item, AvailableSessionDiagnostics)
    assert item.tool_calls == 2
    assert item.patch_count == 1
    assert item.duration_seconds == 3.0
    assert item.usage.total_tokens == 100
    assert item.usage.cache_read_tokens == 5
    assert [entry.model for entry in item.usage_by_model] == ["kimi/k2"]


def test_steps_carry_shape_and_timing_but_never_content(tmp_path):
    provider = one_session(tmp_path, [assistant(), tool("read", start=0, end=1250)])

    steps = provider.diagnose_sessions([ref()]).items[0].steps

    assert [step.kind for step in steps] == [SessionStepKind.TOOL]
    assert steps[0].tool_name == "read"
    assert steps[0].duration_seconds == 1.25
    assert not hasattr(steps[0], "input")


def test_usage_comes_from_the_message_not_the_step_parts(tmp_path):
    """`step-finish` parts repeat a message's tokens when a step is retried
    inside it; the message is what the provider billed."""
    provider = one_session(
        tmp_path,
        [
            assistant(total=100),
            ("part", {"type": "step-finish", "tokens": {"total": 100}}),
            ("part", {"type": "step-finish", "tokens": {"total": 100}}),
        ],
    )

    assert provider.diagnose_sessions([ref()]).items[0].usage.total_tokens == 100


def test_only_assistant_messages_are_billed(tmp_path):
    provider = one_session(
        tmp_path, [("message", {"role": "user", "time": {"created": 1}}), assistant()]
    )

    assert provider.diagnose_sessions([ref()]).items[0].usage.total_tokens == 100


def test_several_models_in_one_session_are_reported_separately(tmp_path):
    """A fallback chain inside one conversation is exactly the case where one
    model's rate would be applied to another's tokens."""
    provider = one_session(
        tmp_path, [assistant(model="zeta", total=10), assistant(model="alpha", total=5)]
    )

    usage = provider.diagnose_sessions([ref()]).items[0].usage_by_model

    assert [entry.model for entry in usage] == ["alpha", "zeta"]
    assert usage[1].usage.total_tokens == 10


# -- nothing the model wrote gets out --------------------------------------

def test_no_prompt_tool_output_or_secret_survives_the_projection(tmp_path):
    provider = one_session(
        tmp_path,
        [
            assistant(),
            ("part", {"type": "text", "text": PROMPT}),
            ("part", {"type": "reasoning", "text": f"the key is {SECRET}"}),
            tool("read"),
        ],
    )

    rendered = repr(provider.diagnose_sessions([ref()]))

    assert SECRET not in rendered
    assert "hunter2" not in rendered
    assert "secrets.env" not in rendered


def test_an_unrecognized_tool_name_is_reported_as_other(tmp_path):
    """A tool name can come from a plugin, and a plugin's name can be a
    customer's."""
    provider = one_session(tmp_path, [assistant(), tool("acme_deploy_prod")])

    item = provider.diagnose_sessions([ref()]).items[0]

    assert item.steps[0].tool_name == "other"
    assert "acme" not in repr(item)


# -- signals ---------------------------------------------------------------

def test_a_tool_called_over_and_over_is_a_signal(tmp_path):
    """The shape of an agent looping rather than progressing."""
    provider = one_session(tmp_path, [assistant()] + [tool("bash") for _ in range(12)])

    signals = provider.diagnose_sessions([ref()]).items[0].signals

    assert signals[0].category is DiagnosticSignalCategory.REPEATED_TOOL
    assert signals[0].tool_name == "bash" and signals[0].count == 12


def test_failing_tools_and_compaction_are_observations(tmp_path):
    provider = one_session(
        tmp_path,
        [
            assistant(),
            tool("bash", status="error"),
            tool("bash", status="error"),
            ("part", {"type": "compaction", "auto": True}),
        ],
    )

    codes = {signal.code for signal in provider.diagnose_sessions([ref()]).items[0].signals}

    assert codes == {"tool_error", "context_compacted"}


def test_an_unreadable_row_is_counted_not_fatal(tmp_path):
    path = database(tmp_path, {"ses_1": [assistant()]})
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO part VALUES ('x','m','ses_1',9,9,'{not json')"
    )
    connection.commit()
    connection.close()

    item = OpenCodeSessionDiagnostics(path).diagnose_sessions([ref()]).items[0]

    assert isinstance(item, AvailableSessionDiagnostics)
    assert [s.code for s in item.signals] == ["unreadable_rows"]


# -- limits ----------------------------------------------------------------

def test_too_many_rows_fails_before_any_payload_is_parsed(tmp_path):
    provider = one_session(tmp_path, [assistant()] + [tool() for _ in range(10)])

    with pytest.raises(DiagnosticsLimitExceeded, match="rows"):
        provider.diagnose_sessions([ref()], limits=DiagnosticsLimits(max_parts=5))


def test_one_oversized_row_fails_without_fetching_it(tmp_path):
    """Discovering a 400 MiB session by running out of memory is not a limit."""
    provider = one_session(
        tmp_path, [assistant(), ("part", {"type": "text", "text": "x" * 5000})]
    )

    with pytest.raises(DiagnosticsLimitExceeded, match="bytes"):
        provider.diagnose_sessions([ref()], limits=DiagnosticsLimits(max_raw_row_bytes=1000))


def test_too_many_bytes_in_total_fails(tmp_path):
    provider = one_session(
        tmp_path,
        [assistant()] + [("part", {"type": "text", "text": "x" * 500}) for _ in range(10)],
    )

    with pytest.raises(DiagnosticsLimitExceeded, match="bytes"):
        provider.diagnose_sessions(
            [ref()], limits=DiagnosticsLimits(max_raw_total_bytes=2000)
        )


def test_more_steps_than_the_limit_truncates_detail_not_totals(tmp_path):
    provider = one_session(tmp_path, [assistant(total=100)] + [tool() for _ in range(10)])

    item = provider.diagnose_sessions([ref()], limits=DiagnosticsLimits(max_steps=4)).items[0]

    assert len(item.steps) == 4
    assert item.truncated is True
    assert item.tool_calls == 10, "the count was truncated with the detail"
    assert item.usage.total_tokens == 100


# -- when it cannot answer -------------------------------------------------

def test_a_missing_database_is_unavailable_not_an_exception(tmp_path):
    provider = OpenCodeSessionDiagnostics(tmp_path / "nothing.db")

    item = provider.diagnose_sessions([ref()]).items[0]

    assert item.reason_code is DiagnosticsReasonCode.STORAGE_UNAVAILABLE


def test_a_schema_that_moved_is_reported_as_such(tmp_path):
    """The two OpenCode databases on a developer's machine already disagree
    about table names between versions. An OperationalError reaching a product
    would say the same thing in a form that looks like the product's bug."""
    path = database(
        tmp_path,
        {},
        schema="CREATE TABLE session_entry (id text, session_id text, data text);",
    )

    item = OpenCodeSessionDiagnostics(path).diagnose_sessions([ref()]).items[0]

    assert item.reason_code is DiagnosticsReasonCode.SCHEMA_UNAVAILABLE


def test_a_locator_with_no_rows_is_not_found(tmp_path):
    provider = one_session(tmp_path, [assistant()])

    item = provider.diagnose_sessions([ref("ses_gone")]).items[0]

    assert item.reason_code is DiagnosticsReasonCode.LOCATOR_NOT_FOUND


def test_a_process_scoped_locator_is_refused_before_the_database_is_opened(tmp_path):
    provider = OpenCodeSessionDiagnostics(tmp_path / "nothing.db")

    item = provider.diagnose_sessions([ref(durable=False)]).items[0]

    assert item.reason_code is DiagnosticsReasonCode.PROCESS_SCOPED_LOCATOR


# -- the point of all of it ------------------------------------------------

def test_a_session_is_diagnosable_from_a_persisted_ref_after_a_restart(tmp_path):
    """No harness, no client, no session object: only the durable locator a
    product wrote down last night, and OpenCode's own database."""
    path = database(tmp_path, {"ses_night": [assistant(total=42), tool("bash")]})
    persisted = {"harness": "opencode", "locator": "ses_night", "scope": "durable"}

    restored = AgentSessionRef.from_dict(persisted)
    item = OpenCodeSessionDiagnostics(path).diagnose_sessions([restored]).items[0]

    assert item.usage.total_tokens == 42
    assert item.tool_calls == 1


def test_the_database_is_never_written_to(tmp_path):
    """It belongs to OpenCode. A diagnostic that wrote to it -- even a helpful
    index -- would corrupt the thing it was asked to explain."""
    path = database(tmp_path, {"ses_1": [assistant()]})
    before = path.read_bytes()

    OpenCodeSessionDiagnostics(path).diagnose_sessions([ref()])

    assert path.read_bytes() == before
