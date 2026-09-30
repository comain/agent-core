"""Reading OpenCode's own record of a session, after everyone has gone home.

The wrapper this package puts around OpenCode keeps a conversation in memory.
When the process exits that is gone — but OpenCode has been writing the whole
conversation to its own SQLite database the entire time, and that survives. So
"why did last night's run cost eleven dollars and produce nothing?" is
answerable this morning, from the durable locator persisted with the result.

Four rules make reading someone else's database safe enough to do in
production.

**Read-only, and never migrated.** The connection is opened with `mode=ro` and
`PRAGMA query_only=ON`. This database belongs to OpenCode; a diagnostic that
wrote to it — even a schema fix, even a helpful index — would be a tool that
corrupts the thing it was asked to explain.

**Measured before it is parsed.** One aggregate query reports how many rows
there are and how large the largest and the total are, using `length(data)` so
no payload is fetched. Only if all three are within bounds does a second query
stream the rows. Discovering a 400 MiB session by running out of memory is not
a limit.

**Schema drift is an answer, not a traceback.** The two OpenCode databases on
a developer's machine right now already disagree about table names between
versions. The tables this needs are checked by name and column before anything
is queried, and a mismatch produces `SCHEMA_UNAVAILABLE` for every requested
session. An `OperationalError` escaping into a product's report would say the
same thing, less usefully, in a form that looks like a bug in the product.

**Nothing the model wrote comes out.** The rows hold prompts, reasoning, file
contents and tool output. What leaves here is counts, durations, token totals,
and tool names from a fixed vocabulary — anything else is reported as `other`,
because a tool name can be a plugin's, and a plugin's name can be a customer's.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_core.config import current_config
from agent_core.harness.diagnostics import (
    AvailableSessionDiagnostics,
    DiagnosticSignal,
    DiagnosticSignalCategory,
    DiagnosticsLimitExceeded,
    DiagnosticsLimits,
    DiagnosticsReasonCode,
    ModelUsage,
    SessionDiagnosticsReport,
    SessionStepDiagnostic,
    SessionStepKind,
    TokenUsage,
    UnavailableSessionDiagnostics,
    build_report,
)
from agent_core.harness.sessions import AgentSessionRef, SessionLocatorScope

logger = logging.getLogger(__name__)

HARNESS = "opencode"

#: The tables and columns this reads. Checked before anything is queried.
_REQUIRED_SCHEMA = {
    "part": {"id", "session_id", "time_created", "data"},
    "message": {"id", "session_id", "time_created", "data"},
}

#: OpenCode's built-in tools. Anything outside this is reported as `other`:
#: a tool name can come from a plugin, and a plugin's name can be a customer's.
_KNOWN_TOOLS = frozenset(
    {
        "bash", "edit", "glob", "grep", "list", "multiedit", "patch", "read",
        "task", "todoread", "todowrite", "webfetch", "write",
    }
)
_OTHER_TOOL = "other"

#: A tool called this many times in one session is worth an operator's
#: attention: it is the shape of an agent looping rather than progressing.
_REPEATED_TOOL_THRESHOLD = 10


def _known_tool(name: Any) -> str:
    text = str(name or "").strip().lower()
    return text if text in _KNOWN_TOOLS else _OTHER_TOOL


def default_database_path() -> Path:
    """Where OpenCode keeps its own record, by the same rules OpenCode uses."""
    configured = str(getattr(current_config(), "opencode_data_home", "") or "")
    if configured:
        return Path(configured).expanduser() / "opencode.db"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "opencode" / "opencode.db"


class OpenCodeSessionDiagnostics:
    """`SessionDiagnosticsProvider` for OpenCode's durable session locators."""

    def __init__(self, database_path: Optional[Path] = None):
        self._database_path = Path(database_path) if database_path else None

    @property
    def database_path(self) -> Path:
        return self._database_path or default_database_path()

    def diagnose_sessions(
        self,
        refs: Sequence[AgentSessionRef],
        *,
        limits: Optional[DiagnosticsLimits] = None,
    ) -> SessionDiagnosticsReport:
        limits = limits or DiagnosticsLimits()
        refs = tuple(refs)
        if len(refs) > limits.max_sessions:
            raise DiagnosticsLimitExceeded(
                f"{len(refs)} sessions requested, limit is {limits.max_sessions}"
            )

        usable = [ref for ref in refs if ref.scope is SessionLocatorScope.DURABLE]
        unusable = {
            ref: DiagnosticsReasonCode.PROCESS_SCOPED_LOCATOR
            for ref in refs
            if ref.scope is not SessionLocatorScope.DURABLE
        }
        if not usable:
            return build_report(
                _unavailable(ref, reason) for ref, reason in unusable.items()
            )

        path = self.database_path
        if not path.is_file():
            return _all_unavailable(refs, DiagnosticsReasonCode.STORAGE_UNAVAILABLE)

        try:
            connection = _open_read_only(path)
        except sqlite3.Error as exc:
            logger.warning("could not open the OpenCode database: %s", exc)
            return _all_unavailable(refs, DiagnosticsReasonCode.STORAGE_UNAVAILABLE)

        try:
            if not _schema_matches(connection):
                return _all_unavailable(refs, DiagnosticsReasonCode.SCHEMA_UNAVAILABLE)
            rows = _fetch_within_limits(
                connection, [ref.locator for ref in usable], limits
            )
        except DiagnosticsLimitExceeded:
            raise
        except sqlite3.Error as exc:
            logger.warning("the OpenCode database could not be read: %s", exc)
            return _all_unavailable(refs, DiagnosticsReasonCode.SCHEMA_UNAVAILABLE)
        finally:
            connection.close()

        items = []
        for ref in refs:
            if ref in unusable:
                items.append(_unavailable(ref, unusable[ref]))
            elif ref.locator not in rows:
                items.append(
                    _unavailable(ref, DiagnosticsReasonCode.LOCATOR_NOT_FOUND)
                )
            else:
                items.append(_summarize(ref, rows[ref.locator], limits))
        return build_report(items, limits=limits)


