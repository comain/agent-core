"""Trusted host configuration shared by catalog refresh and agent workers."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .policy import ModelPolicy
from .sources import PriceWeights


class ProviderConfig(BaseModel):
    """A provider endpoint and nonsecret credential references, not key material."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$")
    base_url: str
    credential_scope_id: str = Field(min_length=1)
    credential_generation: str = Field(min_length=1)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    # Fraction of published list price actually billed through this endpoint:
    # 1 is full price, 0 an internal pool whose models cost the application
    # nothing and are therefore ranked by efficiency alone.
    pricing_discount: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        # Internal token pools intentionally use HTTP. Preserve the trusted host
        # endpoint rather than rewriting a working provider transport.
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("provider requires HTTP(S) URL without credentials, query or fragment")
        return value.rstrip("/")


class EffortOptions(BaseModel):
    """Explicit OpenAI-compatible effort wiring; no arbitrary provider settings."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    reasoningEffort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class SelectionConfig(BaseModel):
    """Validated operator policy; safe to pass between application components."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    schema_version: int = 1
    cache_root: Path
    availability_db: Path
    providers: tuple[ProviderConfig, ...]
    policy: ModelPolicy
    bindings: dict[str, dict[str, Any] | list[dict[str, Any]]] = Field(default_factory=dict)
    variant_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    shared_denylist: tuple[str, ...] = ()
    # Traffic shape used to rank two models by one price; only the ratio matters.
    price_weights: PriceWeights = PriceWeights()
    default_effort: str = ""
    max_age_seconds: int = Field(default=7 * 86400, gt=0)
    threshold_source: str = "default"

    @field_validator("variant_options")
    @classmethod
    def credential_free_variants(cls, value):
        result = {}
        for identity, options in value.items():
            if "reasoningEffort" in options:
                result[identity] = EffortOptions.model_validate(options).model_dump()
            else:
                result[identity] = {
                    variant: EffortOptions.model_validate(settings).model_dump()
                    for variant, settings in options.items()
                }
        return result

    @field_validator("schema_version")
    @classmethod
    def version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported model-selection config schema")
        return value

    @field_validator("cache_root", "availability_db")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("operational paths must be absolute")
        return value

    @model_validator(mode="after")
    def unique_providers(self):
        ids = [provider.id for provider in self.providers]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("providers must be nonempty with unique ids")
        return self


def load_selection_config(path: str | Path, *, environ: Mapping[str, str]) -> SelectionConfig:
    """Load an explicit absolute host file; never search a target repo for .env."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("model selection config path must be absolute")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("model selection config exceeds 1 MiB")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("model selection config must be an object")  # noqa: TRY004
    raw_policy = data.get("policy", {})
    if not isinstance(raw_policy, dict):
        raise ValueError("invalid application policy")  # noqa: TRY004
    policy = dict(raw_policy)
    source = "application" if "minimum_coding_score" in policy else "default"
    if "AGENT_MODEL_CODING_INDEX_MIN" in environ:
        try:
            policy["minimum_coding_score"] = float(environ["AGENT_MODEL_CODING_INDEX_MIN"])
        except (TypeError, ValueError):
            raise ValueError("invalid production coding-index threshold") from None
        source = "production"
    data["policy"] = policy
    data["threshold_source"] = source
    try:
        return SelectionConfig.model_validate(data)
    except ValueError:
        # Configuration may contain an accidental literal secret. Never echo it.
        raise ValueError("invalid model selection configuration") from None
