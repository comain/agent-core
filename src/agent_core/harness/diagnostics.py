"""Explaining a session that has already finished, without reading it aloud.

"Why did that turn cost eleven dollars and produce nothing?" is answerable —
the harness wrote down every model call, tool invocation and patch — but the
place it wrote them is full of the one thing that must not leave: the prompt,
the model's reasoning, the file contents it read. A diagnostic that quotes any
of that has moved private material into a report, a ticket, or a log.

So the contract is a projection, not a dump. Counts, durations, token totals,
tool *names* from a fixed vocabulary, and classified signals. Nothing here can
carry a sentence the model wrote.

Two other properties matter as much.

**Every limit is checked before the thing it protects is loaded.** A row is
measured with `length(data)` before it is parsed; a report is refused before
its payload is fetched. Discovering a 400 MiB session by running out of memory
is not a limit, and a limit that reports a partial total after truncating is
worse than no answer — someone will compare it to a budget.

**Unavailable is a status, not an exception.** A harness that cannot do this, a
locator that no longer resolves, a storage schema that moved: all of those are
ordinary answers about one session, and a report containing them is still
useful for the sessions that did resolve. Only a caller asking for more than
the hard limits allow is an error, because that is a bug in the caller rather
than a fact about a session.

## Why a mixed report has no total

`total_usage` is present only when *every* requested item is available. A total
over the subset that happened to resolve looks authoritative and is always an
undercount; the per-item numbers stay visible, so nothing is hidden — what is
withheld is the one number someone would otherwise put in a budget column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from agent_core.harness.sessions import AgentSessionRef, SessionLocatorScope

__all__ = [
    "AvailableSessionDiagnostics",
    "DiagnosticSignal",
    "DiagnosticSignalCategory",
    "DiagnosticsLimitExceeded",
    "DiagnosticsLimits",
    "DiagnosticsReasonCode",
    "DiagnosticsStatus",
    "ModelUsage",
    "SessionDiagnosticsProvider",
    "SessionDiagnosticsReport",
    "SessionStepDiagnostic",
    "SessionStepKind",
    "TokenUsage",
    "UnavailableSessionDiagnostics",
    "UnsupportedSessionDiagnostics",
    "build_report",
    "create_configured_diagnostics_provider",
    "diagnose_sessions",
    "register_diagnostics_provider",
    "unregister_diagnostics_provider",
]


class DiagnosticsStatus(Enum):
    AVAILABLE = "available"
    #: The harness cannot answer this kind of question at all.
    UNSUPPORTED = "unsupported"
    #: It could, but not for this session right now.
    UNAVAILABLE = "unavailable"


class DiagnosticsReasonCode(Enum):
    HARNESS_UNSUPPORTED = "harness_unsupported"
    PROCESS_SCOPED_LOCATOR = "process_scoped_locator"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    LOCATOR_NOT_FOUND = "locator_not_found"


class DiagnosticSignalCategory(Enum):
    """What kind of observation a signal is, from a closed set.

    Closed because a free-text category is where prose creeps back in: the
    first "just this once" summary of what went wrong is a sentence the model
    wrote, quoted into a report.
    """

    HINT = "hint"
    COMPILE_FACT = "compile_fact"
    REPEATED_TOOL = "repeated_tool"
    OBSERVATION = "observation"


class SessionStepKind(Enum):
    MODEL = "model"
    TOOL = "tool"
    PATCH = "patch"


class DiagnosticsLimitExceeded(RuntimeError):
    """The caller asked for more than the hard limits allow.

    An error rather than a report status: a session too large to summarize is
    a fact about the session and belongs in the report, but a request for 400
    sessions is a bug in the caller, and answering it partially would hide that.
    """


@dataclass(frozen=True)
class DiagnosticsLimits:
    """Ceilings, not targets. A caller may lower any of these, never raise one.

    The hard maxima are the defaults, so the safe call is the default call.
    Every value is a byte or item count that can be checked *before* the thing
    it bounds is loaded.
    """

    max_sessions: int = 32
    max_parts: int = 200_000
    max_steps: int = 5_000
    max_signals: int = 200
    max_raw_row_bytes: int = 1_048_576
    max_raw_total_bytes: int = 67_108_864
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        hard = HARD_LIMITS
        for name in (
            "max_sessions",
            "max_parts",
            "max_steps",
            "max_signals",
            "max_raw_row_bytes",
            "max_raw_total_bytes",
            "max_output_bytes",
        ):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")
            ceiling = getattr(hard, name)
            if value > ceiling:
                raise ValueError(
                    f"{name}={value} exceeds the hard limit of {ceiling}; "
                    "limits may be lowered but never raised"
                )


#: Built without validation, because it *is* the validation.
HARD_LIMITS = DiagnosticsLimits.__new__(DiagnosticsLimits)
object.__setattr__(HARD_LIMITS, "max_sessions", 32)
object.__setattr__(HARD_LIMITS, "max_parts", 200_000)
object.__setattr__(HARD_LIMITS, "max_steps", 5_000)
object.__setattr__(HARD_LIMITS, "max_signals", 200)
object.__setattr__(HARD_LIMITS, "max_raw_row_bytes", 1_048_576)
object.__setattr__(HARD_LIMITS, "max_raw_total_bytes", 67_108_864)
object.__setattr__(HARD_LIMITS, "max_output_bytes", 1_048_576)


@dataclass(frozen=True)
class TokenUsage:
    """Tokens in the names a table stores, not the ones a provider emits."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class ModelUsage:
    model: str
    usage: TokenUsage


