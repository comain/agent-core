"""Tests for template-based prompt rendering."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import os

import pytest
from jinja2 import TemplateNotFound, TemplateSyntaxError, UndefinedError

from agent_core.prompts import (
    PromptLibrary,
    RenderedPrompt,
    materialize_prompt,
    opaque_artifact_id,
    render_placeholders,
)


def test_opaque_artifact_id_is_stable_path_safe_and_does_not_expose_provider_id():
    provider_id = "remote/session: alpha/\u7528\u6237 " + ("x" * 200)

    first = opaque_artifact_id(provider_id, namespace="session")
    second = opaque_artifact_id(provider_id, namespace="session")

    assert first == second
    assert first.startswith("session-")
    assert len(first) == len("session-") + 64
    assert provider_id not in first
    assert all(character.islower() or character.isdigit() or character == "-" for character in first)


@pytest.mark.parametrize("value", ["", 123, None])
def test_opaque_artifact_id_rejects_missing_or_non_string_values(value):
    with pytest.raises((TypeError, ValueError)):
        opaque_artifact_id(value, namespace="session")


@pytest.mark.parametrize("namespace", ["", "bad/name", "bad space", "A" * 65])
def test_opaque_artifact_id_rejects_unsafe_namespaces(namespace):
    with pytest.raises(ValueError):
        opaque_artifact_id("provider-id", namespace=namespace)


@pytest.fixture
def templates(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "reviewer.md.j2").write_text(
        "# Review {{ task_id }}\n{% for f in files %}- {{ f }}\n{% endfor %}", encoding="utf-8"
    )
    return d


def test_render_fills_the_template(templates):
    out = PromptLibrary(templates).render("reviewer.md.j2", task_id="t1", files=["a.py", "b.py"])
    assert "# Review t1" in out
    assert "- a.py" in out and "- b.py" in out


def test_a_misspelled_variable_fails_instead_of_rendering_blank(templates):
    """The silent-empty-string default hides a whole missing prompt section."""
    with pytest.raises(UndefinedError):
        PromptLibrary(templates).render("reviewer.md.j2", task_id="t1")


def test_values_may_be_passed_as_a_mapping_or_keywords(templates):
    lib = PromptLibrary(templates)
    a = lib.render("reviewer.md.j2", {"task_id": "t1", "files": []})
    b = lib.render("reviewer.md.j2", task_id="t1", files=[])
    assert a == b


def test_a_missing_template_is_reported_by_name(templates):
    with pytest.raises(TemplateNotFound):
        PromptLibrary(templates).render("nope.md.j2")


def test_render_to_file_writes_the_prompt_and_its_inputs(templates, tmp_path):
    lib = PromptLibrary(templates)
    artifact = lib.render_to_file(
        "reviewer.md.j2", directory=tmp_path / "turn", task_id="t1", files=["a.py"]
    )

    assert artifact.read().startswith("# Review t1")
    # What the model was told must be reconstructable: a rendered prompt alone
    # cannot distinguish an empty section from an absent one.
    assert json.loads(artifact.inputs_path.read_text()) == {"task_id": "t1", "files": ["a.py"]}


def test_render_to_file_creates_the_directory(templates, tmp_path):
    artifact = PromptLibrary(templates).render_to_file(
        "reviewer.md.j2", directory=tmp_path / "deep" / "nested", task_id="t", files=[]
    )
    assert artifact.path.exists()


def test_render_to_file_can_sort_input_keys_for_stable_artifacts(templates, tmp_path):
    artifact = PromptLibrary(templates, sort_input_keys=True).render_to_file(
        "reviewer.md.j2",
        directory=tmp_path / "turn",
        values={"task_id": "t", "files": [], "z_last": 1, "a_first": 2},
    )

    inputs = artifact.inputs_path.read_text(encoding="utf-8")
    assert inputs.index('"a_first"') < inputs.index('"z_last"')


def test_inputs_are_written_even_when_a_value_is_not_json_serialisable(templates, tmp_path):
    """A Path among the values must not lose the whole inputs file."""
    artifact = PromptLibrary(templates).render_to_file(
        "reviewer.md.j2", directory=tmp_path / "turn", task_id=tmp_path, files=[]
    )
    assert str(tmp_path) in artifact.inputs_path.read_text()


def test_tojson_filter_is_available(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "x.j2").write_text("{{ data | tojson }}", encoding="utf-8")
    out = PromptLibrary(d).render("x.j2", data={"k": "值"})
    # ensure_ascii=False: a prompt is read by a model and by a human debugging it.
    assert "值" in out


def test_templates_load_from_a_package(tmp_path):
    lib = PromptLibrary("agent_core")
    with pytest.raises(TemplateNotFound):
        lib.render("definitely-not-there.j2")


def test_render_sections_partitions_only_the_first_boundary(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "split.j2").write_text(
        "stable {{ common }}<<CUT>>volatile {{ changing }}<<CUT>>tail",
        encoding="utf-8",
    )

    rendered = PromptLibrary(templates).render_sections(
        "split.j2",
        boundary="<<CUT>>",
        values={"common": "A", "changing": "B"},
    )

    assert rendered == RenderedPrompt(
        stable="stable A",
        volatile="volatile B<<CUT>>tail",
    )
    assert rendered.text == f"{rendered.stable}{rendered.volatile}"


def test_render_sections_without_boundary_is_entirely_volatile(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "plain.j2").write_text("hello {{ name }}", encoding="utf-8")

    rendered = PromptLibrary(templates).render_sections(
        "plain.j2", boundary="<<CUT>>", values={"name": "world"}
    )

    assert rendered.stable == ""
    assert rendered.volatile == "hello world"
    assert rendered.text == "hello world"


def test_render_sections_rejects_empty_boundary(templates):
    with pytest.raises(ValueError, match="boundary must not be empty"):
        PromptLibrary(templates).render_sections(
            "reviewer.md.j2", boundary="", task_id="t", files=[]
        )


def test_render_sections_keeps_strict_undefined_in_each_section(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "split.j2").write_text(
        "{{ stable_value }}<<CUT>>{{ volatile_value }}", encoding="utf-8"
    )
    library = PromptLibrary(templates)

    with pytest.raises(UndefinedError):
        library.render_sections(
            "split.j2", boundary="<<CUT>>", stable_value="present"
        )


def test_render_sections_selects_native_trailing_newline_policy(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "split.j2").write_text(
        "stable\n<<CUT>>volatile\n", encoding="utf-8"
    )
    library = PromptLibrary(templates)

    preserved = library.render_sections(
        "split.j2", boundary="<<CUT>>", keep_trailing_newline=True
    )
    legacy = library.render_sections(
        "split.j2", boundary="<<CUT>>", keep_trailing_newline=False
    )

    assert preserved == RenderedPrompt(stable="stable\n", volatile="volatile\n")
    assert legacy == RenderedPrompt(stable="stable", volatile="volatile")


def test_render_sections_does_not_mutate_shared_newline_policy(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "split.j2").write_text("a\n<<CUT>>b\n", encoding="utf-8")
    library = PromptLibrary(templates)

    policies = [False, True] * 50
    with ThreadPoolExecutor(max_workers=8) as executor:
        rendered = list(
            executor.map(
                lambda keep: library.render_sections(
                    "split.j2",
                    boundary="<<CUT>>",
                    keep_trailing_newline=keep,
                ),
                policies,
            )
        )

    assert all(
        result.text == ("ab" if keep is False else "a\nb\n")
        for keep, result in zip(policies, rendered)
    )
    assert library.render("split.j2") == "a\n<<CUT>>b\n"


def test_source_text_cache_is_opt_in(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "value.j2"
    template.write_text("first", encoding="utf-8")
    cached = PromptLibrary(templates, cache_source_text=True)
    uncached = PromptLibrary(templates)

    assert cached.render("value.j2") == "first"
    assert uncached.render("value.j2") == "first"
    template.write_text("second", encoding="utf-8")

    assert cached.render("value.j2") == "first"
    assert uncached.render("value.j2") == "second"


def test_split_jinja_block_reports_template_and_boundary(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "invalid.j2").write_text(
        "{% if enabled %}stable<<CUT>>volatile{% endif %}", encoding="utf-8"
    )

    with pytest.raises(TemplateSyntaxError) as raised:
        PromptLibrary(templates).render_sections(
            "invalid.j2", boundary="<<CUT>>", enabled=True
        )

    assert raised.value.name == "invalid.j2"
    assert "cache boundary" in str(raised.value)


def test_materialize_prompt_preserves_dynamic_text_and_metadata(tmp_path):
    artifact = materialize_prompt(
        "dynamic prompt",
        directory=tmp_path / "turn",
        metadata={"phase": "repair", "attempt": 2},
    )

    assert artifact.read() == "dynamic prompt"
    assert json.loads(artifact.inputs_path.read_text()) == {
        "attempt": 2,
        "phase": "repair",
    }


def test_render_placeholders_replaces_exact_names():
    assert render_placeholders("Hello {{who}}", {"who": "wiki"}) == "Hello wiki"


def test_render_placeholders_inserts_nested_braces_literally():
    """Values are not a second template pass."""
    out = render_placeholders("keep {{body}}", {"body": "see {{page}} later"})
    assert out == "keep see {{page}} later"


def test_render_placeholders_unknown_name_fails():
    with pytest.raises(ValueError, match="unknown placeholder 'missing'"):
        render_placeholders("{{missing}}", {"other": "x"})


def test_render_placeholders_ignores_unused_keys():
    assert render_placeholders("{{a}}", {"a": "1", "b": "2"}) == "1"


def test_strict_metadata_is_deterministic_and_json_safe(tmp_path):
    first = materialize_prompt(
        "prompt",
        directory=tmp_path / "first",
        metadata={"z": [1, True, None], "a": {"unicode": "值"}},
        strict_metadata=True,
    )
    second = materialize_prompt(
        "prompt",
        directory=tmp_path / "second",
        metadata={"a": {"unicode": "值"}, "z": [1, True, None]},
        strict_metadata=True,
    )

    assert first.inputs_path.read_bytes() == second.inputs_path.read_bytes()
    assert "值" in first.inputs_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "metadata",
    [
        {"object": object()},
        {1: "non-string key"},
        {"tuple": (1, 2)},
        {"nan": math.nan},
        {"positive_infinity": math.inf},
        {"negative_infinity": -math.inf},
    ],
)
def test_strict_metadata_rejects_unsupported_values(tmp_path, metadata):
    with pytest.raises((TypeError, ValueError)):
        materialize_prompt(
            "prompt",
            directory=tmp_path / "turn",
            metadata=metadata,
            strict_metadata=True,
        )

    assert not (tmp_path / "turn" / "prompt.md").exists()
    assert not (tmp_path / "turn" / "inputs.json").exists()


def test_strict_metadata_enforces_utf8_byte_limit_before_writing(tmp_path):
    with pytest.raises(ValueError, match="exceeds 16 bytes"):
        materialize_prompt(
            "prompt",
            directory=tmp_path / "turn",
            metadata={"value": "值" * 20},
            strict_metadata=True,
            metadata_max_bytes=16,
        )

    assert not (tmp_path / "turn").exists()


def test_secure_materialization_applies_requested_file_mode(tmp_path):
    artifact = materialize_prompt(
        "prompt",
        directory=tmp_path / "turn",
        metadata={"phase": "generate"},
        strict_metadata=True,
        file_mode=0o600,
    )

    assert artifact.path.stat().st_mode & 0o777 == 0o600
    assert artifact.inputs_path.stat().st_mode & 0o777 == 0o600


def test_secure_materialization_rejects_symlink_directory(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        materialize_prompt(
            "prompt",
            directory=linked,
            strict_metadata=True,
            file_mode=0o600,
        )


def test_secure_materialization_rejects_symlink_destination(tmp_path):
    target = tmp_path / "turn"
    target.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    (target / "prompt.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        materialize_prompt(
            "replace me",
            directory=target,
            strict_metadata=True,
            file_mode=0o600,
        )

    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("filename", ["../outside.md", "/tmp/outside.md", "nested/prompt.md"])
def test_secure_materialization_rejects_filename_escape(tmp_path, filename):
    with pytest.raises(ValueError, match="simple file name"):
        materialize_prompt(
            "prompt",
            directory=tmp_path / "turn",
            filename=filename,
            strict_metadata=True,
            file_mode=0o600,
        )


def test_secure_materialization_requires_distinct_filenames(tmp_path):
    with pytest.raises(ValueError, match="must differ"):
        materialize_prompt(
            "prompt",
            directory=tmp_path / "turn",
            filename="artifact.txt",
            inputs_filename="artifact.txt",
            strict_metadata=True,
            file_mode=0o600,
        )


def test_secure_materialization_recovers_after_second_replace_fails(
    tmp_path, monkeypatch
):
    import agent_core.prompts as prompts_module

    original_replace = os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected inputs replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(prompts_module.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="injected"):
        materialize_prompt(
            "first",
            directory=tmp_path / "turn",
            metadata={"attempt": 1},
            strict_metadata=True,
            file_mode=0o600,
        )

    monkeypatch.setattr(prompts_module.os, "replace", original_replace)
    artifact = materialize_prompt(
        "second",
        directory=tmp_path / "turn",
        metadata={"attempt": 2},
        strict_metadata=True,
        file_mode=0o600,
    )

    assert artifact.read() == "second"
    assert json.loads(artifact.inputs_path.read_text()) == {"attempt": 2}


def test_include_resolves_against_a_directory_source(tmp_path):
    (tmp_path / "shared.md").write_text("shared: {{ who }}\n", encoding="utf-8")
    (tmp_path / "main.md.j2").write_text(
        "top\n{% include 'shared.md' %}", encoding="utf-8"
    )
    library = PromptLibrary(tmp_path)
    assert library.render("main.md.j2", {"who": "spec"}) == "top\nshared: spec\n"


def test_include_of_a_missing_template_is_reported_by_name(tmp_path):
    (tmp_path / "main.md.j2").write_text("{% include 'absent.md' %}", encoding="utf-8")
    with pytest.raises(TemplateNotFound):
        PromptLibrary(tmp_path).render("main.md.j2", {})


def test_an_included_template_is_re_read_unless_caching_is_asked_for(tmp_path):
    (tmp_path / "shared.md").write_text("first\n", encoding="utf-8")
    (tmp_path / "main.md.j2").write_text("{% include 'shared.md' %}", encoding="utf-8")

    live = PromptLibrary(tmp_path)
    assert live.render("main.md.j2", {}) == "first\n"
    (tmp_path / "shared.md").write_text("second\n", encoding="utf-8")
    # An operator overriding a prompt without a release is the documented
    # reason directory sources exist; an include must follow the same rule.
    assert live.render("main.md.j2", {}) == "second\n"


def test_caching_pins_an_included_template(tmp_path):
    (tmp_path / "shared.md").write_text("first\n", encoding="utf-8")
    (tmp_path / "main.md.j2").write_text("{% include 'shared.md' %}", encoding="utf-8")

    cached = PromptLibrary(tmp_path, cache_source_text=True)
    assert cached.render("main.md.j2", {}) == "first\n"
    (tmp_path / "shared.md").write_text("second\n", encoding="utf-8")
    assert cached.render("main.md.j2", {}) == "first\n"


def test_render_to_file_renders_an_included_template(tmp_path):
    source = tmp_path / "prompts"
    source.mkdir()
    (source / "contract.md").write_text("reply with markdown only\n", encoding="utf-8")
    (source / "main.md.j2").write_text(
        "write {{ doc }}\n{% include 'contract.md' %}", encoding="utf-8"
    )
    artifact = PromptLibrary(source).render_to_file(
        "main.md.j2", directory=tmp_path / "out", values={"doc": "spec.md"}
    )
    assert "reply with markdown only" in artifact.read()
