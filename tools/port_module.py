#!/usr/bin/env python3
"""Port a harness module from unit-test-agent to agent-core.

The port is a mechanical rewrite of exactly three things:

1. ``from uta.config import settings``  ->  ``from agent_core.config import settings``
2. intra-package imports rewritten to the new package path

Read sites are NOT rewritten. ``agent_core.config.settings`` is a proxy that
forwards every attribute access to the currently active configuration, so
``settings.X`` and ``getattr(settings, ...)`` both resolve through the ContextVar
untouched. Read counts are reported only so the port can be audited.

Intra-package imports are rewritten to the new package path. Nothing else is
touched: any further change a ported module needs is a port defect to be
recorded, not smoothed over here.

Usage: port_module.py <module_name> [...]
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = pathlib.Path(os.environ.get("PORT_SRC", _ROOT.parent / "unit-test-agent" / "uta" / "opencode"))
DST = pathlib.Path(os.environ.get("PORT_DST", _ROOT / "src" / "agent_core" / "harness"))

SETTINGS_IMPORT = re.compile(r"^from uta\.config import settings$", re.M)
GETATTR_READ = re.compile(r"\bgetattr\(\s*settings\s*,")
DOT_READ = re.compile(r"\bsettings\.(?=[A-Za-z_])")
PKG_IMPORT = re.compile(r"\buta\.opencode\.")
PKG_FROM = re.compile(r"^from uta\.opencode import\b", re.M)

# Deviation D1: the source named a public helper and its runtime path after the
# product it lived in. A shared package cannot ship those, so the identifier is
# renamed everywhere it appears -- definition, deferred imports, call sites, and
# local variables. No test references the branded names.
# Prose carried over from the source that names the product it used to live in.
# Comments only -- no behaviour -- but a shared package should not document
# another product, and one of these actively documented the wrong env prefix
# after the port changed it.
PROSE_FIXES = (
    ("Prefer the provider-specific token from UTA_OPENCODE_PROVIDER_TOKENS",
     "Prefer the provider-specific token from AGENT_OPENCODE_PROVIDER_TOKENS"),
    ("When UTA targets native OpenCode OpenAI auth",
     "When the consumer targets native OpenCode OpenAI auth"),
    ("for provider auth flows (``uta connect``) and the ``/init`` slash bootstrap.",
     "for provider auth flows and the ``/init`` slash bootstrap."),
    ("# HTTP-based auth client \u2014 preserved for `uta connect`, `project_summary.py`",
     "# HTTP-based auth client \u2014 preserved for consumer auth flows"),
    ("Prepending it keeps Python self-checks aligned with UTA's verifier runtime.",
     "Prepending it keeps Python self-checks aligned with the consumer's verifier runtime."),
    ("ask Linux to stop it if UTA dies", "ask Linux to stop it if the parent dies"),
)

# Deviation D6: attachment support. `opencode run` accepts `-f/--file`, which is
# how an image reaches a vision model. The source harness never exposed it -- its
# workflows are text-only -- so this is additive: the parameter defaults to None
# and the emitted command is byte-identical when unused, which is why every
# ported test still exercises the original path unchanged.
D6_ATTACHMENTS = (
    (
        "        session_id: Optional[str],\n"
        "        model_id: Optional[str],\n"
        "        variant: Optional[str] = None,\n"
        "        repo_path: Optional[str] = None,\n"
        "    ) -> List[str]:",
        "        session_id: Optional[str],\n"
        "        model_id: Optional[str],\n"
        "        variant: Optional[str] = None,\n"
        "        repo_path: Optional[str] = None,\n"
        "        attachments: Optional[Sequence[str]] = None,\n"
        "    ) -> List[str]:",
    ),
    (
        "        session_id: Optional[str] = None,\n"
        "        model_id: Optional[str] = None,\n"
        "        variant: Optional[str] = None,\n"
        "        repo_path: str,\n"
        "        timeout: int = 3600,",
        "        session_id: Optional[str] = None,\n"
        "        model_id: Optional[str] = None,\n"
        "        variant: Optional[str] = None,\n"
        "        repo_path: str,\n"
        "        attachments: Optional[Sequence[str]] = None,\n"
        "        timeout: int = 3600,",
    ),
    (
        "from typing import Any, Callable, Dict, List, Optional",
        "from typing import Any, Callable, Dict, List, Optional, Sequence",
    ),
    # Attachments must follow the message. `-f` takes an array, so placing it
    # first makes yargs swallow the prompt as another filename -- the failure
    # reads "File not found: <your entire prompt>".
    (
        "        cmd.extend(_message_args(message, repo_path=repo_path))\n"
        "        return cmd",
        "        cmd.extend(_message_args(message, repo_path=repo_path))\n"
        "        for attachment in attachments or ():\n"
        "            cmd += [\"-f\", str(attachment)]\n"
        "        return cmd",
    ),
    # Pass-through from run_turn to _build_cmd.
    (
        "        cmd = self._build_cmd(\n"
        "            message,",
        "        cmd = self._build_cmd(\n"
        "            message,\n"
        "            attachments=attachments,",
    ),
)

# Deviation D19: recognise a transport failure inside a structured error event.
#
# D16 catches the case where OpenCode reports an unreachable provider on stderr.
# It can also report the same thing as a JSON error event, nested arbitrarily:
#
#   {"type":"error","error":{"name":"AI_RetryError",
#    "errors":[{"name":"AI_APICallError","cause":{"code":"ConnectionRefused"}}]}}
#
# Upstream's classifier inspects only the top level, so this was not
# fallback-eligible and the chain stopped on an unreachable provider.
D19_STRUCTURED_TRANSPORT = (
    (
        "def provider_failure_from_stderr(line: str):",
        '''def contains_transport_failure(value) -> bool:
    """Whether a transport marker appears anywhere in a nested error payload.

    Recursive because OpenCode nests the real cause under `errors[].cause.code`,
    and inspecting only the top level misses every retry-wrapped failure.
    """
    if isinstance(value, dict):
        return any(contains_transport_failure(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_transport_failure(item) for item in value)
    if isinstance(value, str):
        return any(marker in value for marker in PROVIDER_TRANSPORT_FAILURES)
    return False


def provider_failure_from_stderr(line: str):''',
    ),
    (
        "                fallback_reason = classify_provider_model_error(error_obj)\n",
        "                fallback_reason = classify_provider_model_error(error_obj)\n"
        "                if fallback_reason is None and contains_transport_failure(error_obj):\n"
        "                    # A retry-wrapped transport failure: the provider is\n"
        "                    # unreachable, so the next candidate is worth trying.\n"
        '                    fallback_reason = "provider_error"\n',
    ),
)


# Deviation D18: --dangerously-skip-permissions becomes conditional.
#
# Upstream passes it unconditionally, which overrides the permission block
# entirely -- so D12's configurable permissions were inert: a product setting
# {"edit": "deny"} still got an agent that could edit. Found because a consumer
# test asserts the flag is absent.
#
# Defaults to skipping only when no permission policy is configured, so the safe
# behaviour is automatic rather than another flag to remember.
D18_SKIP_PERMISSIONS = (
    (
        '        cmd = base + ["--print-logs", "--format", "json", "--dangerously-skip-permissions"]',
        '        cmd = base + ["--print-logs", "--format", "json"]\n'
        "        if settings.skip_permission_prompts:\n"
        '            cmd.append("--dangerously-skip-permissions")',
    ),
)


# Deviation D17: --model on the command line becomes optional.
#
# A consumer deliberately omits it and lets the generated opencode.json carry the
# model, so OpenCode resolves it through its own provider configuration instead
# of being told directly. Its tests assert the flag's absence, so this was a
# considered choice rather than an oversight.
#
# Default True, preserving upstream behaviour.
D17_MODEL_FLAG = (
    (
        "        if model_id:\n"
        '            cmd += ["--model", model_id]',
        "        if model_id and settings.opencode_pass_model_flag:\n"
        '            cmd += ["--model", model_id]',
    ),
)


# Deviation D16: detect a provider transport failure from stderr and fail fast.
#
# OpenCode reports an unreachable provider on stderr and then simply produces no
# output. Upstream waits for the stream-idle timeout before giving up, so a dead
# provider costs a full timeout *per model in the chain* -- and the turn is then
# classified `no_output`, which is indistinguishable from a slow model, so the
# provider-skip rule can never fire.
#
# Recognising the stderr line ends the turn immediately with `provider_error`,
# which is what makes fallback fast and makes should_skip_provider() work.
D16_STDERR_TRANSPORT = (
    (
        "def classify_provider_model_error(",
        '''#: stderr markers meaning OpenCode could not reach the provider at all.
PROVIDER_TRANSPORT_FAILURES = (
    "ConnectionRefused",
    "ECONNREFUSED",
    "ENOTFOUND",
    "EAI_AGAIN",
    "ETIMEDOUT",
    "ECONNRESET",
    "fetch failed",
    "socket hang up",
)


def provider_failure_from_stderr(line: str):
    """A provider-transport failure reported on stderr, or None.

    Requires both an OpenCode API-error marker and a transport marker: either
    alone produces false positives on ordinary provider noise.
    """
    if "AI_APICallError" not in line and "AI_RetryError" not in line:
        return None
    if not any(marker in line for marker in PROVIDER_TRANSPORT_FAILURES):
        return None
    return {"message": "opencode provider transport failure", "stderr": line[:4000]}


def classify_provider_model_error(''',
    ),
    (
        "                line = raw.decode(errors=\"replace\").rstrip()\n"
        "                stderr_lines.append(line)\n"
        "                record_raw_line(\"stderr\", line)\n",
        "                line = raw.decode(errors=\"replace\").rstrip()\n"
        "                stderr_lines.append(line)\n"
        "                record_raw_line(\"stderr\", line)\n"
        "                failure = provider_failure_from_stderr(line)\n"
        "                if failure is not None and provider_failure[0] is None:\n"
        "                    # Do not wait for the idle timeout: the provider is\n"
        "                    # unreachable and no output is coming.\n"
        "                    provider_failure[0] = failure\n",
    ),
    (
        "        events: List[Dict[str, Any]] = []\n"
        "        cancelled = False\n",
        "        events: List[Dict[str, Any]] = []\n"
        "        cancelled = False\n"
        "        provider_failure: List[Any] = [None]\n",
    ),
    (
        "            if is_cancelled is not None and is_cancelled():\n",
        "            if provider_failure[0] is not None:\n"
        "                _terminate_opencode_process(proc)\n"
        "                return finish_raw_log(TurnResult(\n"
        '                    type="error",\n'
        "                    model_id=model_id,\n"
        "                    session_id=collected_session_id,\n"
        "                    error=provider_failure[0],\n"
        "                    fallback_eligible=True,\n"
        '                    fallback_reason="provider_error",\n'
        "                ))\n"
        "            if is_cancelled is not None and is_cancelled():\n",
    ),
)


# Deviation D15: track spawned processes so a shutdown can reap them.
#
# Each turn is spawned into its own process group, which means it outlives the
# service unless something reaps it: a deploy sends SIGTERM, the service exits,
# and every in-flight OpenCode run keeps going, holding a provider connection
# and spending on a task nobody is waiting for. Upstream has no handling for
# this at all.
D15_TRACK_PROCESS = (
    (
        "        proc = subprocess.Popen(\n"
        "            cmd,\n"
        "            stdin=subprocess.DEVNULL,\n"
        "            stdout=subprocess.PIPE,\n"
        "            stderr=subprocess.PIPE,\n"
        "            cwd=repo_path,\n"
        "            env=_build_env(repo_path, model_id=model_id),\n"
        "            **_opencode_popen_kwargs(),\n"
        "        )\n"
        "        return self._read_stream(\n"
        "            proc,\n"
        "            timeout=timeout,\n"
        "            on_update=on_update,\n"
        "            model_id=model_id,\n"
        "            started_at=time.time(),\n"
        "            raw_log_path=_opencode_turn_log_path(repo_path, model_id=model_id),\n"
        "            is_cancelled=is_cancelled,\n"
        "        )",
        "        proc = subprocess.Popen(\n"
        "            cmd,\n"
        "            stdin=subprocess.DEVNULL,\n"
        "            stdout=subprocess.PIPE,\n"
        "            stderr=subprocess.PIPE,\n"
        "            cwd=repo_path,\n"
        "            env=_build_env(repo_path, model_id=model_id),\n"
        "            **_opencode_popen_kwargs(),\n"
        "        )\n"
        "        with _track_process(proc):\n"
        "            return self._read_stream(\n"
        "                proc,\n"
        "                timeout=timeout,\n"
        "                on_update=on_update,\n"
        "                model_id=model_id,\n"
        "                started_at=time.time(),\n"
        "                raw_log_path=_opencode_turn_log_path(repo_path, model_id=model_id),\n"
        "                is_cancelled=is_cancelled,\n"
        "            )",
    ),
    (
        "from agent_core.config import settings",
        "from agent_core.config import settings\n"
        "from agent_core.harness.shutdown import track as _track_process",
    ),
)


# Deviation D7: cooperative cancellation.
#
# The source harness has no way to stop a turn in flight. One consumer's fork
# added `is_cancelled` and wires it to its stop/cancel control channel, so
# without this a migrated product silently loses the ability to stop a running
# review -- the operator presses stop and the model keeps working.
#
# Discovered by attempting the migration rather than by reading LOC counts: the
# spec had recorded that fork as a "trimmed adaptation" of the source, which was
# true by module count and false by capability.
D7_CANCELLATION = (
    # run_turn accepts the predicate
    (
        "        repo_path: str,\n"
        "        attachments: Optional[Sequence[str]] = None,\n"
        "        timeout: int = 3600,",
        "        repo_path: str,\n"
        "        attachments: Optional[Sequence[str]] = None,\n"
        "        timeout: int = 3600,\n"
        "        is_cancelled: Optional[Callable[[], bool]] = None,",
    ),
    # and hands it to the reader. Appended after the existing keywords: the call
    # opens with positional arguments, so inserting at the top produces
    # "positional argument follows keyword argument".
    (
        "            raw_log_path=_opencode_turn_log_path(repo_path, model_id=model_id),\n"
        "        )",
        "            raw_log_path=_opencode_turn_log_path(repo_path, model_id=model_id),\n"
        "            is_cancelled=is_cancelled,\n"
        "        )",
    ),
    (
        "        model_id: Optional[str] = None,\n"
        "        started_at: Optional[float] = None,\n"
        "        raw_log_path: Optional[Path] = None,\n"
        "    ) -> TurnResult:",
        "        model_id: Optional[str] = None,\n"
        "        started_at: Optional[float] = None,\n"
        "        raw_log_path: Optional[Path] = None,\n"
        "        is_cancelled: Optional[Callable[[], bool]] = None,\n"
        "    ) -> TurnResult:",
    ),
    # polled once per loop iteration, before any timeout arithmetic
    (
        "        while stdout_thread.is_alive() or stderr_thread.is_alive():\n",
        "        while stdout_thread.is_alive() or stderr_thread.is_alive():\n"
        "            if is_cancelled is not None and is_cancelled():\n"
        "                _terminate_opencode_process(proc)\n"
        "                cancelled = True\n"
        "                break\n",
    ),
    (
        "        events: List[Dict[str, Any]] = []\n"
        "        collected_session_id: Optional[str] = None\n",
        "        events: List[Dict[str, Any]] = []\n"
        "        cancelled = False\n"
        "        collected_session_id: Optional[str] = None\n",
    ),
    # A cancelled turn is its own terminal state. Crucially it must NOT be
    # fallback_eligible: an operator pressing stop would otherwise roll on to the
    # next provider in the chain and keep spending.
    (
        "        try:\n"
        "            proc.wait(timeout=5)\n"
        "        except subprocess.TimeoutExpired:\n"
        "            _terminate_opencode_process(proc)\n",
        "        try:\n"
        "            proc.wait(timeout=5)\n"
        "        except subprocess.TimeoutExpired:\n"
        "            _terminate_opencode_process(proc)\n"
        "\n"
        "        if cancelled:\n"
        "            return finish_raw_log(TurnResult(\n"
        "                type=\"cancelled\",\n"
        "                model_id=model_id,\n"
        "                session_id=collected_session_id,\n"
        "                error={\"message\": \"task cancelled by operator\"},\n"
        "                fallback_eligible=False,\n"
        "            ))\n",
    ),
)

# Deviation D10: a prompt file is a first-class input, preferred over a string.
#
# The source only writes a prompt file above a size threshold -- and that
# threshold reads an undeclared setting (see C2), so in practice it is always
# 60000 and short prompts always travel as an argv string. A consumer's fork
# instead has the caller supply the file, which is the better contract:
#
#   * argv has an OS length limit, so a long prompt can fail exec outright
#   * argv is world-readable in `ps` output, so prompt content leaks to any
#     user on the host
#   * the file is a durable artifact, which is what makes a failed turn
#     reproducible after the fact
#
# run_turn now accepts either `message` or `prompt_file`, exactly one.
D10_PROMPT_FILE = (
    (
        "    def run_turn(\n"
        "        self,\n"
        "        message: str,\n"
        "        *,\n",
        "    def run_turn(\n"
        "        self,\n"
        "        message: Optional[str] = None,\n"
        "        *,\n"
        "        prompt_file: Optional[Path] = None,\n",
    ),
    (
        "        cmd = self._build_cmd(\n"
        "            message,\n"
        "            attachments=attachments,",
        "        if (message is None) == (prompt_file is None):\n"
        "            raise ValueError(\"pass exactly one of message= or prompt_file=\")\n"
        "        cmd = self._build_cmd(\n"
        "            message,\n"
        "            prompt_file=prompt_file,\n"
        "            attachments=attachments,",
    ),
    (
        "        repo_path: Optional[str] = None,\n"
        "        attachments: Optional[Sequence[str]] = None,\n"
        "    ) -> List[str]:",
        "        repo_path: Optional[str] = None,\n"
        "        attachments: Optional[Sequence[str]] = None,\n"
        "        prompt_file: Optional[Path] = None,\n"
        "    ) -> List[str]:",
    ),
    (
        "        cmd.extend(_message_args(message, repo_path=repo_path))\n",
        "        if prompt_file is not None:\n"
        "            cmd.extend(_prompt_file_args(Path(prompt_file)))\n"
        "        else:\n"
        "            cmd.extend(_message_args(message or \"\", repo_path=repo_path))\n",
    ),
    (
        "def _write_prompt_file(repo_path: str, message: str) -> Path:",
        '''def _prompt_file_args(prompt_path: Path) -> List[str]:
    """Reference a caller-supplied prompt file.

    Same shape as the oversized-message branch: a short instruction, then the
    file. `--file` is an array option, so the instruction must come first or the
    CLI consumes it as another path.
    """
    return [
        f"Read and follow the attached prompt file exactly: {prompt_path.name}",
        "--file",
        str(prompt_path),
    ]


def _write_prompt_file(repo_path: str, message: str) -> Path:''',
    ),
)


# Deviation D9: capabilities a consumer's fork has that the source lacks.
#
# Found by a systematic symbol-level diff during migration rather than one at a
# time. All four are generic -- nothing product-specific -- and their absence
# would have forced the migrating consumer to keep its own copy of the module
# purely for two methods.
D9_STREAM = (
    (
        "    def extract_tokens(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:",
        '''    def extract_session_id(self, events: List[Dict[str, Any]]) -> Optional[str]:
        """First session id seen, from the event or its part."""
        for event in events:
            if not isinstance(event, dict):
                continue
            value = event.get("sessionID")
            if isinstance(value, str) and value:
                return value
            part = event.get("part") or {}
            value = part.get("sessionID")
            if isinstance(value, str) and value:
                return value
        return None

    def extract_cost(self, events: List[Dict[str, Any]]) -> Optional[float]:
        """Summed cost across events, or None when no event reported one.

        None and 0.0 are different: a provider that reports no cost must not be
        recorded as having been free.
        """
        total = 0.0
        saw_cost = False
        for event in events:
            if not isinstance(event, dict):
                continue
            for value in (event.get("cost"), (event.get("part") or {}).get("cost")):
                if value is None:
                    continue
                try:
                    total += float(value)
                    saw_cost = True
                except (TypeError, ValueError):
                    continue
        return total if saw_cost else None

    def extract_tokens(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:''',
    ),
    # Hardening from the same fork: json.loads can return a list or a scalar,
    # and returning that from parse_line propagates a non-dict into every caller
    # that does event.get(...).
    (
        "        try:\n"
        "            return json.loads(line)\n"
        "        except json.JSONDecodeError:\n"
        "            return None",
        "        try:\n"
        "            loaded = json.loads(line)\n"
        "        except json.JSONDecodeError:\n"
        "            return None\n"
        "        return loaded if isinstance(loaded, dict) else None",
    ),
)

D9_ROUTING = (
    # A default argument binds httpx.get at import time, so the usual
    # monkeypatch("httpx.get") never takes effect and an availability probe
    # cannot be stubbed by a consumer's tests. Resolve it at call time instead.
    (
        "def _provider_available_models(\n"
        "    provider_id: str,\n"
        "    *,\n"
        "    http_get: Callable[..., Any] = httpx.get,\n"
        ") -> Optional[Set[str]]:\n"
        "    url = _model_api_url(provider_id)",
        "def _provider_available_models(\n"
        "    provider_id: str,\n"
        "    *,\n"
        "    http_get: Optional[Callable[..., Any]] = None,\n"
        ") -> Optional[Set[str]]:\n"
        "    http_get = http_get or httpx.get\n"
        "    url = _model_api_url(provider_id)",
    ),
    (
        "def available_provider_candidates(\n"
        "    *,\n"
        "    fallback_enabled: Optional[bool] = None,\n"
        "    http_get: Callable[..., Any] = httpx.get,\n"
        ") -> List[ProviderCandidate]:",
        "def available_provider_candidates(\n"
        "    *,\n"
        "    fallback_enabled: Optional[bool] = None,\n"
        "    http_get: Optional[Callable[..., Any]] = None,\n"
        ") -> List[ProviderCandidate]:",
    ),
    # A non-positive timeout means "do not probe" rather than "probe with a zero
    # timeout". Without this the probe fires whenever a base URL is configured,
    # so a test suite silently makes live HTTP calls and its results depend on
    # which earlier test warmed the cache. It also gives an operator a way to
    # switch the probe off in an environment where the model API is unreachable.
    (
        "    url = _model_api_url(provider_id)\n"
        "    if not url:\n"
        "        return None\n",
        "    if float(settings.opencode_model_api_timeout_seconds or 0) <= 0:\n"
        "        return None\n"
        "    url = _model_api_url(provider_id)\n"
        "    if not url:\n"
        "        return None\n",
    ),
    (
        "_tracker = ModelHealthTracker()",
        '''_tracker = ModelHealthTracker()


def is_model_healthy(model_id: str) -> bool:
    """Whether a model is outside its rate-limit cooldown."""
    return _tracker.is_healthy(model_id)


def reset_model_health() -> None:
    """Clear all cooldowns. Primarily for tests and operator recovery."""
    _tracker.reset()''',
    ),
)


# Deviation D8b: populate the fields D8 added.
#
# Declaring model_id and cost_usd is not enough -- the success path must fill
# them, or cost accounting silently reads None and a chain retry cannot be
# attributed to the model that actually answered.
D8_POPULATE = (
    # The raw turn log is what a post-hoc investigation reads, so it must carry
    # the fields D8 added. Serialising a TurnResult minus its cost is how a cost
    # question becomes unanswerable after the fact.
    (
        '                "result_type": result.type,\n'
        '                "session_id": result.session_id,\n'
        '                "tokens": result.tokens,\n',
        '                "result_type": result.type,\n'
        '                "session_id": result.session_id,\n'
        '                "model_id": result.model_id,\n'
        '                "cost_usd": result.cost_usd,\n'
        '                "tokens": result.tokens,\n',
    ),
    # There are TWO completed returns: this fast path for an explicit terminal
    # "stop" -- which is what a well-behaved turn actually takes -- and the
    # general one below. Patching only the second left cost and model dropped on
    # the common path, which a consumer's integration test caught.
    (
        '            return TurnResult(\n'
        '                type="completed",\n'
        "                result=result_text,\n"
        "                session_id=session_id,\n"
        "                tokens=tokens,\n"
        "                patch_count=patch_count,\n"
        "            )",
        '            return TurnResult(\n'
        '                type="completed",\n'
        "                result=result_text,\n"
        "                session_id=session_id,\n"
        "                model_id=model_id,\n"
        "                cost_usd=self._parser.extract_cost(events),\n"
        "                tokens=tokens,\n"
        "                patch_count=patch_count,\n"
        "            )",
    ),
    (
        '        return TurnResult(\n'
        '            type="completed",\n'
        "            result=result_text,\n"
        "            session_id=session_id,\n"
        "            tokens=tokens,\n"
        "        )",
        '        return TurnResult(\n'
        '            type="completed",\n'
        "            result=result_text,\n"
        "            session_id=session_id,\n"
        "            model_id=model_id,\n"
        "            cost_usd=self._parser.extract_cost(events),\n"
        "            tokens=tokens,\n"
        "        )",
    ),
)


# Deviation D8: TurnResult carries model_id and cost_usd.
#
# Both are present in one consumer's fork and absent from the source. model_id
# is needed to attribute a result when a provider chain retried across models;
# cost_usd is what that product's cost accounting reads. Additive defaults, so
# nothing that ignores them changes.
D8_RESULT_FIELDS = (
    (
        "    type: str  # \"completed\" | \"error\" | \"rate_limited\" | \"timeout\"\n"
        "    result: str = \"\"\n"
        "    session_id: Optional[str] = None\n",
        "    type: str  # \"completed\" | \"error\" | \"rate_limited\" | \"timeout\" | \"cancelled\"\n"
        "    result: str = \"\"\n"
        "    session_id: Optional[str] = None\n"
        "    model_id: Optional[str] = None\n"
        "    cost_usd: Optional[float] = None\n",
    ),
)

D1_RENAMES = (
    ("uta_debug_log_dir", "debug_log_dir"),
    ("uta_log_dir", "debug_dir"),
    ("uta-run-logs", "agent-run-logs"),
)

# Deviation D5: two product-branded values are cross-repo contracts, not internal
# names -- the source's Python verifier reads UTA_SERVICE_PYTHON_BIN, and 63 sites
# across that repo read `.uta_cache`. Renaming outright would silently break them
# when they migrate. Both names are emitted during a compatibility window instead;
# the legacy ones are dropped once every consumer has migrated.
D5_ENV_VAR = (
    '        env.setdefault("UTA_SERVICE_PYTHON_BIN", str(Path(sys.executable).resolve()))',
    '        resolved_python = str(Path(sys.executable).resolve())\n'
    '        env.setdefault("AGENT_SERVICE_PYTHON_BIN", resolved_python)\n'
    '        # Compatibility window (D5): the source repo\'s Python verifier reads the\n'
    '        # legacy name. Emit both until that consumer migrates, then drop this.\n'
    '        env.setdefault("UTA_SERVICE_PYTHON_BIN", resolved_python)',
)
# Deviation D11: split config construction from writing it, and allow a per-turn
# model override.
#
# `generate_opencode_config` builds a dict and writes it to <repo>/opencode.json
# in one step, which forces every turn in a repository to share one config file.
# Concurrent turns wanting different models then overwrite each other and the
# last writer decides what all of them run. Exposing the builder lets a turn get
# a private config (see harness/workspace.py) without touching the repo root.
D11_CONFIG_SPLIT = (
    (
        "def generate_opencode_config(repo_path: str):\n"
        '    """Write a project-level opencode.json with the provider-chain model config.',
        "def build_opencode_config_dict(repo_path: str, *, model_id: Optional[str] = None) -> dict:\n"
        '    """Build the opencode.json contents.',
    ),
    # per-turn override: the selected model is otherwise global config
    (
        "        model = explicit\n"
        "    small_model = model",
        "        model = explicit\n"
        "    if model_id:\n"
        "        model = model_id\n"
        "    small_model = model",
    ),
    (
        '    config_path = Path(repo_path) / "opencode.json"\n'
        "    with open(config_path, \"w\") as f:\n"
        "        json.dump(config, f, indent=2)\n"
        "    return config_path",
        "    return config\n"
        "\n"
        "\n"
        "def generate_opencode_config(repo_path: str, *, model_id: Optional[str] = None):\n"
        '    """Write a project-level opencode.json into the repository root.\n'
        "\n"
        "    Shared by every turn in that repository. For concurrent turns needing\n"
        "    different models, use ``harness.workspace.per_turn_workspace`` instead.\n"
        '    """\n'
        "    config = build_opencode_config_dict(repo_path, model_id=model_id)\n"
        '    config_path = Path(repo_path) / "opencode.json"\n'
        "    with open(config_path, \"w\") as f:\n"
        "        json.dump(config, f, indent=2)\n"
        "    return config_path",
    ),
)


