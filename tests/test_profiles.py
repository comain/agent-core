import json

import pytest

from agent_core.profiles import ProfileError, ProfileRegistry


def test_profile_registry_loads_config_and_uses_declared_default(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text(
        """
default: correctness
profiles:
  correctness:
    role: Code reviewer
    focus: [correctness, architecture]
    tool_policy: [read only]
  security:
    role: Security reviewer
    focus: [trust boundaries]
""",
        encoding="utf-8",
    )

    profiles = ProfileRegistry.from_file(path)

    assert profiles.names == ("correctness", "security")
    assert profiles.get("security")["focus"] == ["trust boundaries"]
    assert profiles.get("unknown").name == "correctness"
    assert profiles.default.name == "correctness"


def test_profile_data_is_returned_as_a_copy(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "default": "review",
                "profiles": {"review": {"role": "Reviewer", "rules": ["one"]}},
            }
        ),
        encoding="utf-8",
    )
    profile = ProfileRegistry.from_file(path).default

    first = profile.to_dict()
    first["rules"].append("mutated")

    assert profile.to_dict()["rules"] == ["one"]
    assert profile.get("missing", ["fallback"]) == ["fallback"]


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"profiles": {"review": {"role": "Reviewer"}}}, "default"),
        (
            {"default": "missing", "profiles": {"review": {"role": "Reviewer"}}},
            "missing",
        ),
        ({"default": "review", "profiles": {"review": "not a mapping"}}, "mapping"),
    ],
)
def test_profile_registry_rejects_invalid_contracts(tmp_path, payload, message):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProfileError, match=message):
        ProfileRegistry.from_file(path)


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"default": "review", "profiles": {}}, "no profiles"),
        ({"default": "review", "profiles": {"": {}}}, "name"),
        (["not", "a", "mapping"], "must be a mapping"),
        ({"default": "review", "profiles": []}, "profiles must be a mapping"),
    ],
)
def test_profile_registry_rejects_empty_and_wrong_shapes(tmp_path, payload, message):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProfileError, match=message):
        ProfileRegistry.from_file(path)


def test_profile_registry_reports_file_and_syntax_errors(tmp_path):
    with pytest.raises(ProfileError, match="cannot read"):
        ProfileRegistry.from_file(tmp_path / "missing.json")

    unsupported = tmp_path / "profiles.txt"
    unsupported.write_text("profiles", encoding="utf-8")
    with pytest.raises(ProfileError, match="unsupported"):
        ProfileRegistry.from_file(unsupported)

    invalid_json = tmp_path / "profiles.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid JSON"):
        ProfileRegistry.from_file(invalid_json)

    invalid_yaml = tmp_path / "profiles.yaml"
    invalid_yaml.write_text("profiles: [", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid YAML"):
        ProfileRegistry.from_file(invalid_yaml)