@dataclass(frozen=True)
class SessionStepDiagnostic:
    """One step, as shape and timing only. No inputs, no outputs."""

    sequence: int
    kind: SessionStepKind
    duration_seconds: Optional[float] = None
    tool_name: Optional[str] = None


@dataclass(frozen=True)
class DiagnosticSignal:
    """Something worth an operator's attention, already classified.

    ``private_summary`` is optional and, where a provider fills it, sanitized
    and bounded. It is named *private* to make the disclosure decision explicit
    at every use: it may reach an operator, never a report a product publishes.
    """

    category: DiagnosticSignalCategory
    code: str
    count: int = 1
    tool_name: Optional[str] = None
    private_summary: Optional[str] = None


@dataclass(frozen=True)
class AvailableSessionDiagnostics:
    session: AgentSessionRef
    usage: TokenUsage = field(default_factory=TokenUsage)
    usage_by_model: Tuple[ModelUsage, ...] = ()
    duration_seconds: Optional[float] = None
    tool_calls: Optional[int] = None
    patch_count: Optional[int] = None
    steps: Tuple[SessionStepDiagnostic, ...] = ()
    signals: Tuple[DiagnosticSignal, ...] = ()
    #: Detail was cut to fit an output bound. Totals above are still exact.
    truncated: bool = False
    status: DiagnosticsStatus = DiagnosticsStatus.AVAILABLE


@dataclass(frozen=True)
class UnsupportedSessionDiagnostics:
    session: AgentSessionRef
    reason_code: DiagnosticsReasonCode
    status: DiagnosticsStatus = DiagnosticsStatus.UNSUPPORTED


@dataclass(frozen=True)
class UnavailableSessionDiagnostics:
    session: AgentSessionRef
    reason_code: DiagnosticsReasonCode
    status: DiagnosticsStatus = DiagnosticsStatus.UNAVAILABLE


#: The closed union. A fourth member would be a fourth branch in every reader.
SessionDiagnosticsItem = (
    AvailableSessionDiagnostics,
    UnsupportedSessionDiagnostics,
    UnavailableSessionDiagnostics,
)


@dataclass(frozen=True)
class SessionDiagnosticsReport:
    items: Tuple[Any, ...] = ()
    #: Present only when every item is available. See the module docstring.
    total_usage: Optional[TokenUsage] = None
    usage_by_model: Tuple[ModelUsage, ...] = ()
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return self.total_usage is not None


@runtime_checkable
class SessionDiagnosticsProvider(Protocol):
    """Implemented beside a harness adapter, not by every harness.

    Optional on purpose: a harness that keeps no durable record of a finished
    session should say so once, here, rather than growing a method that returns
    an apology.
    """

    def diagnose_sessions(
        self, refs: Sequence[AgentSessionRef], *, limits: DiagnosticsLimits
    ) -> SessionDiagnosticsReport: ...


DiagnosticsProviderFactory = Callable[
    [Any, Mapping[str, Any]], SessionDiagnosticsProvider
]
_DIAGNOSTICS_FACTORIES: dict[str, DiagnosticsProviderFactory] = {}


