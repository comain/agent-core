import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Optional
from agent_core.config import settings
from agent_core.harness.opencode_runtime import (
    is_v2,
    permission_rules_from_v1,
    providers_from_v1,
    v2_workspace_defaults,
)
from agent_core.harness.tiered_router import (
    available_provider_candidates,
    is_model_healthy,
    opencode_model_id,
    parse_provider_base_urls,
    parse_provider_chain,
    parse_provider_tokens,
    provider_local_model_id,
    provider_candidates,
)


def _provider_api_key(provider_id: str) -> str:
    """Resolve the apiKey opencode should use for a provider.

    Prefer the provider-specific token from AGENT_OPENCODE_PROVIDER_TOKENS
    (``<provider>.token=...``) so a configured token-pool/deepseek/etc. token
    actually authenticates that provider; fall back to the legacy per-provider
    settings (openai_api_key / deepseek_api_key / tencent_api_key) for
    deployments that still supply the key that way.
    """
    chain_token = parse_provider_tokens(settings.opencode_provider_tokens).get(provider_id)
    if chain_token:
        return chain_token
    if provider_id == "deepseek":
        return settings.deepseek_api_key or ""
    if provider_id == "tencent":
        return settings.tencent_api_key or ""
    return settings.openai_api_key or ""


# D2: resolved against the consumer's working directory, not this package's
# location. Deriving it from __file__ depth would point inside the installed
# package. Consumers that keep the file elsewhere override this attribute.
EXTERNAL_DIRS_CONFIG = Path("config") / "opencode_external_dirs.json"
GLOBAL_OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
GLOBAL_OPENCODE_PLUGIN_ROOT = Path.home() / ".config" / "opencode"
OPENCODE_PLUGIN_CACHE_ROOT = Path.home() / ".cache" / "opencode" / "packages"
CURSOR_PLUGIN_NAME = "opencode-cursor-oauth"


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _path_list(value: str) -> list[str]:
    normalized = (value or "").replace("\n", ",")
    if ":" in normalized:
        normalized = normalized.replace(":", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _permission_pattern(path_value: str) -> str:
    if "*" in path_value:
        return path_value
    return f"{Path(path_value).expanduser().resolve()}/**"


def _openrouter_model_options(model_id: str) -> dict:
    """OpenRouter reads routing controls from providerOptions.openrouter.provider."""
    only = _csv_list(settings.openrouter_provider_only)
    order = _csv_list(settings.openrouter_provider_order)
    if only == ["auto"] and "/" in model_id:
        only = [model_id.split("/", 1)[0]]
    if order == ["auto"] and "/" in model_id:
        order = [model_id.split("/", 1)[0]]
    provider: dict = {}

    if only:
        provider["only"] = only
        provider["order"] = order or only
    elif order:
        provider["order"] = order

    if not provider:
        return {}

    provider["allow_fallbacks"] = settings.openrouter_allow_fallbacks
    if settings.openrouter_require_parameters:
        provider["require_parameters"] = True
    return {"provider": provider}


def _model_limit(provider_id: str) -> dict:
    if provider_id == "google":
        return {
            "context": 1048576,
            "output": 65536,
        }
    if provider_id == "tencent":
        return {
            "context": 262144,
            "output": 65536,
        }
    if provider_id == "ollama":
        return {
            "context": settings.ollama_num_ctx,
            "output": 32768,
        }
    if provider_id == "openrouter":
        return {
            "context": 203000,
            "output": 65536,
        }
    if provider_id == "deepseek":
        return {
            "context": 1048576,
            "output": 65536,
        }
    if provider_id == "openai":
        return {
            "context": 272000,
            "output": 128000,
        }
    if provider_id == "cursor":
        return {
            "context": 262144,
            "output": 65536,
        }
    return {
        "context": 262144,
        "output": 32768,
    }


def _provider_base_url(provider_id: str) -> str:
    configured = parse_provider_base_urls(settings.opencode_provider_base_urls)
    if configured.get(provider_id):
        return configured[provider_id]
    if provider_id in {"openai", "token-pool"}:
        return (settings.openai_base_url or "").rstrip("/")
    if provider_id == "tencent":
        return (settings.tencent_base_url or "").rstrip("/")
    if provider_id == "ollama":
        ollama_host = (settings.ollama_host or "").rstrip("/")
        return f"{ollama_host}/v1" if ollama_host else ""
    if provider_id not in {"cursor", "deepseek", "google", "openrouter"}:
        return (settings.openai_base_url or "").rstrip("/")
    return ""


def _register_discovery_provider_models(config: dict, provider_id: str, *models: str) -> None:
    """Discovery inventories describe explicit OpenAI-compatible endpoints.

    A scope named 'google' is not permission to use Google's native adapter or
    ambient credentials. Register the same endpoint/key the runtime admitted.
    """
    base_url = parse_provider_base_urls(settings.opencode_provider_base_urls).get(provider_id)
    api_key = parse_provider_tokens(settings.opencode_provider_tokens).get(provider_id)
    if not base_url or not api_key:
        raise ValueError("discovery provider requires an explicit scoped endpoint and key")
    provider = config.setdefault("provider", {}).setdefault(provider_id, {
        "npm": "@ai-sdk/openai-compatible",
        "name": provider_id,
        "options": {"baseURL": base_url, "apiKey": api_key},
        "models": {},
    })
    for full_model in models:
        model_id = provider_local_model_id(provider_id, full_model)
        model = {"name": model_id, "limit": _model_limit(provider_id)}
        variants = settings.opencode_discovery_variants.get(f"{provider_id}/{model_id}")
        if variants:
            model["variants"] = deepcopy(variants)
        provider["models"][model_id] = model


def _register_provider_models(config: dict, provider_id: str, *models: str) -> None:
    provider = config.setdefault("provider", {}).setdefault(provider_id, {"models": {}})
    provider_base_url = _provider_base_url(provider_id)
    if provider_id == "tencent":
        tencent_base = provider_base_url or "https://tokenhub.tencentmaas.com/v1"
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "Tencent TokenHub")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", tencent_base)
        if _provider_api_key("tencent"):
            provider["options"].setdefault("apiKey", _provider_api_key("tencent"))
    if provider_id == "ollama":
        ollama_base = provider_base_url or f"{(settings.ollama_host or 'http://127.0.0.1:11434').rstrip('/')}/v1"
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "Ollama (local)")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", ollama_base)
    if provider_id == "openai" and provider_base_url:
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "OpenAI-compatible")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        if _provider_api_key("openai"):
            provider["options"].setdefault("apiKey", _provider_api_key("openai"))
    if provider_id == "deepseek" and provider_base_url:
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "DeepSeek OpenAI-compatible")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        if _provider_api_key("deepseek"):
            provider["options"].setdefault("apiKey", _provider_api_key("deepseek"))
    if (
        provider_id not in {"google", "openrouter", "deepseek", "tencent", "ollama", "openai", "cursor"}
        and provider_base_url
    ):
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", provider_id)
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        if _provider_api_key(provider_id):
            provider["options"].setdefault("apiKey", _provider_api_key(provider_id))
    if provider_id == "cursor":
        provider.setdefault("name", "Cursor")
    for full_model in models:
        model_id = provider_local_model_id(provider_id, full_model)
        if not model_id:
            continue
        provider["models"].setdefault(
            model_id,
            {
                "name": model_id,
                "limit": _model_limit(provider_id),
            },
        )
        if provider_id == "ollama":
            provider["models"][model_id].setdefault("options", {})
            provider["models"][model_id]["options"].setdefault("num_ctx", settings.ollama_num_ctx)
        if provider_id == "openrouter":
            options = _openrouter_model_options(model_id)
            if options:
                provider["models"][model_id].setdefault("options", {})
                provider["models"][model_id]["options"].update(options)


