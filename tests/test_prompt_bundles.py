"""A rendered prompt as evidence, not as a file that happens to be on disk.

When a review is disputed months later the question is what the model was told,
exactly. A directory of files answers that only if a complete bundle can be
told from one a crashed worker left halfway through — which is what the
manifest is for. It is written last, it lists every file with its size and
digest, and its presence is the only thing that means "this is what it claims".
"""

from __future__ import annotations

import json

import pytest

from agent_core.prompts import (
    MANIFEST_SCHEMA_VERSION,
    MAX_BUNDLE_REFERENCES,
    PromptBundleIncomplete,
    PromptLibrary,
    PromptReference,
    read_bundle_manifest,
    verify_bundle,
)
from agent_core.runtime import (
    ArtifactConflictError,
    ArtifactSecurityError,
    SecureArtifactStore,
)


@pytest.fixture
def library(tmp_path):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "review.md.j2").write_text("Review {{ target }} 中文\n")
    return PromptLibrary(templates)


@pytest.fixture
def store(private_root):
    return SecureArtifactStore(private_root / "prompts")


def render(library, store, *, target="Main.java", namespace="run/1", **over):
    """Render one bundle. `over` reaches `render_bundle` directly."""
    fields = dict(
        store=store,
        namespace=namespace,
        values={"target": target},
        references=[PromptReference("plan.md", "the approved plan")],
    )
    fields.update(over)
    return library.render_bundle("review.md.j2", **fields)


# -- what a bundle is ------------------------------------------------------

def test_a_bundle_is_prompt_inputs_references_and_a_manifest(library, store):
    bundle = render(library, store)

    assert sorted(p.name for p in (store.root / "run/1").iterdir()) == [
        "inputs.json",
        "manifest.json",
        "plan.md",
        "prompt.md",
    ]
    assert bundle.prompt.relative_path == "run/1/prompt.md"
    assert [r.relative_path for r in bundle.references] == ["run/1/plan.md"]


def test_the_prompt_bytes_are_exactly_what_was_rendered(library, store):
    """Unicode included: a bundle that normalized what it stored would be
    evidence of something slightly different from what was sent."""
    render(library, store)

    assert (store.root / "run/1/prompt.md").read_bytes() == (
        "Review Main.java 中文\n".encode("utf-8")
    )


def test_the_manifest_is_metadata_only_and_bundle_relative(library, store):
    """A manifest quoting the prompt would move the private material into the
    index built to avoid reading it. Paths are bundle-relative, so a bundle
    that is moved or archived still verifies."""
    render(library, store)

    manifest = json.loads((store.root / "run/1/manifest.json").read_text())

    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert [entry["path"] for entry in manifest["files"]] == [
        "inputs.json",
        "plan.md",
        "prompt.md",
    ]
    assert set(manifest["files"][0]) == {"path", "media_type", "bytes", "sha256"}
    assert "Main.java" not in json.dumps(manifest)
    assert "run/1" not in json.dumps(manifest)


def test_the_manifest_does_not_list_itself(library, store):
    render(library, store)

    manifest = json.loads((store.root / "run/1/manifest.json").read_text())

    assert "manifest.json" not in [entry["path"] for entry in manifest["files"]]


def test_two_renders_of_the_same_values_produce_the_same_manifest_bytes(
    library, store
):
    """So a diff between two manifests means something."""
    render(library, store, namespace="run/1")
    render(library, store, namespace="run/2")

    assert (store.root / "run/1/manifest.json").read_bytes() == (
        store.root / "run/2/manifest.json"
    ).read_bytes()


# -- completion ------------------------------------------------------------

def test_a_bundle_without_a_manifest_is_incomplete(library, store):
    """The failure mode this prevents: an incomplete bundle looks exactly like
    a complete one with a section missing."""
    render(library, store)
    (store.root / "run/1/manifest.json").unlink()

    with pytest.raises(PromptBundleIncomplete, match="no manifest"):
        verify_bundle(store, "run/1")


def test_a_bundle_whose_file_changed_no_longer_verifies(library, store):
    render(library, store)
    (store.root / "run/1/plan.md").write_text("a different plan")

    with pytest.raises(PromptBundleIncomplete, match="does not match"):
        verify_bundle(store, "run/1")


def test_a_manifest_from_a_future_schema_is_refused(library, store):
    render(library, store)
    (store.root / "run/1/manifest.json").write_text(
        json.dumps({"schema_version": 99, "files": []})
    )

    with pytest.raises(PromptBundleIncomplete, match="does not understand"):
        read_bundle_manifest(store, "run/1")


def test_an_interrupted_bundle_is_rematerialized(library, store):
    """A crash before the manifest claimed nothing, so a later render may
    replace what it left."""
    render(library, store)
    (store.root / "run/1/manifest.json").unlink()
    (store.root / "run/1/prompt.md").write_text("half-written")

    render(library, store)

    assert verify_bundle(store, "run/1")
    assert "Main.java" in (store.root / "run/1/prompt.md").read_text()


