"""What a harness needs before it can run a turn, without naming which harness.

Running a turn was never the whole story. Before the first prompt a product
has to get the workspace into a state the agent understands, find out whether
the agent can actually reach its provider, and sometimes let the agent look
around the repository once so later turns are not starting cold.

Those three things were being done by importing a concrete implementation's
configuration writer, its auth client, and its session protocol -- which is
how a product ends up knowing which agent it selected. They belong to the
implementation: only it knows whether it needs repository configuration, how
its authentication is confirmed, and what "initialise this project" means.

So they are three optional capabilities plus three neutral helpers. Optional,
because a harness that only runs turns is still a harness and must keep
working; the helpers therefore check the capability rather than the class.

The one rule worth stating on its own: **a missing readiness capability is
never read as "authenticated"**. A product that cannot tell "no answer" from
"yes" starts paid work against a provider that will refuse it, and the
failure surfaces halfway through a run instead of at startup. Absence raises.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

#: Longest public readiness detail. Diagnostics beyond this belong in the
#: implementation's own logs, which already redact.
DETAIL_MAX_LENGTH = 200

#: The attribute a created harness carries its spec's declaration on. Read it
#: through :func:`readiness_declaration_of` rather than by name.
READINESS_DECLARATION_ATTR = "_agent_core_readiness_declaration"

#: What a spec may declare. ``None`` -- the default -- declares nothing.
READINESS_DECLARATIONS = ("probe", "not_required")

_sleep = time.sleep

_SECRETS = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_\-]{4,}"
    r"|\bbearer\s+\S+"
    r"|\b(?:api[_-]?key|access[_-]?key|token|secret|password)\b\s*[=:]\s*\S+)"
)
_STRUCTURED = re.compile(r"[\{\[].*?[\}\]]", re.DOTALL)


def sanitize_detail(detail: Any) -> str:
    """Bound and scrub a public readiness/bootstrap explanation.

    Implementations write these for a human reading a startup failure, and the
    tempting thing to write is the provider's response. Credentials, JSON
    payloads, and multi-line stack traces are stripped here so that a careless
    adapter cannot leak them through a public field.
    """
    text = " ".join(str(detail or "").split())
    text = _STRUCTURED.sub("[redacted]", text)
    text = _SECRETS.sub("[redacted]", text)
    if len(text) > DETAIL_MAX_LENGTH:
        text = text[: DETAIL_MAX_LENGTH - 1].rstrip() + "…"
    return text


class ReadinessStatus(str, Enum):
    """Why a harness can or cannot start work, in neutral terms.

    Three states rather than a boolean because the two failures call for
    different product responses: authentication is a human action, and
    unavailability is worth waiting out.
    """

    READY = "ready"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HarnessReadiness:
    """One readiness answer. ``detail`` is public-safe and bounded."""

    ready: bool
    status: ReadinessStatus
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ReadinessStatus(self.status))
        object.__setattr__(self, "ready", bool(self.ready))
        object.__setattr__(self, "detail", sanitize_detail(self.detail))


@dataclass(frozen=True)
class WorkspaceBootstrapRequest:
    """What a product wants from one bootstrap, in terms any harness can meet.

    ``purpose`` is a neutral label for logs -- not a prompt. Supply
    ``prompt_file`` to say exactly what the bootstrap turn should do;
    without one the implementation uses its own initialisation mechanism.
    """

    purpose: str
    timeout_seconds: int = 120
    prompt_file: Optional[Path] = None

    def __post_init__(self) -> None:
        if not str(self.purpose or "").strip():
            raise ValueError("a bootstrap request needs a purpose")
        if int(self.timeout_seconds) <= 0:
            raise ValueError("bootstrap timeout_seconds must be positive")
        if self.prompt_file is not None:
            prompt = Path(self.prompt_file)
            if not prompt.is_file():
                raise ValueError(f"bootstrap prompt file does not exist: {prompt}")
            object.__setattr__(self, "prompt_file", prompt)


@dataclass(frozen=True)
class BootstrapResult:
    """What one bootstrap did, described without naming the implementation.

    ``output_text`` is deliberately unstructured: this package does not know
    what a project summary is. The product decides whether that text is worth
    keeping and where it goes.
    """

    completed: bool
    session_id: Optional[str]
    output_text: str
    duration_seconds: float
    usage: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class ReadinessRetryPolicy:
    """How hard to try before believing a harness is unavailable.

    The defaults are the budget products already pay at startup: three
    attempts, three then six seconds apart.
    """

    attempts: int = 3
    backoff_seconds: Callable[[int], float] = field(default=lambda attempt: 3.0 * attempt)

    def __post_init__(self) -> None:
        if int(self.attempts) < 1:
            raise ValueError("a readiness retry policy needs at least one attempt")


class ReadinessUnsupportedError(TypeError):
    """The harness can run turns, but cannot say whether it is ready."""


class BootstrapUnsupportedError(TypeError):
    """The harness can run turns, but has no project bootstrap of its own."""


@runtime_checkable
class WorkspacePreparingHarness(Protocol):
    """A harness that needs the repository put in order before a turn."""

    def prepare_workspace(self, *, repo_path: Path) -> None: ...


@runtime_checkable
class ReadinessCheckingHarness(Protocol):
    """A harness that can confirm it is able to reach its provider."""

    def check_readiness(
        self, *, repo_path: Path, timeout_seconds: int
    ) -> HarnessReadiness: ...


@runtime_checkable
class WorkspaceBootstrappingHarness(Protocol):
    """A harness with its own way of introducing itself to a repository."""

    def bootstrap_workspace(
        self, *, repo_path: Path, request: WorkspaceBootstrapRequest
    ) -> BootstrapResult: ...


def readiness_declaration_of(harness: Any) -> Optional[str]:
    """What the spec that created this harness declared about readiness."""
    declaration = getattr(harness, READINESS_DECLARATION_ATTR, None)
    return str(declaration) if declaration else None


def declare_readiness(harness: Any, declaration: Optional[str]) -> None:
    """Record a spec's readiness declaration on the harness it created.

    Called by :func:`agent_core.harness.create_configured_harness`. A harness
    that refuses the attribute (``__slots__``) simply keeps no declaration,
    which lands on the safe side: readiness then requires the capability.
    """
    if not declaration:
        return
    try:
        setattr(harness, READINESS_DECLARATION_ATTR, str(declaration))
    except AttributeError:  # pragma: no cover - exotic harness
        pass


def prepare_harness_workspace(harness: Any, *, repo_path: Path) -> None:
    """Let the harness put the repository into the shape it expects.

    A no-op for a harness with nothing to prepare, because most have nothing
    to prepare and requiring a stub would break every third-party one.
    """
    repo = _valid_repo(repo_path)
    if isinstance(harness, WorkspacePreparingHarness):
        harness.prepare_workspace(repo_path=repo)


def check_harness_readiness(
    harness: Any,
    *,
    repo_path: Path,
    timeout_seconds: int,
    retry: ReadinessRetryPolicy = ReadinessRetryPolicy(),
) -> HarnessReadiness:
    """Ask whether this harness could start work now, and report what it said.

    Returns the outcome rather than raising on a bad one: what an exhausted
    retry means -- abort the run, wait, or fall back -- is a product decision,
    and a library that made it would be making it for every product.

    Only ``unavailable`` is retried. Retrying a refused credential just makes
    startup slower by the same amount every time.
    """
    repo = _valid_repo(repo_path)
    timeout = int(timeout_seconds)
    if timeout <= 0:
        raise ValueError("readiness timeout_seconds must be positive")

    if readiness_declaration_of(harness) == "not_required":
        return HarnessReadiness(
            ready=True,
            status=ReadinessStatus.READY,
            detail="the harness declares no external readiness check",
        )

    if not isinstance(harness, ReadinessCheckingHarness):
        raise ReadinessUnsupportedError(
            f"{type(harness).__name__} cannot report readiness and its specification "
            "did not declare readiness='not_required'; absence is not authentication"
        )

    attempts = int(retry.attempts)
    outcome: Optional[HarnessReadiness] = None
    for attempt in range(1, attempts + 1):
        outcome = harness.check_readiness(repo_path=repo, timeout_seconds=timeout)
        if not isinstance(outcome, HarnessReadiness):
            raise TypeError(
                f"{type(harness).__name__}.check_readiness must return HarnessReadiness, "
                f"got {type(outcome).__name__}"
            )
        if outcome.status is not ReadinessStatus.UNAVAILABLE:
            return outcome
        if attempt < attempts:
            _sleep(float(retry.backoff_seconds(attempt)))
    assert outcome is not None  # attempts >= 1 is enforced by the policy
    return outcome


def bootstrap_harness_workspace(
    harness: Any,
    *,
    repo_path: Path,
    request: WorkspaceBootstrapRequest,
) -> BootstrapResult:
    """Run one bootstrap through whichever harness was selected.

    Unsupported is reported, not faked: a product whose policy makes bootstrap
    optional can skip it, and one that depends on it finds out here.
    """
    repo = _valid_repo(repo_path)
    if not isinstance(request, WorkspaceBootstrapRequest):
        raise TypeError("request must be a WorkspaceBootstrapRequest")
    if not isinstance(harness, WorkspaceBootstrappingHarness):
        raise BootstrapUnsupportedError(
            f"{type(harness).__name__} has no workspace bootstrap capability"
        )
    result = harness.bootstrap_workspace(repo_path=repo, request=request)
    if not isinstance(result, BootstrapResult):
        raise TypeError(
            f"{type(harness).__name__}.bootstrap_workspace must return BootstrapResult, "
            f"got {type(result).__name__}"
        )
    return result


def _valid_repo(repo_path: Path) -> Path:
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repository path is not a directory: {repo}")
    return repo