# -- reading ---------------------------------------------------------------

def _open_read_only(path: Path) -> sqlite3.Connection:
    """`mode=ro` *and* `query_only`: the first stops this process opening a
    writable handle, the second stops any statement that slips through."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _schema_matches(connection: sqlite3.Connection) -> bool:
    for table, columns in _REQUIRED_SCHEMA.items():
        try:
            present = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
        except sqlite3.Error:
            return False
        if not present or not columns.issubset(present):
            logger.info("OpenCode's %s table is not the shape this understands", table)
            return False
    return True


def _fetch_within_limits(
    connection: sqlite3.Connection, locators: Sequence[str], limits: DiagnosticsLimits
) -> Dict[str, List[Tuple[str, dict]]]:
    """Measure with `length(data)`, then stream. Never the other way round."""
    placeholders = ",".join("?" for _ in locators)
    scope = (
        f"SELECT 'part' AS source, session_id, time_created, id, data "
        f"FROM part WHERE session_id IN ({placeholders}) "
        f"UNION ALL "
        f"SELECT 'message' AS source, session_id, time_created, id, data "
        f"FROM message WHERE session_id IN ({placeholders})"
    )
    bindings = list(locators) * 2

    count, largest, total = connection.execute(
        f"SELECT COUNT(*), COALESCE(MAX(LENGTH(data)), 0), COALESCE(SUM(LENGTH(data)), 0) "
        f"FROM ({scope})",
        bindings,
    ).fetchone()

    if count > limits.max_parts:
        raise DiagnosticsLimitExceeded(
            f"{count} rows exceeds the {limits.max_parts}-row limit"
        )
    if largest > limits.max_raw_row_bytes:
        raise DiagnosticsLimitExceeded(
            f"a row of {largest} bytes exceeds the {limits.max_raw_row_bytes}-byte limit"
        )
    if total > limits.max_raw_total_bytes:
        raise DiagnosticsLimitExceeded(
            f"{total} bytes exceeds the {limits.max_raw_total_bytes}-byte limit"
        )

    grouped: Dict[str, List[Tuple[str, dict]]] = {}
    for source, session_id, _created, _id, data in connection.execute(
        f"SELECT * FROM ({scope}) ORDER BY session_id, time_created, id", bindings
    ):
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            # One unreadable row is not a reason to lose the session; it is
            # counted as an observation below.
            payload = {"type": "__unreadable__"}
        if not isinstance(payload, dict):
            payload = {"type": "__unreadable__"}
        grouped.setdefault(session_id, []).append((source, payload))
    return grouped


# -- projecting ------------------------------------------------------------

def _summarize(
    ref: AgentSessionRef, rows: Sequence[Tuple[str, dict]], limits: DiagnosticsLimits
) -> AvailableSessionDiagnostics:
    usage = TokenUsage()
    by_model: Dict[str, TokenUsage] = {}
    steps: List[SessionStepDiagnostic] = []
    tool_calls = 0
    patch_count = 0
    tool_uses: Counter = Counter()
    tool_errors: Counter = Counter()
    observations: Counter = Counter()
    unreadable = 0
    first_start: Optional[int] = None
    last_end: Optional[int] = None
    truncated = False

    for source, payload in rows:
        kind = str(payload.get("type") or "")
        if source == "message":
            if str(payload.get("role") or "") != "assistant":
                continue
            # Usage is taken from the message rather than from `step-finish`
            # parts: the message is what the provider billed, and the parts
            # double-count when a step is retried inside one message.
            tokens = _tokens(payload.get("tokens"))
            usage = usage + tokens
            model = str(payload.get("modelID") or "unknown")
            by_model[model] = by_model.get(model, TokenUsage()) + tokens
            times = payload.get("time") or {}
            first_start = _earliest(first_start, times.get("created"))
            last_end = _latest(last_end, times.get("completed"))
            continue

        if kind == "__unreadable__":
            unreadable += 1
            continue
        if kind == "tool":
            tool_calls += 1
            name = _known_tool(payload.get("tool"))
            tool_uses[name] += 1
            state = payload.get("state") or {}
            if str(state.get("status") or "") == "error":
                tool_errors[name] += 1
            steps.append(
                SessionStepDiagnostic(
                    sequence=len(steps) + 1,
                    kind=SessionStepKind.TOOL,
                    duration_seconds=_duration(state.get("time")),
                    tool_name=name,
                )
            )
        elif kind == "patch":
            patch_count += 1
            steps.append(
                SessionStepDiagnostic(sequence=len(steps) + 1, kind=SessionStepKind.PATCH)
            )
        elif kind == "step-finish":
            steps.append(
                SessionStepDiagnostic(sequence=len(steps) + 1, kind=SessionStepKind.MODEL)
            )
        elif kind == "compaction":
            # The context window filled and OpenCode summarized the history.
            # Worth surfacing: it is a common reason a long session starts
            # answering as though it has forgotten what it was doing.
            observations["context_compacted"] += 1

        if len(steps) > limits.max_steps:
            steps = steps[: limits.max_steps]
            truncated = True

    signals = _signals(tool_uses, tool_errors, observations, unreadable, limits)
    return AvailableSessionDiagnostics(
        session=ref,
        usage=usage,
        usage_by_model=tuple(
            ModelUsage(model=model, usage=by_model[model]) for model in sorted(by_model)
        ),
        duration_seconds=_span(first_start, last_end),
        tool_calls=tool_calls,
        patch_count=patch_count,
        steps=tuple(steps),
        signals=signals,
        truncated=truncated or len(signals) >= limits.max_signals,
    )


def _signals(
    tool_uses: Counter,
    tool_errors: Counter,
    observations: Counter,
    unreadable: int,
    limits: DiagnosticsLimits,
) -> Tuple[DiagnosticSignal, ...]:
    signals: List[DiagnosticSignal] = []
    for name, count in sorted(tool_uses.items()):
        if count >= _REPEATED_TOOL_THRESHOLD:
            signals.append(
                DiagnosticSignal(
                    category=DiagnosticSignalCategory.REPEATED_TOOL,
                    code="repeated_tool_calls",
                    count=count,
                    tool_name=name,
                )
            )
    for name, count in sorted(tool_errors.items()):
        signals.append(
            DiagnosticSignal(
                category=DiagnosticSignalCategory.OBSERVATION,
                code="tool_error",
                count=count,
                tool_name=name,
            )
        )
    for code, count in sorted(observations.items()):
        signals.append(
            DiagnosticSignal(
                category=DiagnosticSignalCategory.OBSERVATION, code=code, count=count
            )
        )
    if unreadable:
        signals.append(
            DiagnosticSignal(
                category=DiagnosticSignalCategory.OBSERVATION,
                code="unreadable_rows",
                count=unreadable,
            )
        )
    return tuple(signals[: limits.max_signals])


def _tokens(raw: Any) -> TokenUsage:
    tokens = raw if isinstance(raw, dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    return TokenUsage(
        input_tokens=_int(tokens.get("input")),
        output_tokens=_int(tokens.get("output")),
        reasoning_tokens=_int(tokens.get("reasoning")),
        cache_read_tokens=_int(cache.get("read")),
        cache_write_tokens=_int(cache.get("write")),
        total_tokens=_int(tokens.get("total")),
    )


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _duration(raw: Any) -> Optional[float]:
    times = raw if isinstance(raw, dict) else {}
    start, end = times.get("start"), times.get("end")
    return _span(start, end)


def _span(start: Any, end: Any) -> Optional[float]:
    """OpenCode records epoch milliseconds."""
    try:
        if start is None or end is None:
            return None
        elapsed = (float(end) - float(start)) / 1000.0
    except (TypeError, ValueError):
        return None
    return round(elapsed, 3) if elapsed >= 0 else None


def _earliest(current: Optional[int], value: Any) -> Optional[int]:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return current
    return candidate if current is None else min(current, candidate)


def _latest(current: Optional[int], value: Any) -> Optional[int]:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return current
    return candidate if current is None else max(current, candidate)


def _unavailable(
    ref: AgentSessionRef, reason: DiagnosticsReasonCode
) -> UnavailableSessionDiagnostics:
    return UnavailableSessionDiagnostics(session=ref, reason_code=reason)


def _all_unavailable(
    refs: Sequence[AgentSessionRef], reason: DiagnosticsReasonCode
) -> SessionDiagnosticsReport:
    return build_report(_unavailable(ref, reason) for ref in refs)