def _permission_block(repo_path: str) -> dict:
    """Assemble the opencode.json permission block.

    The repository and any configured extra directories are readable;
    everything else comes from ``opencode_permissions``, which a product
    sets to constrain what its agent may do.
    """
    permission = dict(settings.opencode_permissions or {})
    external = _external_directory_permissions(repo_path)
    for raw in (settings.opencode_permission_dirs or "").split(","):
        entry = raw.strip()
        if entry:
            external[f"{Path(entry).expanduser().resolve()}/**"] = "allow"
    # Merge rather than replace: a product may also name external dirs.
    configured = permission.get("external_directory")
    if isinstance(configured, dict):
        external.update(configured)
    permission["external_directory"] = external
    return permission


def _external_directory_permissions(repo_path: str) -> dict:
    permissions = {}

    # Headless runs can wedge when OpenCode asks for approval on temp scratch
    # paths, and JVM builds read the Maven cache. A product that only reads
    # code turns these off rather than inheriting access it has no use for.
    if settings.opencode_default_external_dirs:
        permissions["/tmp/**"] = "allow"
        permissions[f"{Path(tempfile.gettempdir()).resolve()}/**"] = "allow"
        permissions[f"{(Path.home() / '.m2').resolve()}/**"] = "allow"

    if EXTERNAL_DIRS_CONFIG.exists():
        data = json.loads(EXTERNAL_DIRS_CONFIG.read_text(encoding="utf-8"))
        for pattern in data.get("allow", []):
            permissions[str(pattern)] = "allow"

    configured_dirs = _path_list(settings.opencode_external_dirs or settings.index_source_dirs)
    for path_value in configured_dirs:
        permissions[_permission_pattern(path_value)] = "allow"

    return permissions