# Deviation D12: the permission block is configurable.
#
# Upstream emits only `permission.external_directory`, leaving every other
# capability at OpenCode's default. A consumer whose agent reviews code rather
# than writing it needs {"edit": "deny"} -- a reviewer able to modify the code it
# is reviewing is a real hazard, and upstream cannot express it.
#
# Empty by default, so upstream behaviour is unchanged; a product opts in.
D12_PERMISSIONS = (
    (
        '        "permission": {\n'
        '            "external_directory": _external_directory_permissions(repo_path),\n'
        "        },",
        '        "permission": _permission_block(repo_path),',
    ),
    (
        '    permissions = {}\n'
        "\n"
        "    # Headless runs can wedge when OpenCode asks for approval on temp scratch paths.\n"
        '    permissions["/tmp/**"] = "allow"\n'
        '    permissions[f"{Path(tempfile.gettempdir()).resolve()}/**"] = "allow"\n'
        '    permissions[f"{(Path.home() / \'.m2\').resolve()}/**"] = "allow"\n',
        '    permissions = {}\n'
        "\n"
        "    # Headless runs can wedge when OpenCode asks for approval on temp scratch\n"
        "    # paths, and JVM builds read the Maven cache. A product that only reads\n"
        "    # code turns these off rather than inheriting access it has no use for.\n"
        "    if settings.opencode_default_external_dirs:\n"
        '        permissions["/tmp/**"] = "allow"\n'
        '        permissions[f"{Path(tempfile.gettempdir()).resolve()}/**"] = "allow"\n'
        '        permissions[f"{(Path.home() / \'.m2\').resolve()}/**"] = "allow"\n',
    ),
    (
        "def _external_directory_permissions(",
        "def _permission_block(repo_path: str) -> dict:\n"
        '    """Assemble the opencode.json permission block.\n'
        "\n"
        "    The repository and any configured extra directories are readable;\n"
        "    everything else comes from ``opencode_permissions``, which a product\n"
        "    sets to constrain what its agent may do.\n"
        '    """\n'
        "    permission = dict(settings.opencode_permissions or {})\n"
        "    external = _external_directory_permissions(repo_path)\n"
        '    for raw in (settings.opencode_permission_dirs or "").split(","):\n'
        "        entry = raw.strip()\n"
        "        if entry:\n"
        '            external[f"{Path(entry).expanduser().resolve()}/**"] = "allow"\n'
        "    # Merge rather than replace: a product may also name external dirs.\n"
        '    configured = permission.get("external_directory")\n'
        "    if isinstance(configured, dict):\n"
        "        external.update(configured)\n"
        '    permission["external_directory"] = external\n'
        "    return permission\n"
        "\n"
        "\n"
        "def _external_directory_permissions(",
    ),
)

