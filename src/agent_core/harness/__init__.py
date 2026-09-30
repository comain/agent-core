"""OpenCode agent harness.

The public surface below is derived from every production call site in the
source repo, cross-checked against each module's declared public symbols --
not from what the tests happened to import. An earlier draft used the test
imports and missed ten symbols, including two of the three public names in
``fallback``, both of which had live production callers.

Anything not exported here is private and may change without notice. Tests are
permitted to reach into module internals; consumers are not.

``PROJECT_ROOT`` is deliberately absent: the source derived it from package
depth, which resolves somewhere meaningless under this package's layout.
"""

from agent_core.harness.opencode import OpenCodeHarness, create_opencode_harness
from agent_core.harness.diagnostics import (
    AvailableSessionDiagnostics,
    DiagnosticSignal,
    DiagnosticSignalCategory,
    DiagnosticsLimitExceeded,
    DiagnosticsLimits,
    DiagnosticsReasonCode,
    DiagnosticsStatus,
    ModelUsage,
    SessionDiagnosticsProvider,
    SessionDiagnosticsReport,
    SessionStepDiagnostic,
    SessionStepKind,
    TokenUsage,
    UnavailableSessionDiagnostics,
    UnsupportedSessionDiagnostics,
    create_configured_diagnostics_provider,
    diagnose_sessions,
    register_diagnostics_provider,
    unregister_diagnostics_provider,
)
from agent_core.harness.opencode_diagnostics import OpenCodeSessionDiagnostics
from agent_core.harness.execution import (
    AgentTurnContext,
    AgentTurnExecutionError,
    AgentTurnExecutionResult,
    AgentTurnRequest,
    CancellationSource,
    ExecutionObserver,
    HarnessBinding,
    ResultCommitError,
    SessionFactory,
    TurnGuard,
    TurnProgressPort,
    TurnResultPort,
    execute_agent_turn,
)
from agent_core.harness.cost import (
    AttemptNotAdmitted,
    CostAdmissionError,
    PaidAttempts,
    TurnCostPort,
    accounts_for_paid_attempts,
    aggregate_turn_costs,
)
from agent_core.harness.sessions import (
    AgentSessionRef,
    CostGate,
    FallbackHarnessSession,
    HarnessSession,
    ResumableHarness,
    SessionLocatorScope,
    SessionSnapshot,
    SessionUnsupportedError,
    TurnCost,
    merge_session_refs,
    open_harness_session,
)
from agent_core.harness.registry import (
    Harness,
    HarnessSpec,
    PolicyEnforcingHarness,
    TurnProgress,
    UnknownHarnessError,
    available_harnesses,
    create_configured_harness,
    create_harness,
    preferred_model_of,
    register_harness,
    unregister_harness,
)
from agent_core.harness.lifecycle import (
    BootstrapResult,
    BootstrapUnsupportedError,
    HarnessReadiness,
    ReadinessCheckingHarness,
    ReadinessRetryPolicy,
    ReadinessStatus,
    ReadinessUnsupportedError,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrappingHarness,
    WorkspacePreparingHarness,
    bootstrap_harness_workspace,
    check_harness_readiness,
    prepare_harness_workspace,
    readiness_declaration_of,
)
from agent_core.harness.node import (
    NodeOutcome,
    NullRecorder,
    TurnRecorder,
    run_harness_node,
)
from agent_core.harness.records import TurnRecord, describe_turn, token_usage_from_turn
from agent_core.harness.timeouts import (
    effective_timeout,
    parse_provider_timeout_multipliers,
    provider_of,
    timeout_multiplier_for,
)
from agent_core.harness.usage import (
    TOKEN_FIELDS,
    add_usage,
    empty_usage,
    sum_usage,
)
from agent_core.harness.recovery import (
    STALL_TYPES,
    continue_session,
    is_stall,
    session_recovery,
)
from agent_core.harness.turns import (
    StructuredTurn,
    TurnLoop,
    run_structured_turn,
    run_until_accepted,
)
from agent_core.harness.client import OpenCodeAuthClient, OpenCodeClient
from agent_core.harness.config import (
    CURSOR_PLUGIN_NAME,
    _provider_api_key as provider_api_key,
    build_opencode_config_dict,
    EXTERNAL_DIRS_CONFIG,
    GLOBAL_OPENCODE_CONFIG,
    GLOBAL_OPENCODE_PLUGIN_ROOT,
    OPENCODE_PLUGIN_CACHE_ROOT,
    generate_opencode_config,
)
from agent_core.harness.fallback import (
    ProviderRateLimitError,
    poll_completion_with_task_guard,
    raise_for_provider_fallback_event,
)
from agent_core.harness.net import build_base_url, format_host_for_url
from agent_core.harness.process import (
    OpenCodeProcess,
    TurnResult,
    classify_provider_model_error,
)
from agent_core.harness.rate_limit import (
    debug_log_dir,
    detect_rate_limit_in_logs,
    opencode_log_dir,
    parse_rate_limit_payload,
    recent_log_files,
)
from agent_core.harness.affinity import SessionAffinity
from agent_core.prompts import render_placeholders
from agent_core.harness.testing import FakeOpenCodeProcess
from agent_core.harness.shutdown import (
    active_process_count,
    install_shutdown_handlers,
    shutdown_requested,
    terminate_active_processes,
)
from agent_core.harness.workspace import per_turn_workspace
from agent_core.harness.runner import (
    run_turn_with_fallback,
    should_skip_provider,
)
from agent_core.harness.server import OpenCodeServer
from agent_core.harness.stream import OpenCodeStreamParser
from agent_core.harness.structured_output import extract_json_object
from agent_core.harness.tiered_router import (
    ModelHealthTracker,
    ProviderCandidate,
    available_provider_candidates,
    cheap_model_for_phase,
    effective_model,
    is_model_healthy,
    is_model_permanently_unhealthy,
    mark_model_unhealthy,
    model_health_for_candidates,
    opencode_model_id,
    parse_model_list_response,
    parse_provider_base_urls,
    parse_provider_chain,
    parse_provider_tokens,
    provider_candidates,
    provider_local_model_id,
    provider_token_statuses,
    reset_model_availability_cache,
    reset_model_health,
)