def _finalize_opencode_config(config: dict) -> dict:
    """Emit native V2 fields when the binary is 2.x; leave V1 files unchanged."""
    if not is_v2():
        return config
    permission = config.pop("permission", None)
    if isinstance(permission, dict):
        config["permissions"] = permission_rules_from_v1(permission)
    provider = config.pop("provider", None)
    if isinstance(provider, dict):
        config["providers"] = providers_from_v1(provider)
    config.pop("plugin", None)
    small_model = config.pop("small_model", None) or config.get("model")
    if small_model:
        config.setdefault("agents", {}).setdefault("title", {})["model"] = small_model
    config.update(v2_workspace_defaults())
    return config


def build_opencode_config_dict(repo_path: str, *, model_id: Optional[str] = None) -> dict:
    """Build the opencode.json contents.

    Registers provider/model metadata for all configured chain candidates so
    OpenCode can resolve provider-specific IDs such as `openrouter/z-ai/glm-5.1`.
    """
    chain = parse_provider_chain(settings.opencode_provider_chain)
    if settings.opencode_selection_mode == "discovery":
        model = model_id or settings.opencode_model
        if not chain or model not in {opencode_model_id(candidate) for candidate in chain}:
            raise ValueError("discovery model is outside the resolved chain")
        variant = settings.opencode_variant
        variants = settings.opencode_discovery_variants.get(model, {})
        if variant and (not variants.get(variant) or variants[variant].get("disabled") is True):
            raise ValueError("discovery variant requires explicit trusted options")
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "small_model": model,
            "permission": _permission_block(repo_path),
        }
        for candidate in chain:
            _register_discovery_provider_models(config, candidate.provider, candidate.model)
        return _finalize_opencode_config(config)
    selected = available_provider_candidates(fallback_enabled=False)
    if not selected:
        selected = [
            candidate
            for candidate in chain
            if is_model_healthy(opencode_model_id(candidate))
        ]
    if not selected:
        # Everything is marked unusable; naming the first link beats
        # emitting no model at all.
        selected = provider_candidates(fallback_enabled=False)
    chain_models = {
        model_id
        for candidate in chain
        for model_id in (candidate.model, opencode_model_id(candidate))
    }
    # D13: walk the configured chain in order and take the first candidate
    # not marked unusable. An explicitly selected model still wins, but only
    # while it is itself usable -- otherwise pinning a model that has been
    # rate-limited or withdrawn would strand the task on it rather than
    # falling through to the next link.
    explicit = settings.opencode_model
    explicit_candidate = next(
        (
            candidate
            for candidate in chain
            if explicit in {candidate.model, opencode_model_id(candidate)}
        ),
        None,
    )
    if explicit_candidate is not None and is_model_healthy(
        opencode_model_id(explicit_candidate)
    ):
        model = opencode_model_id(explicit_candidate)
    elif selected:
        model = opencode_model_id(selected[0])
    elif explicit in chain_models:
        matching = next(
            (
                candidate
                for candidate in chain
                if explicit in {candidate.model, opencode_model_id(candidate)}
            ),
            None,
        )
        model = opencode_model_id(matching) if matching else explicit
    else:
        model = explicit
    if model_id:
        model = model_id
    small_model = model

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": small_model,
        "permission": _permission_block(repo_path),
    }

    provider_models: dict[str, list[str]] = {}
    if chain:
        for candidate in chain:
            provider_models.setdefault(candidate.provider, []).append(candidate.model)
    else:
        for full_model in (model, settings.opencode_small_model):
            if "/" in full_model:
                provider_id = full_model.split("/", 1)[0]
            else:
                provider_id = settings.opencode_provider
            if provider_id:
                provider_models.setdefault(provider_id, []).append(full_model)
    providers = set(provider_models)
    for provider_id in sorted(providers):
        _register_provider_models(config, provider_id, *provider_models[provider_id])

    return _finalize_opencode_config(config)


def generate_opencode_config(repo_path: str, *, model_id: Optional[str] = None):
    """Write a project-level opencode.json into the repository root.

    Shared by every turn in that repository. For concurrent turns needing
    different models, use ``harness.workspace.per_turn_workspace`` instead.
    """
    config = build_opencode_config_dict(repo_path, model_id=model_id)
    config_path = Path(repo_path) / "opencode.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return config_path
