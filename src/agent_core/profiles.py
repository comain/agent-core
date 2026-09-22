"""Configuration-backed agent profiles.

Products define the profile fields. Agent-core owns loading, validation,
default selection, and read-only access so every consumer does not grow its
own Python registry beside its prompt code.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping, Union


class ProfileError(ValueError):
    """A profile configuration cannot be used."""


@dataclass(frozen=True)
class AgentProfile:
    """One named, product-defined agent profile."""

    name: str
    _values: dict[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_values", deepcopy(self._values))

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._values[key])

    def get(self, key: str, default: Any = None) -> Any:
        return deepcopy(self._values.get(key, default))

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._values)


class ProfileRegistry:
    """Validated profiles with an explicit fallback profile."""

    def __init__(
        self,
        profiles: Mapping[str, Mapping[str, Any]],
        *,
        default: str,
    ) -> None:
        if not default:
            raise ProfileError("profile configuration must declare a default")
        built: dict[str, AgentProfile] = {}
        for raw_name, raw_values in profiles.items():
            name = str(raw_name).strip()
            if not name:
                raise ProfileError("profile name must not be empty")
            if not isinstance(raw_values, Mapping):
                raise ProfileError(f"profile {name!r} must be a mapping")
            built[name] = AgentProfile(name=name, _values=dict(raw_values))
        if not built:
            raise ProfileError("profile configuration has no profiles")
        if default not in built:
            raise ProfileError(f"default profile {default!r} is missing")
        self._profiles = built
        self._default_name = default

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    @property
    def default(self) -> AgentProfile:
        return self._profiles[self._default_name]

    def get(self, name: str) -> AgentProfile:
        return self._profiles.get(name, self.default)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "ProfileRegistry":
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfileError(f"cannot read profiles {str(path)!r}: {exc}") from exc
        suffix = path.suffix.lower()
        try:
            if suffix in {".yaml", ".yml"}:
                data = _load_yaml(text, path)
            elif suffix == ".json":
                data = json.loads(text)
            else:
                raise ProfileError(
                    f"unsupported profile format {suffix!r}; use .yaml, .yml or .json"
                )
        except json.JSONDecodeError as exc:
            raise ProfileError(f"invalid JSON in profiles {str(path)!r}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ProfileError("profile configuration must be a mapping")
        profiles = data.get("profiles")
        if not isinstance(profiles, Mapping):
            raise ProfileError("profile configuration profiles must be a mapping")
        default = str(data.get("default") or "").strip()
        return cls(profiles, default=default)


def _load_yaml(text: str, path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise ProfileError(
            f"reading {str(path)!r} needs PyYAML: pip install 'agent-core[yaml]'"
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid YAML in profiles {str(path)!r}: {exc}") from exc