# Deviation D13: model selection walks the configured chain in order.
#
# Upstream prefers ``opencode_model`` whenever it appears anywhere in the chain,
# so an explicitly named model jumps ahead of earlier entries. The intended
# semantics are simpler, and are what a consumer's fork implemented: take chain
# candidates in order, use the first not marked unusable.
D13_CHAIN_ORDER = (
    # When the single-candidate view yields nothing usable, fall back to the
    # whole chain filtered by health -- not to the unfiltered chain, which would
    # reintroduce the very model that was marked unusable.
    (
        "    selected = available_provider_candidates(fallback_enabled=False)\n"
        "    if not selected:\n"
        "        selected = provider_candidates(fallback_enabled=False)",
        "    selected = available_provider_candidates(fallback_enabled=False)\n"
        "    if not selected:\n"
        "        selected = [\n"
        "            candidate\n"
        "            for candidate in chain\n"
        "            if is_model_healthy(opencode_model_id(candidate))\n"
        "        ]\n"
        "    if not selected:\n"
        "        # Everything is marked unusable; naming the first link beats\n"
        "        # emitting no model at all.\n"
        "        selected = provider_candidates(fallback_enabled=False)",
    ),
    (
        "from agent_core.harness.tiered_router import (\n"
        "    available_provider_candidates,\n",
        "from agent_core.harness.tiered_router import (\n"
        "    available_provider_candidates,\n"
        "    is_model_healthy,\n",
    ),
    (
        "    if settings.opencode_model in chain_models:\n"
        "        matching = next(\n"
        "            (\n"
        "                candidate\n"
        "                for candidate in chain\n"
        "                if settings.opencode_model in {candidate.model, opencode_model_id(candidate)}\n"
        "            ),\n"
        "            None,\n"
        "        )\n"
        "        model = opencode_model_id(matching) if matching else settings.opencode_model\n"
        "    elif selected:\n"
        "        model = opencode_model_id(selected[0])\n"
        "    else:\n"
        "        model = settings.opencode_model",
        "    # D13: walk the configured chain in order and take the first candidate\n"
        "    # not marked unusable. An explicitly selected model still wins, but only\n"
        "    # while it is itself usable -- otherwise pinning a model that has been\n"
        "    # rate-limited or withdrawn would strand the task on it rather than\n"
        "    # falling through to the next link.\n"
        "    explicit = settings.opencode_model\n"
        "    explicit_candidate = next(\n"
        "        (\n"
        "            candidate\n"
        "            for candidate in chain\n"
        "            if explicit in {candidate.model, opencode_model_id(candidate)}\n"
        "        ),\n"
        "        None,\n"
        "    )\n"
        "    if explicit_candidate is not None and is_model_healthy(\n"
        "        opencode_model_id(explicit_candidate)\n"
        "    ):\n"
        "        model = opencode_model_id(explicit_candidate)\n"
        "    elif selected:\n"
        "        model = opencode_model_id(selected[0])\n"
        "    elif explicit in chain_models:\n"
        "        matching = next(\n"
        "            (\n"
        "                candidate\n"
        "                for candidate in chain\n"
        "                if explicit in {candidate.model, opencode_model_id(candidate)}\n"
        "            ),\n"
        "            None,\n"
        "        )\n"
        "        model = opencode_model_id(matching) if matching else explicit\n"
        "    else:\n"
        "        model = explicit",
    ),
)