def test_rendering_the_same_bundle_twice_is_a_no_op(library, store):
    first = render(library, store)

    assert render(library, store) == first


def test_a_different_render_under_a_complete_identity_is_a_conflict(library, store):
    """One of the two is already quoted in a report; replacing it silently is
    how a report stops matching the prompt it names."""
    render(library, store, target="Main.java")

    with pytest.raises(ArtifactConflictError):
        render(library, store, target="Other.java")

    assert "Main.java" in (store.root / "run/1/prompt.md").read_text()


# -- living beside other files ---------------------------------------------

def test_other_files_in_the_directory_are_none_of_its_business(library, store):
    """CR writes review output into the same directory."""
    render(library, store)
    (store.root / "run/1/findings.json").write_text("[]")

    assert verify_bundle(store, "run/1")


# -- refusals --------------------------------------------------------------

@pytest.mark.parametrize(
    "name, error",
    [
        ("../escape.md", ArtifactSecurityError),
        ("-rf", ArtifactSecurityError),
        ("prompt.md", ValueError),
        ("manifest.json", ValueError),
    ],
)
def test_an_unsafe_or_reserved_reference_name_is_refused(library, store, name, error):
    """And refused before anything is written: a name rejected halfway through
    would leave the first two files of a bundle that never gets a manifest."""
    with pytest.raises(error):
        render(library, store, references=[PromptReference(name, "x")])

    assert not (store.root / "run/1").exists()


def test_too_many_references_are_refused(library, store):
    references = [
        PromptReference(f"ref-{index}.md", "x")
        for index in range(MAX_BUNDLE_REFERENCES + 1)
    ]

    with pytest.raises(ValueError, match="exceeds"):
        render(library, store, references=references)


def test_a_bundle_over_the_configured_size_is_refused(library, store):
    with pytest.raises(ValueError, match="exceeds"):
        render(
            library,
            store,
            references=[PromptReference("big.md", "x" * 5000)],
            max_total_bytes=1000,
        )


def test_the_three_fixed_filenames_must_differ(library, store):
    with pytest.raises(ValueError, match="must all differ"):
        render(library, store, inputs_filename="prompt.md")


# -- privacy ---------------------------------------------------------------

def test_bundle_files_are_owner_only(library, store):
    import os
    import stat

    render(library, store)

    for path in (store.root / "run/1").iterdir():
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# -- re-rendering a namespace that is an object, not a render ---------------

def test_a_conflicting_render_can_be_a_deliberate_replacement(library, store):
    """A reviewer's prompt directory is per task and reviewer, and a requeued
    task genuinely re-renders it — the diff moved. Failing there would turn an
    ordinary retry into a dead task."""
    render(library, store, target="Main.java")

    bundle = render(library, store, target="Other.java", on_conflict="replace")

    assert verify_bundle(store, "run/1")
    assert "Other.java" in (store.root / "run/1/prompt.md").read_text()
    assert bundle.prompt.sha256 != ""


def test_a_replacement_still_writes_the_manifest_last(library, store, monkeypatch):
    """From the moment the old marker is removed until the new one lands the
    bundle reads as incomplete, which is exactly what it is. A reader must
    never see a manifest describing files that no longer match it."""
    render(library, store, target="Main.java")
    seen: list = []
    original = SecureArtifactStore.write_text

    def watch(self, name, content, **kwargs):
        if name.endswith("prompt.md"):
            seen.append(read_state(store))
        return original(self, name, content, **kwargs)

    def read_state(store):
        try:
            return "complete" if verify_bundle(store, "run/1") else "?"
        except PromptBundleIncomplete:
            return "incomplete"

    monkeypatch.setattr(SecureArtifactStore, "write_text", watch)
    render(library, store, target="Other.java", on_conflict="replace")

    assert seen == ["incomplete"], "the old manifest was still standing"
    assert verify_bundle(store, "run/1")


def test_replacing_with_identical_bytes_is_still_a_no_op(library, store):
    first = render(library, store)

    assert render(library, store, on_conflict="replace") == first


def test_an_unknown_conflict_policy_is_refused(library, store):
    with pytest.raises(ValueError, match="on_conflict"):
        render(library, store, on_conflict="overwrite-silently")


def test_references_may_be_grouped_by_kind(library, store):
    """`references/personas/backend.md` is a path already quoted in stored
    prompts; flattening it would change what those prompts point at."""
    bundle = render(
        library,
        store,
        references=[PromptReference("personas/backend.md", "the persona")],
    )

    assert (store.root / "run/1/personas/backend.md").read_text() == "the persona"
    assert bundle.references[0].relative_path == "run/1/personas/backend.md"
    assert verify_bundle(store, "run/1")
