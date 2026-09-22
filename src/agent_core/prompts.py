"""Rendering prompts from templates.

Every consumer builds its agent prompts the same way: a Jinja template plus a
dictionary of values, written to a file that the harness passes to the CLI. The
templates differ per product; the mechanics do not.

Two decisions differ from the hand-rolled versions this replaces:

* **Undefined variables are an error.** ``Template(text).render(**values)``
  silently renders a misspelled variable as the empty string, and the result is
  a prompt missing a section that nobody notices until the model's answer is
  wrong. ``StrictUndefined`` turns that into a failure at render time.
* **The values are written out beside the prompt.** When a review goes wrong the
  first question is always what the model was actually told, and a rendered
  prompt alone does not answer it -- you cannot tell an empty section from an
  absent one. The inputs file makes the turn reproducible.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from jinja2 import (
    Environment,
    FunctionLoader,
    StrictUndefined,
    TemplateNotFound,
    TemplateSyntaxError,
)

from agent_core.paths import relative_parts

_PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


def render_placeholders(template: str, values: Mapping[str, str]) -> str:
    """Replace exact ``{{name}}`` tokens. Unknown names fail; values are literal.

    Unused keys are ignored here — the product wrapper checks them. A value
    that itself contains ``{{`` is inserted as-is, not expanded again.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unknown placeholder {name!r}")
        return values[name]

    return _PLACEHOLDER.sub(replace, template)


_ARTIFACT_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def opaque_artifact_id(value: str, *, namespace: str = "artifact") -> str:
    """Map an opaque provider identity to one stable, non-reversible path component.

    Provider/session identifiers are transport values, not filesystem names.
    Hashing keeps products and harness adapters from accidentally depending on
    a provider's current identifier alphabet or exposing that identifier in an
    application-state path.
    """
    if not isinstance(value, str):
        raise TypeError("artifact identity must be a string")
    if not value:
        raise ValueError("artifact identity must not be empty")
    if not isinstance(namespace, str) or not _ARTIFACT_NAMESPACE.fullmatch(namespace):
        raise ValueError(
            "artifact identity namespace must be 1-64 lowercase letters, "
            "digits, underscores, or hyphens"
        )
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{namespace}-{digest}"


@dataclass(frozen=True)
class PromptArtifact:
    """A rendered prompt on disk, and the values it was rendered from."""

    path: Path
    inputs_path: Path

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class RenderedPrompt:
    """A rendered prompt split into stable and volatile provider sections."""

    stable: str
    volatile: str

    @property
    def text(self) -> str:
        """Return the exact section concatenation without normalization."""
        return f"{self.stable}{self.volatile}"


def materialize_prompt(
    text: str,
    *,
    directory: Union[str, Path],
    metadata: Optional[Mapping[str, Any]] = None,
    filename: str = "prompt.md",
    inputs_filename: str = "inputs.json",
    strict_metadata: bool = False,
    metadata_max_bytes: Optional[int] = None,
    file_mode: Optional[int] = None,
) -> PromptArtifact:
    """Persist an already-rendered prompt with reproducibility metadata.

    Products sometimes assemble a prompt from deterministic validation output
    after their template has rendered.  The harness still consumes a file and
    operators still need to know which phase/attempt produced it; those common
    mechanics should not be reimplemented by every workflow.
    """
    metadata_text = _serialize_prompt_metadata(metadata, strict=strict_metadata)
    metadata_bytes = metadata_text.encode("utf-8")
    if metadata_max_bytes is not None:
        if metadata_max_bytes < 0:
            raise ValueError("metadata_max_bytes must be non-negative")
        if len(metadata_bytes) > metadata_max_bytes:
            raise ValueError(
                f"prompt metadata is {len(metadata_bytes)} bytes and exceeds "
                f"{metadata_max_bytes} bytes"
            )
    if file_mode is not None and not 0 <= file_mode <= 0o777:
        raise ValueError("file_mode must be between 0o000 and 0o777")

    target = Path(directory)
    secure = (
        strict_metadata or metadata_max_bytes is not None or file_mode is not None
    )
    if secure:
        _validate_artifact_filename(filename)
        _validate_artifact_filename(inputs_filename)
        if filename == inputs_filename:
            raise ValueError("prompt and inputs filenames must differ")
    if secure and target.is_symlink():
        raise ValueError(f"prompt artifact directory must not be a symlink: {target}")
    target.mkdir(parents=True, exist_ok=True)
    prompt_path = target / filename
    inputs_path = target / inputs_filename
    if secure:
        _secure_write_prompt_pair(
            prompt_path,
            text,
            inputs_path,
            metadata_text,
            file_mode=file_mode,
        )
    else:
        prompt_path.write_text(text, encoding="utf-8")
        inputs_path.write_text(metadata_text, encoding="utf-8")
    return PromptArtifact(path=prompt_path, inputs_path=inputs_path)


def _validate_artifact_filename(filename: str) -> None:
    path = Path(filename)
    if (
        not filename
        or path.is_absolute()
        or path.name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError(
            f"prompt artifact filename must be a simple file name: {filename!r}"
        )


def _serialize_prompt_metadata(
    metadata: Optional[Mapping[str, Any]], *, strict: bool
) -> str:
    values = dict(metadata or {})
    if strict:
        _validate_json_value(values, path="metadata", active=set())
        return json.dumps(
            values,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    return json.dumps(
        values,
        ensure_ascii=False,
        indent=2,
        default=str,
        sort_keys=True,
    )


def _validate_json_value(value: Any, *, path: str, active: set[int]) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        return
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a circular list")
        active.add(identity)
        try:
            for index, item in enumerate(value):
                _validate_json_value(item, path=f"{path}[{index}]", active=active)
        finally:
            active.remove(identity)
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a circular mapping")
        active.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} keys must be strings")
                _validate_json_value(item, path=f"{path}.{key}", active=active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")


def _secure_write_prompt_pair(
    prompt_path: Path,
    prompt_text: str,
    inputs_path: Path,
    inputs_text: str,
    *,
    file_mode: Optional[int],
) -> None:
    for destination in (prompt_path, inputs_path):
        if destination.is_symlink():
            raise ValueError(
                f"prompt artifact destination must not be a symlink: {destination}"
            )

    prompt_temp: Optional[Path] = None
    inputs_temp: Optional[Path] = None
    try:
        prompt_temp = _prepare_text_file(prompt_path.parent, prompt_text, file_mode)
        inputs_temp = _prepare_text_file(inputs_path.parent, inputs_text, file_mode)
        os.replace(prompt_temp, prompt_path)
        prompt_temp = None
        os.replace(inputs_temp, inputs_path)
        inputs_temp = None
        if file_mode is not None:
            prompt_path.chmod(file_mode)
            inputs_path.chmod(file_mode)
    finally:
        for temporary in (prompt_temp, inputs_temp):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _prepare_text_file(directory: Path, text: str, file_mode: Optional[int]) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".prompt-", dir=directory)
    path = Path(raw_path)
    try:
        if file_mode is not None:
            os.fchmod(descriptor, file_mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


class PromptLibrary:
    """Templates loaded from a package's resources or a directory.

    A package is the normal case -- templates ship with the product that owns
    them -- while a directory lets an operator override a prompt without a
    release, and lets tests use a tmp_path. ``sort_input_keys`` gives audit
    artifacts stable textual ordering without changing the default contract.
    """

    def __init__(
        self,
        source: Union[str, Path],
        *,
        filters: Optional[Mapping[str, Any]] = None,
        sort_input_keys: bool = False,
        cache_source_text: bool = False,
    ):
        self.source = source
        self.sort_input_keys = sort_input_keys
        self._cache_source_text = cache_source_text
        self._env = Environment(
            # A loader, so `{% include %}`, `{% import %}` and `{% extends %}`
            # resolve. Without one Jinja has nowhere to look up a name and a
            # template that includes another fails at render time. Prompts
            # repeat themselves -- an output contract, a rubric quoted by two
            # stages -- and composing them is the normal way to stop that.
            loader=FunctionLoader(self._load_for_jinja),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,  # prompts are markdown for a model, not HTML
        )
        self._env.filters["tojson"] = lambda v, indent=2: json.dumps(v, ensure_ascii=False, indent=indent)
        for name, fn in (filters or {}).items():
            self._env.filters[name] = fn
        self._text_loader = (
            lru_cache(maxsize=None)(self._load_template_text)
            if cache_source_text
            else self._load_template_text
        )

    def _template_text(self, name: str) -> str:
        return self._text_loader(name)

    def _load_for_jinja(self, name: str):
        """Resolve an included name through the same source as a top-level render.

        The uptodate callable is what keeps the two consistent. ``render`` re-reads
        its template unless ``cache_source_text`` says otherwise -- that is what
        lets an operator override a prompt without a release -- so an included
        template must be re-read on the same terms rather than being pinned by
        Jinja's compiled-template cache.
        """
        text = self._template_text(name)
        uptodate = None if self._cache_source_text else (lambda: False)
        return text, str(name), uptodate

    def _load_template_text(self, name: str) -> str:
        if isinstance(self.source, Path) or "/" in str(self.source) or "\\" in str(self.source):
            path = Path(self.source) / name
            if not path.exists():
                raise TemplateNotFound(name)
            return path.read_text(encoding="utf-8")
        try:
            return (resources.files(str(self.source)) / name).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise TemplateNotFound(name) from exc

    def render(self, name: str, values: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> str:
        merged: Dict[str, Any] = {**(values or {}), **kwargs}
        return self._env.from_string(self._template_text(name)).render(**merged)

    def render_sections(
        self,
        name: str,
        *,
        boundary: str,
        keep_trailing_newline: Optional[bool] = None,
        values: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> RenderedPrompt:
        """Render stable and volatile regions split on the first boundary."""
        if not boundary:
            raise ValueError("boundary must not be empty")

        raw = self._template_text(name)
        if boundary in raw:
            stable_source, _, volatile_source = raw.partition(boundary)
        else:
            stable_source, volatile_source = "", raw

        merged: Dict[str, Any] = {**(values or {}), **kwargs}
        environment = (
            self._env
            if keep_trailing_newline is None
            else self._env.overlay(keep_trailing_newline=keep_trailing_newline)
        )

        def render_section(source: str) -> str:
            try:
                return environment.from_string(source).render(**merged)
            except TemplateSyntaxError as exc:
                raise TemplateSyntaxError(
                    f"{exc.message} after splitting template at cache boundary",
                    exc.lineno,
                    name=name,
                    filename=exc.filename,
                ) from exc

        return RenderedPrompt(
            stable=render_section(stable_source),
            volatile=render_section(volatile_source),
        )

    def render_to_file(
        self,
        name: str,
        *,
        directory: Union[str, Path],
        values: Optional[Mapping[str, Any]] = None,
        filename: str = "prompt.md",
        inputs_filename: str = "inputs.json",
        **kwargs: Any,
    ) -> PromptArtifact:
        """Render into ``directory``, writing the values alongside.

        The harness is given a prompt *file* rather than a string -- a long
        prompt on a command line hits the argument length limit -- so rendering
        to disk is the normal path, not a debugging aid.
        """
        merged: Dict[str, Any] = {**(values or {}), **kwargs}
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)

        prompt_path = target / filename
        prompt_path.write_text(self.render(name, merged), encoding="utf-8")

        inputs_path = target / inputs_filename
        inputs_path.write_text(
            json.dumps(
                merged,
                ensure_ascii=False,
                indent=2,
                default=str,
                sort_keys=self.sort_input_keys,
            ),
            encoding="utf-8",
        )
        return PromptArtifact(path=prompt_path, inputs_path=inputs_path)

    def render_bundle(
        self,
        name: str,
        *,
        store: Any,
        namespace: str,
        values: Optional[Mapping[str, Any]] = None,
        references: Sequence["PromptReference"] = (),
        filename: str = "prompt.md",
        inputs_filename: str = "inputs.json",
        manifest_filename: str = "manifest.json",
        max_total_bytes: Optional[int] = None,
        on_conflict: str = "fail",
        **kwargs: Any,
    ) -> "PromptBundle":
        """Render one prompt into a verifiable bundle, manifest last.

        Everything is written under the namespace lock, so a reader never sees
        a manifest describing files that are still being written, and two
        workers rendering the same identity are ordered rather than interleaved.

        Rendering the same values twice is a no-op that returns the same
        receipts. Rendering *different* values under an identity that already
        has a complete bundle is an error: one of the two is already quoted
        somewhere, and silently replacing it is how a report stops matching the
        prompt it names.
        """
        merged: Dict[str, Any] = {**(values or {}), **kwargs}
        prompt_text = self.render(name, merged)
        inputs_text = json.dumps(
            merged,
            ensure_ascii=False,
            indent=2,
            default=str,
            sort_keys=self.sort_input_keys,
        )
        return materialize_bundle(
            store,
            namespace,
            prompt=prompt_text,
            inputs=inputs_text,
            references=references,
            filename=filename,
            inputs_filename=inputs_filename,
            manifest_filename=manifest_filename,
            max_total_bytes=max_total_bytes,
            on_conflict=on_conflict,
        )


# ---------------------------------------------------------------------------
# Prompt bundles
# ---------------------------------------------------------------------------
#
# A rendered prompt is evidence. When a review is disputed months later the
# question is what the model was told, exactly — and a directory of files
# answers that only if you can tell a complete one from a directory a crashed
# worker left halfway through. That is what the manifest is for: it is written
# last, it lists every file with its size and digest, and its presence is the
# only thing that means "this bundle is what it claims to be".
#
# Three consequences follow.
#
# **A crash before the manifest is recoverable, and a conflict after it is
# not.** An incomplete bundle has no manifest, so a later render may overwrite
# it — nothing has claimed those bytes. A complete bundle whose files would be
# rewritten with different bytes is a collision between two different renders
# under one identity, and that is an error rather than an overwrite: one of
# them is already quoted in a report.
#
# **The manifest holds no prompt text.** Paths, byte counts, digests, media
# types. A manifest that quoted the prompt would move the private material into
# the index built to avoid reading it.
#
# **Other files in the directory are none of its business.** CR writes review
# output into the same directory. The manifest lists what the bundle put there
# and verification checks exactly those, so a mixed directory keeps working.

#: The manifest's own shape. Bumped only when a reader must behave differently.
MANIFEST_SCHEMA_VERSION = 1

#: A bundle is prompt, inputs, manifest and references. The cap is on
#: references; it exists so a product cannot turn a bundle into a filesystem.
MAX_BUNDLE_REFERENCES = 256

#: Hard ceiling per bundle. A product may configure something smaller.
MAX_BUNDLE_BYTES = 64 * 1024 * 1024

_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
    ".patch": "text/x-diff",
    ".diff": "text/x-diff",
}


def _media_type(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "text/plain")


@dataclass(frozen=True)
class PromptReference:
    """A file the prompt refers to, carried with it so the pair stays together.

    A prompt that says "the plan below" and a plan stored somewhere else are
    two artifacts with independent lifetimes, and the one that survives is
    never the one you need.
    """

    name: str
    text: str


@dataclass(frozen=True)
class PromptBundle:
    """A complete bundle, as the receipts for what was written."""

    namespace: str
    prompt: Any
    inputs: Any
    manifest: Any
    references: tuple = ()

    @property
    def files(self) -> tuple:
        return (self.prompt, self.inputs) + tuple(self.references)


class PromptBundleIncomplete(RuntimeError):
    """No manifest, or a manifest that does not describe what is on disk.

    A reader must never accept one: an incomplete bundle looks exactly like a
    complete bundle with a section missing, which is the failure mode that
    makes a prompt archive worthless.
    """


def _manifest_bytes(entries) -> str:
    """``entries`` are ``(bundle-relative name, StoredArtifact)`` pairs.

    Bundle-relative, never store-relative: a manifest that named
    ``run/1/prompt.md`` would stop verifying the moment the bundle moved, and
    moving a bundle is what retention and archival do.
    """
    return json.dumps(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            # Sorted by path so two renders of the same bundle produce the same
            # manifest bytes, and a diff between them means something.
            "files": [
                {
                    "path": name,
                    "media_type": _media_type(name),
                    "bytes": entry.bytes,
                    "sha256": entry.sha256,
                }
                for name, entry in sorted(entries, key=lambda item: item[0])
            ],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )


def read_bundle_manifest(store, namespace: str, *, max_bytes: int = 1 << 20) -> dict:
    """The manifest, or `PromptBundleIncomplete` if there is not a valid one."""
    from agent_core.runtime.artifacts import ArtifactError

    name = f"{namespace}/manifest.json"
    if not store.exists(name):
        raise PromptBundleIncomplete(f"{namespace} has no manifest")
    try:
        manifest = json.loads(store.read_bytes(name, max_bytes=max_bytes))
    except (ArtifactError, ValueError) as exc:
        raise PromptBundleIncomplete(f"{namespace} has an unreadable manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != (
        MANIFEST_SCHEMA_VERSION
    ):
        raise PromptBundleIncomplete(
            f"{namespace} has a manifest this version does not understand"
        )
    return manifest


def verify_bundle(store, namespace: str, *, max_bytes: int = MAX_BUNDLE_BYTES) -> dict:
    """Accept a bundle only if every file the manifest lists still verifies."""
    from agent_core.runtime.artifacts import ArtifactError

    manifest = read_bundle_manifest(store, namespace)
    for entry in manifest.get("files") or ():
        try:
            data = store.read_verified(
                f"{namespace}/{entry['path']}",
                sha256=entry["sha256"],
                max_bytes=max_bytes,
            )
        except (ArtifactError, FileNotFoundError, KeyError, TypeError) as exc:
            raise PromptBundleIncomplete(
                f"{namespace} does not match its manifest"
            ) from exc
        if len(data) != entry.get("bytes"):
            raise PromptBundleIncomplete(f"{namespace} does not match its manifest")
    return manifest


def _validate_bundle_filename(name: str) -> None:
    """A safe relative name, which may group references by kind.

    Nesting is allowed because references are already grouped that way in
    stored prompts -- ``references/personas/backend.md`` -- and flattening them
    would change paths those prompts point at. What is not allowed is anything
    that could leave the namespace; the store's own name rules decide that, so
    there is one answer to "is this a safe name" rather than two.
    """
    from agent_core.runtime.artifacts import ArtifactSecurityError

    relative_parts(name, error=ArtifactSecurityError)


def materialize_bundle(
    store: Any,
    namespace: str,
    *,
    prompt: str,
    inputs: str,
    references: Sequence[PromptReference] = (),
    filename: str = "prompt.md",
    inputs_filename: str = "inputs.json",
    manifest_filename: str = "manifest.json",
    max_total_bytes: Optional[int] = None,
    on_conflict: str = "fail",
) -> PromptBundle:
    """Write a bundle whose manifest is the only completion marker.

    Separate from `PromptLibrary` so a product that assembled its prompt some
    other way -- from validation output, from a template engine of its own --
    gets the same durability without pretending to render.

    ``on_conflict`` says what a namespace that already holds a *different*
    complete bundle means, and the answer depends on what the namespace
    identifies -- which only the caller knows:

    ``"fail"``
        The namespace is one render. A different one under the same identity is
        two things claiming to be the same evidence, and one of them is already
        quoted in a report. This is right for an operation-scoped namespace.

    ``"replace"``
        The namespace is an *object* that is legitimately re-rendered -- a
        reviewer's prompt for a task that was requeued, where the diff moved
        and the prompt genuinely differs. Failing there would turn an ordinary
        retry into a dead task. The manifest is still written last, so a reader
        never sees a half-replaced bundle, and the namespace lock still keeps
        two writers from interleaving.

    Identical bytes are a no-op under both.
    """
    if on_conflict not in ("fail", "replace"):
        raise ValueError(f"on_conflict must be 'fail' or 'replace', got {on_conflict!r}")
    from agent_core.runtime.artifacts import ArtifactConflictError

    fixed = {filename, inputs_filename, manifest_filename}
    if len(fixed) != 3:
        raise ValueError("prompt, inputs and manifest filenames must all differ")
    if len(references) > MAX_BUNDLE_REFERENCES:
        raise ValueError(
            f"{len(references)} references exceeds the {MAX_BUNDLE_REFERENCES} allowed"
        )

    files = [(filename, prompt), (inputs_filename, inputs)]
    seen = set(fixed)
    for reference in references:
        # Validated before anything is written, not when its turn comes: a name
        # refused halfway through would leave a directory holding the first two
        # files of a bundle that will never have a manifest.
        _validate_bundle_filename(reference.name)
        if reference.name in seen:
            raise ValueError(f"duplicate or reserved reference name: {reference.name!r}")
        seen.add(reference.name)
        files.append((reference.name, reference.text))

    ceiling = MAX_BUNDLE_BYTES if max_total_bytes is None else min(
        max_total_bytes, MAX_BUNDLE_BYTES
    )
    total = sum(len(text.encode("utf-8")) for _, text in files)
    if total > ceiling:
        raise ValueError(f"bundle is {total} bytes and exceeds the {ceiling} allowed")

    with store.locked(namespace):
        try:
            complete = read_bundle_manifest(store, namespace) is not None
        except PromptBundleIncomplete:
            complete = False

        # Immutable only once a manifest exists and the caller says a second
        # render of this identity is a mistake. An incomplete bundle has
        # claimed nothing, so a later render may always replace it.
        immutable = complete and on_conflict == "fail"
        if complete and not immutable:
            # Remove the completion marker first: from here until the new
            # manifest lands the bundle reads as incomplete, which is exactly
            # what it is. Leaving the old manifest up would have it describing
            # files that no longer match it.
            store.delete_file(f"{namespace}/{manifest_filename}")

        stored = {}
        for entry_name, text in files:
            stored[entry_name] = store.write_text(
                f"{namespace}/{entry_name}", text, immutable=immutable
            )

        manifest_text = _manifest_bytes(stored.items())
        if immutable:
            # Same bytes is a no-op; different bytes conflicts here rather than
            # leaving a manifest that describes a file it no longer matches.
            manifest = store.write_text(
                f"{namespace}/{manifest_filename}", manifest_text, immutable=True
            )
        else:
            manifest = store.write_text(
                f"{namespace}/{manifest_filename}", manifest_text
            )

        return PromptBundle(
            namespace=namespace,
            prompt=stored[filename],
            inputs=stored[inputs_filename],
            manifest=manifest,
            references=tuple(stored[reference.name] for reference in references),
        )