# Deviation D2: the source derived a repo root from package depth
# (``parents[2]``) and used it to locate a config file the *consumer* owns. Under
# this package's layout the identical expression resolves to ``src/`` -- still
# valid Python, silently different meaning. The depth derivation is removed and
# the path becomes consumer-relative.
D2_PROJECT_ROOT = (
    'PROJECT_ROOT = Path(__file__).resolve().parents[2]\n'
    'EXTERNAL_DIRS_CONFIG = PROJECT_ROOT / "config" / "opencode_external_dirs.json"',
    '# D2: resolved against the consumer\'s working directory, not this package\'s\n'
    '# location. Deriving it from __file__ depth would point inside the installed\n'
    '# package. Consumers that keep the file elsewhere override this attribute.\n'
    'EXTERNAL_DIRS_CONFIG = Path("config") / "opencode_external_dirs.json"',
)

D5_CACHE = (
    ('    root = Path(repo_path) / ".uta_cache" / "opencode" / "prompts"',
     '    root = Path(repo_path) / current_config().agent_cache_dir / "opencode" / "prompts"'),
    ('    configured = Path(settings.opencode_turn_log_dir or ".uta_cache/opencode_turns")',
     '    configured = Path(\n'
     '        settings.opencode_turn_log_dir\n'
     '        or f"{current_config().agent_cache_dir}/opencode_turns"\n'
     '    )'),
)


