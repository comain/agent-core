"""Injectable harness configuration.

Ported from ``uta/config.py`` (the subset the OpenCode harness actually reads).

Harness code must never import a settings singleton directly. It calls
:func:`current_config`, which resolves through a :class:`~contextvars.ContextVar`
so a consumer can scope configuration per task or per thread with
:func:`use_config`. When nothing is scoped, the module-level :data:`settings`
instance is returned.

See ADR-002 in the dev-flow-agent repo for why resolution works this way rather
than threading a config parameter through every function.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import Any, Callable, Dict, Iterator, Literal, Optional, TypeVar

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

T = TypeVar("T")

_SECRET_FIELDS = frozenset(
    {
        "openai_api_key",
        "deepseek_api_key",
        "tencent_api_key",
        "gemini_api_key",
        "openrouter_api_key",
        "opencode_provider_tokens",
        "opencode_credential_env",
        "opencode_server_password",
    }
)


class HarnessConfig(BaseSettings):
    """Flat configuration for the OpenCode harness.

    Flat rather than grouped by concern: it keeps the diff against the original
    call sites minimal, which is what makes migrating existing consumers cheap.

    Subclass to change the environment prefix::

        class CrHarnessConfig(HarnessConfig):
            model_config = SettingsConfigDict(env_prefix="CR_", extra="ignore")
    """

    # ``populate_by_name`` is required for injection: several credential fields
    # carry an explicit alias, and without it ``HarnessConfig(openai_api_key=...)``
    # silently yields None because pydantic accepts the alias only. The source
    # never needed this -- it loaded from the environment exclusively -- but
    # constructing configuration in code is the point of this package.
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    # -- OpenCode server / process ------------------------------------------
    opencode_port: int = 4096
    opencode_host: str = "127.0.0.1"
    opencode_bin: Optional[str] = None
    opencode_spawn_cmd: Optional[str] = None
    opencode_serve_cmd: Optional[str] = None
    opencode_attach_url: Optional[str] = None
    # None = probe the configured binary's `--version` and cache 1 or 2.
    # Pin when the binary cannot be probed, or when a consumer must not
    # follow PATH. Probe `opencode_bin`, not a bare `opencode` name.
    opencode_major: Optional[int] = None
    opencode_server_username: str = "opencode"
    opencode_server_password: str = ""
    opencode_data_home: str = ""
    opencode_server_debug: bool = False
    opencode_server_print_logs: bool = False
    opencode_server_log_to_file: bool = True

    # Keep runs isolated from user/global OpenCode plugins.
    opencode_pure: bool = True

    # NOTE: ``opencode_prompt_file_threshold_chars`` is deliberately NOT declared.
    # ``harness/process.py`` reads it via ``getattr(..., 0) or 60000``, and it is
    # undeclared in the source this was ported from. Because the settings model
    # ignores extra input, the environment variable has never had any effect --
    # the read always falls through to the literal 60000. Declaring it here would
    # silently activate a dead knob, which is a behaviour change no ported test
    # could catch. Leave it undeclared. See the C2 decision in the design doc.

    # -- Model / provider selection -----------------------------------------
    opencode_provider: str = "token-pool"
    opencode_model: str = "token-pool/gpt-5.5"
    opencode_small_model: str = "token-pool/gpt-5.5"
    opencode_variant: str = ""
    opencode_selection_mode: Literal["manual", "discovery"] = "manual"
    # Trusted adapter options only, keyed by full model identity then variant.
    opencode_discovery_variants: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)
    opencode_provider_chain: str = "token-pool:token-pool/gpt-5.5"
    opencode_provider_tokens: str = ""
    opencode_credential_env: Dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    opencode_provider_base_urls: str = ""
    opencode_provider_fallback_enabled: bool = False
    opencode_model_api_timeout_seconds: int = 5
    opencode_model_api_cache_seconds: int = 300

    # Whether to pass --model on the command line. When False the model comes
    # only from the generated opencode.json, which lets OpenCode resolve it
    # through its own provider configuration rather than being told directly.
    #
    # Only safe to disable when every turn gets a config naming its model --
    # i.e. alongside a per-turn workspace. Without one, a fallback attempt would
    # inherit whatever config was last written.
    opencode_pass_model_flag: bool = True

    # Whether to pass --dangerously-skip-permissions.
    #
    # None means "decide from opencode_permissions": skip permission prompts
    # only when no permission policy is configured. This is deliberately
    # automatic -- the flag overrides the permission block entirely, so a
    # product that sets {"edit": "deny"} and forgot to also unset this would
    # silently get an agent that can edit anyway.
    opencode_skip_permissions: Optional[bool] = None

    @property
    def skip_permission_prompts(self) -> bool:
        if self.opencode_skip_permissions is not None:
            return self.opencode_skip_permissions
        return not self.opencode_permissions

    # -- Timeouts / liveness -------------------------------------------------
    opencode_active_timeout_multiplier: float = 2.0

    # Applied to every turn budget a consumer asks for.
    opencode_timeout_multiplier: float = 1.0

    # Per-provider multipliers, ``provider=multiplier`` separated by semicolons,
    # e.g. ``"deepseek=2.0;openrouter=1.5"``. Data rather than a predicate per
    # vendor: a consumer decides what a phase is worth, and how much longer one
    # provider needs to deliver it is not something the consumer should know.
    opencode_provider_timeout_multipliers: str = ""
    # Bound time-to-first meaningful model output independently from the full
    # coding-turn budget. A process that only creates a session shell must not
    # hold a worker for the entire repair timeout.
    opencode_initial_output_timeout_seconds: int = 180
    opencode_stalled_no_progress_seconds: int = 900
    opencode_stream_idle_timeout_seconds: int = 900

    # -- Turn logging --------------------------------------------------------
    opencode_turn_log_enabled: bool = True
    # Deviation D3: neutral cache path (source default was ``.uta_cache/...``).
    opencode_turn_log_dir: str = ".agent_cache/opencode_turns"

    # Deviation D5: the source hard-coded ``.uta_cache`` as the repo-local cache
    # directory. 63 sites in that repo still read that path, so it cannot simply
    # be renamed -- it becomes configurable, defaulting to a neutral name, and a
    # migrating consumer overrides it back to its own legacy value.
    agent_cache_dir: str = ".agent_cache"

    # Where the consumer writes agent run logs. The harness scans it for a
    # provider's rate-limit response, so it has to match wherever the
    # consumer's own CLI writes them -- a mismatch loses the evidence
    # silently. Absolute, or a bare name under the system temp directory.
    agent_debug_log_dir: str = ""

    # Extra keys merged into opencode.json's "permission" block. Empty by
    # default, which reproduces the upstream behaviour of permitting everything.
    #
    # A product whose agent must not modify the checkout sets {"edit": "deny"}.
    # A code reviewer that can edit the code it is reviewing is a real hazard,
    # and there is no way to express that upstream -- which is why this exists.
    opencode_permissions: Dict[str, Any] = Field(default_factory=dict)

    # Extra directories granted read access, beyond the repository itself.
    opencode_permission_dirs: str = ""

    # Whether to grant the built-in scratch directories (/tmp, the system temp
    # dir, ~/.m2). They exist because headless runs wedge when OpenCode asks for
    # approval on temp paths, and because JVM builds read the Maven cache.
    # A product that only *reads* code should turn them off rather than inherit
    # filesystem access it has no use for.
    opencode_default_external_dirs: bool = True

    # -- Workspace visibility ------------------------------------------------
    opencode_external_dirs: str = ""
    # Deviation D3: empty default. The source shipped deployment-specific paths.
    # The field is retained under its original name because the harness reads it
    # (``harness/config.py``: ``opencode_external_dirs or index_source_dirs``)
    # and a ported test patches it by name. Ownership of this field is shared
    # with the consumer and must be resolved when unit-test-agent migrates.
    index_source_dirs: str = ""

    # -- Provider credentials and endpoints ----------------------------------
    # Deviation D4: the source bound these to product-branded environment names
    # (``UTA_OPENAI_API_KEY``, ``UTA_BASE_URL``) that bypass ``env_prefix``.
    # A shared package cannot claim another product's namespace, so the branded
    # aliases are dropped and the vendor-standard names retained.
    openai_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("AGENT_OPENAI_API_KEY", "AGENT_OPENAI_KEY", "OPENAI_KEY"),
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("AGENT_BASE_URL", "OPENAI_BASE_URL"),
    )
    deepseek_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_KEY"),
    )
    tencent_api_key: Optional[str] = Field(default=None, alias="TENCENT_API_KEY")
    tencent_base_url: Optional[str] = Field(default=None, alias="TENCENT_BASE_URL")
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    ollama_host: Optional[str] = Field(default=None, alias="OLLAMA_HOST")
    ollama_num_ctx: int = Field(default=262144, alias="OLLAMA_NUM_CTX")

    # -- OpenRouter routing preferences --------------------------------------
    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_provider_only: str = ""
    openrouter_provider_order: str = ""
    openrouter_allow_fallbacks: bool = True
    openrouter_require_parameters: bool = False

    def __repr__(self) -> str:
        redacted = {
            name: ("***" if name in _SECRET_FIELDS and getattr(self, name) else getattr(self, name))
            for name in type(self).model_fields
        }
        inner = ", ".join(f"{key}={value!r}" for key, value in redacted.items())
        return f"{type(self).__name__}({inner})"

    __str__ = __repr__

    def model_dump(self, *args: Any, reveal_secrets: bool = False, **kwargs: Any) -> dict:
        """Dump the configuration, with secrets redacted unless asked.

        Redaction is the default because the overwhelmingly common use is
        showing this somewhere — a log line, an error, a status page — and a
        token that reaches any of those is a token to rotate.

        ``reveal_secrets=True`` exists because the other use is round-tripping:
        rebuilding a configuration from a dump, which is how a consumer hands
        its settings to a harness. A redacted dump is *plausible* there rather
        than obviously broken — every field is present and the token is a
        non-empty string — so the harness authenticates with ``"***"`` and the
        only symptom is a 401 from the provider, several layers away. That cost
        a day of beta debugging. Making the reveal explicit means the one call
        site that needs real values says so, and every other call stays safe.
        """
        data = super().model_dump(*args, **kwargs)
        if reveal_secrets:
            return data
        for name in _SECRET_FIELDS:
            if data.get(name):
                data[name] = "***"
        return data


#: Fallback instance used when no configuration is scoped.
_default = HarnessConfig()

_active: ContextVar[Optional[HarnessConfig]] = ContextVar("agent_core_harness_config", default=None)


def current_config() -> HarnessConfig:
    """Return the active configuration: scoped if set, else the module default."""
    return _active.get() or _default


def set_default_config(config: HarnessConfig) -> HarnessConfig:
    """Replace the fallback configuration for the whole process.

    :func:`use_config` scopes a configuration to a block and does not reach
    threads started outside it, which is the right tool for a per-task
    override. It is the wrong tool for a consumer whose *entire* process reads
    one configuration -- one that keeps its settings under its own environment
    prefix, say::

        class ProductHarnessConfig(HarnessConfig):
            model_config = SettingsConfigDict(env_prefix="PRODUCT_", extra="ignore")

        set_default_config(ProductHarnessConfig())

    Without this, such a consumer has to either wrap every entry point in
    ``use_config`` -- easy to miss one, and the miss is silent, because the
    default instance answers every read with a plausible wrong value -- or
    reach into the private module state.

    Returns the configuration it installed, so a caller can keep a reference
    without a second lookup. A scoped :func:`use_config` still wins while it
    is active.
    """
    global _default
    _default = config
    return config


class _ConfigProxy:
    """Attribute-forwarding view of whatever configuration is currently active.

    Harness modules do ``from agent_core.config import settings`` and read
    ``settings.X``, exactly as the source did. Each read resolves through
    :func:`current_config`, so scoping with :func:`use_config` takes effect
    without the harness holding a direct reference to any instance.

    This is what lets the port leave all 99 read sites untouched, and lets the
    ported tests keep both of their patching idioms -- replacing a module's
    ``settings`` name with a mock, and setting attributes on
    ``agent_core.config.settings``.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        # Deliberately not a blanket getattr: unknown names must raise
        # AttributeError so that `getattr(settings, "missing", default)` still
        # falls through to its default, as it does in the source.
        return getattr(current_config(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(current_config(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(current_config(), name)

    def __repr__(self) -> str:
        return repr(current_config())


#: Module-level configuration view. Reads and writes forward to the active
#: configuration, so this is safe to import at module scope.
settings = _ConfigProxy()


@contextmanager
def use_config(config: HarnessConfig) -> Iterator[HarnessConfig]:
    """Scope ``config`` for the duration of the block.

    Isolation follows :mod:`contextvars` rules, so it does *not* reach worker
    threads started inside the block. Wrap those with :func:`propagate`.
    """
    token = _active.set(config)
    try:
        yield config
    finally:
        _active.reset(token)


def propagate(func: Callable[..., T]) -> Callable[..., T]:
    """Carry the *currently scoped* configuration across a thread hop.

    A thread started by :class:`concurrent.futures.ThreadPoolExecutor` begins
    with an empty context, so a callable submitted from inside :func:`use_config`
    would otherwise silently fall back to the module default -- reading the wrong
    provider, model, or credentials with no error. Submit ``propagate(fn)``
    instead of ``fn``::

        with use_config(cfg):
            executor.submit(propagate(run_reviewer), task)

    The scoped configuration is captured once, when ``propagate`` is called, and
    re-applied on each invocation. It deliberately does **not** replay an entire
    :class:`~contextvars.Context`: a single captured ``Context`` cannot be
    entered by two threads at once, so reusing one wrapped callable across a
    pool -- the exact case this exists for -- would raise
    ``cannot enter context: ... is already entered``.
    """
    captured = _active.get()

    def _runner(*args: Any, **kwargs: Any) -> T:
        token = _active.set(captured)
        try:
            return func(*args, **kwargs)
        finally:
            _active.reset(token)

    return _runner