def register_diagnostics_provider(
    name: str, factory: DiagnosticsProviderFactory, *, replace: bool = False
) -> None:
    """Register an optional diagnostics capability beside a harness adapter."""
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("diagnostics provider name must not be empty")
    if key in _DIAGNOSTICS_FACTORIES and not replace:
        raise ValueError(f"diagnostics are already registered for {name!r}")
    _DIAGNOSTICS_FACTORIES[key] = factory


def unregister_diagnostics_provider(name: str) -> None:
    """Remove a diagnostics registration. Intended for isolated tests."""
    _DIAGNOSTICS_FACTORIES.pop(str(name or "").strip().lower(), None)


def create_configured_diagnostics_provider(
    spec: Any, **options: Any
) -> Optional[SessionDiagnosticsProvider]:
    """Resolve optional diagnostics from the neutral execution specification.

    ``None`` means the configured harness has no offline diagnostics capability;
    passing an empty provider mapping to :func:`diagnose_sessions` then returns
    explicit unsupported items for its references.
    """
    factory = _DIAGNOSTICS_FACTORIES.get(str(spec.name or "").strip().lower())
    return factory(spec, dict(options)) if factory is not None else None


def build_report(
    items: Iterable[Any], *, limits: Optional[DiagnosticsLimits] = None
) -> SessionDiagnosticsReport:
    """Assemble a report, withholding a total the parts cannot justify."""
    ordered = tuple(items)
    available = [item for item in ordered if isinstance(item, AvailableSessionDiagnostics)]
    truncated = any(getattr(item, "truncated", False) for item in ordered)

    if len(available) != len(ordered) or not ordered:
        # A total over whatever resolved is an undercount that reads as a fact.
        return SessionDiagnosticsReport(items=ordered, truncated=truncated)

    total = TokenUsage()
    by_model: dict = {}
    for item in available:
        total = total + item.usage
        for entry in item.usage_by_model:
            by_model[entry.model] = by_model.get(entry.model, TokenUsage()) + entry.usage

    return SessionDiagnosticsReport(
        items=ordered,
        total_usage=total,
        # Sorted, because a report read side by side with yesterday's should
        # differ only where the numbers differ.
        usage_by_model=tuple(
            ModelUsage(model=model, usage=by_model[model]) for model in sorted(by_model)
        ),
        truncated=truncated,
    )


def diagnose_sessions(
    refs: Sequence[AgentSessionRef],
    *,
    providers: Mapping[str, SessionDiagnosticsProvider],
    limits: Optional[DiagnosticsLimits] = None,
) -> SessionDiagnosticsReport:
    """Diagnose refs across harnesses, in the order they were given.

    Order is the record of what was tried, so it is preserved even though the
    work is grouped by harness underneath.

    Registry support is resolved *here*, when diagnostics is asked for, rather
    than when the ref was built: a ref persisted months ago must still be
    constructible after its harness is unregistered, and the right answer then
    is an unsupported item, not a failure to load the row.
    """
    limits = limits or DiagnosticsLimits()
    if len(refs) > limits.max_sessions:
        raise DiagnosticsLimitExceeded(
            f"{len(refs)} sessions requested, limit is {limits.max_sessions}"
        )

    answers: dict = {}
    wanted: dict = {}
    for ref in refs:
        if ref.scope is not SessionLocatorScope.DURABLE:
            # The locator only ever meant something inside a process that has
            # since exited. Nothing can resolve it, and pretending otherwise
            # produces a lookup miss that reads like deleted data.
            answers[ref] = UnsupportedSessionDiagnostics(
                session=ref, reason_code=DiagnosticsReasonCode.PROCESS_SCOPED_LOCATOR
            )
        elif ref.harness not in providers:
            answers[ref] = UnsupportedSessionDiagnostics(
                session=ref, reason_code=DiagnosticsReasonCode.HARNESS_UNSUPPORTED
            )
        else:
            wanted.setdefault(ref.harness, []).append(ref)

    truncated = False
    for harness, group in wanted.items():
        report = providers[harness].diagnose_sessions(tuple(group), limits=limits)
        truncated = truncated or report.truncated
        for item in report.items:
            answers[item.session] = item

    ordered = tuple(answers[ref] for ref in refs if ref in answers)
    report = build_report(ordered, limits=limits)
    if truncated and not report.truncated:
        import dataclasses

        report = dataclasses.replace(report, truncated=True)
    return report