def port(name: str) -> tuple[int, int]:
    text = (SRC / f"{name}.py").read_text()

    text = SETTINGS_IMPORT.sub("from agent_core.config import settings", text)
    n_getattr = len(GETATTR_READ.findall(text))
    n_dot = len(DOT_READ.findall(text))
    text = PKG_IMPORT.sub("agent_core.harness.", text)
    text = PKG_FROM.sub("from agent_core.harness import", text)
    for old, new in D1_RENAMES:
        text = text.replace(old, new)
    for old, new in PROSE_FIXES:
        text = text.replace(old, new)
    if name == "stream":
        for old, new in D9_STREAM:
            assert old in text, f"D9 stream anchor not found: {old[:50]!r}"
            text = text.replace(old, new, 1)
    if name == "tiered_router":
        for old, new in D9_ROUTING:
            assert old in text, f"D9 routing anchor not found: {old[:50]!r}"
            text = text.replace(old, new, 1)
    if name == "config":
        for old, new in D12_PERMISSIONS:
            assert old in text, f"D12 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D13_CHAIN_ORDER:
            assert old in text, f"D13 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D11_CONFIG_SPLIT:
            assert old in text, f"D11 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        assert D2_PROJECT_ROOT[0] in text, "D2 anchor not found"
        text = text.replace(*D2_PROJECT_ROOT)
    if name == "process":
        for old, new in D6_ATTACHMENTS:
            assert old in text, f"D6 anchor not found: {old[:60]!r}"
            text = text.replace(old, new)
        for old, new in D7_CANCELLATION:
            assert old in text, f"D7 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D10_PROMPT_FILE:
            assert old in text, f"D10 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D18_SKIP_PERMISSIONS:
            assert old in text, f"D18 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D17_MODEL_FLAG:
            assert old in text, f"D17 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D16_STDERR_TRANSPORT:
            assert old in text, f"D16 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D19_STRUCTURED_TRANSPORT:
            assert old in text, f"D19 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D15_TRACK_PROCESS:
            assert old in text, f"D15 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D8_POPULATE:
            assert old in text, f"D8b anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        for old, new in D8_RESULT_FIELDS:
            assert old in text, f"D8 anchor not found: {old[:60]!r}"
            text = text.replace(old, new, 1)
        assert D5_ENV_VAR[0] in text, "D5 env-var anchor not found"
        text = text.replace(*D5_ENV_VAR)
        for old, new in D5_CACHE:
            assert old in text, f"D5 cache anchor not found: {old!r}"
            text = text.replace(old, new)
        text = text.replace(
            "from agent_core.config import settings",
            "from agent_core.config import current_config, settings",
        )

    (DST / f"{name}.py").write_text(text)
    return n_dot, n_getattr


def main() -> int:
    total_dot = total_getattr = 0
    for name in sys.argv[1:]:
        n_dot, n_getattr = port(name)
        total_dot += n_dot
        total_getattr += n_getattr
        print(f"ported {name}.py: {n_dot} dot-access + {n_getattr} getattr reads forwarded")
    print(f"TOTAL reads: {total_dot} dot-access + {total_getattr} getattr = {total_dot + total_getattr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
