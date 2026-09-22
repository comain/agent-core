"""0.8.0 release canaries: version pin and mixed-pair rule in the README."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_version_is_0_8_52():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == "0.8.52"


def test_new_0_8_symbols_are_exported():
    from agent_core.git import update_refs_with_lease
    from agent_core.harness import SessionAffinity, render_placeholders

    assert callable(SessionAffinity.model_id)
    assert callable(render_placeholders)
    assert callable(update_refs_with_lease)


def test_readme_names_mixed_pair_rule_and_breaks():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "0.8.0" in text
    assert "0.7.x" in text
    assert "unsupported" in text.lower()
    assert "fcntl.flock" in text
    assert "bootstrap_message" in text
    assert "SessionAffinity.run" in text