__all__ = [
    "session_recovery",
    "continue_session",
    "is_stall",
    "STALL_TYPES",
    "sum_usage",
    "add_usage",
    "empty_usage",
    "TOKEN_FIELDS",
    "effective_timeout",
    "timeout_multiplier_for",
    "parse_provider_timeout_multipliers",
    "provider_of",
    "Harness",
    "AgentSessionRef",
    "SessionLocatorScope",
    "merge_session_refs",
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
    "create_configured_diagnostics_provider",
    "diagnose_sessions",
    "register_diagnostics_provider",
    "unregister_diagnostics_provider",
    "AgentTurnContext",
    "AgentTurnExecutionError",
    "AgentTurnExecutionResult",
    "AgentTurnRequest",
    "CancellationSource",
    "ExecutionObserver",
    "HarnessBinding",
    "ResultCommitError",
    "SessionFactory",
    "TurnGuard",
    "TurnProgressPort",
    "TurnResultPort",
    "execute_agent_turn",
    "AttemptNotAdmitted",
    "CostAdmissionError",
    "PaidAttempts",
    "TurnCostPort",
    "accounts_for_paid_attempts",
    "aggregate_turn_costs",
    "CostGate",
    "FallbackHarnessSession",
    "HarnessSession",
    "ResumableHarness",
    "SessionSnapshot",
    "SessionUnsupportedError",
    "TurnCost",
    "open_harness_session",
    "HarnessSpec",
    "PolicyEnforcingHarness",
    "TurnProgress",
    "NodeOutcome",
    "OpenCodeHarness",
    "UnknownHarnessError",
    "available_harnesses",
    "create_harness",
    "create_configured_harness",
    "preferred_model_of",
    "register_harness",
    "unregister_harness",
    # lifecycle
    "HarnessReadiness",
    "ReadinessStatus",
    "ReadinessRetryPolicy",
    "ReadinessUnsupportedError",
    "ReadinessCheckingHarness",
    "WorkspacePreparingHarness",
    "WorkspaceBootstrappingHarness",
    "WorkspaceBootstrapRequest",
    "BootstrapResult",
    "BootstrapUnsupportedError",
    "prepare_harness_workspace",
    "check_harness_readiness",
    "bootstrap_harness_workspace",
    "readiness_declaration_of",
    "NullRecorder",
    "StructuredTurn",
    "TurnLoop",
    "TurnRecord",
    "TurnRecorder",
    "describe_turn",
    "run_harness_node",
    "token_usage_from_turn",
    "run_structured_turn",
    "run_until_accepted",
    # client
    "OpenCodeClient",
    "OpenCodeAuthClient",
    # process
    "OpenCodeProcess",
    "TurnResult",
    "classify_provider_model_error",
    # runner
    "run_turn_with_fallback",
    "should_skip_provider",
    # server
    "OpenCodeServer",
    # stream
    "OpenCodeStreamParser",
    "extract_json_object",
    # config
    "generate_opencode_config",
    "provider_api_key",
    "build_opencode_config_dict",
    "per_turn_workspace",
    "install_shutdown_handlers",
    "shutdown_requested",
    "terminate_active_processes",
    "active_process_count",
    "SessionAffinity",
    "render_placeholders",
    "FakeOpenCodeProcess",
    "CURSOR_PLUGIN_NAME",
    "EXTERNAL_DIRS_CONFIG",
    "GLOBAL_OPENCODE_CONFIG",
    "GLOBAL_OPENCODE_PLUGIN_ROOT",
    "OPENCODE_PLUGIN_CACHE_ROOT",
    # tiered_router
    "ProviderCandidate",
    "ModelHealthTracker",
    "effective_model",
    "cheap_model_for_phase",
    "provider_candidates",
    "available_provider_candidates",
    "opencode_model_id",
    "provider_local_model_id",
    "provider_token_statuses",
    "mark_model_unhealthy",
    "is_model_healthy",
    "is_model_permanently_unhealthy",
    "reset_model_health",
    "model_health_for_candidates",
    "reset_model_availability_cache",
    "parse_provider_chain",
    "parse_provider_tokens",
    "parse_provider_base_urls",
    "parse_model_list_response",
    # fallback
    "ProviderRateLimitError",
    "raise_for_provider_fallback_event",
    "poll_completion_with_task_guard",
    # rate_limit
    "opencode_log_dir",
    "debug_log_dir",
    "recent_log_files",
    "parse_rate_limit_payload",
    "detect_rate_limit_in_logs",
    # net
    "format_host_for_url",
    "build_base_url",
]

# The built-in implementation, registered on import so a consumer naming
# "opencode" in configuration does not have to register anything. Registered
# here rather than in `registry`, which imports no implementation on purpose.
register_harness("opencode", create_opencode_harness)
register_diagnostics_provider(
    "opencode",
    lambda _spec, options: OpenCodeSessionDiagnostics(options.get("database_path")),
)
