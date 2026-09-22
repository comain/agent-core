"""Tests for attachment storage.

Uploaded filenames are treated as hostile: they arrive from a browser and are
the only caller-controlled part of a filesystem write.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.runtime import Attachment, AttachmentStore, AttachmentTooLarge, safe_name

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


@pytest.fixture
def store(tmp_path):
    return AttachmentStore(tmp_path / "attachments")


# -- filename safety -----------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\system32\\config",
        "....//....//etc/shadow",
    ],
)
def test_traversal_is_neutralised(hostile):
    out = safe_name(hostile)
    assert "/" not in out and "\\" not in out
    assert not out.startswith(".")


def test_null_bytes_removed():
    assert "\x00" not in safe_name("evil\x00.png")


def test_exotic_characters_are_replaced():
    assert safe_name("my file (1)!.png") == "my_file_1.png"


def test_empty_name_gets_a_fallback():
    assert safe_name("") == "file"
    assert safe_name("...") == "file"
    assert safe_name("/") == "file"


def test_long_names_are_truncated():
    assert len(safe_name("a" * 500 + ".png")) <= 75


def test_suffix_is_preserved_lowercased():
    assert safe_name("SHOT.PNG").endswith(".png")


# -- storage -------------------------------------------------------------------


def test_save_writes_the_bytes(store):
    att = store.save(task_ref="t1", filename="shot.png", data=PNG)
    assert att.path.read_bytes() == PNG
    assert att.size == len(PNG) and att.is_image


def test_stored_name_is_content_addressed(store):
    """A caller cannot pick the destination or clobber another file."""
    a = store.save(task_ref="t1", filename="shot.png", data=PNG)
    b = store.save(task_ref="t1", filename="shot.png", data=PNG + b"x")
    assert a.path != b.path
    assert a.path.name.startswith(a.digest) and b.path.name.startswith(b.digest)


def test_identical_bytes_are_idempotent(store):
    a = store.save(task_ref="t1", filename="shot.png", data=PNG)
    b = store.save(task_ref="t1", filename="shot.png", data=PNG)
    assert a.path == b.path
    assert len(store.list("t1")) == 1


def test_traversal_in_filename_stays_inside_the_task_directory(store, tmp_path):
    att = store.save(task_ref="t1", filename="../../escape.png", data=PNG)
    assert (tmp_path / "attachments" / "t1") in att.path.parents


def test_traversal_in_task_ref_is_neutralised(store, tmp_path):
    """task_ref is caller-supplied too."""
    att = store.save(task_ref="../../evil", filename="a.png", data=PNG)
    assert (tmp_path / "attachments").resolve() in att.path.resolve().parents


def test_size_limit_enforced(tmp_path):
    small = AttachmentStore(tmp_path, max_bytes=10)
    with pytest.raises(AttachmentTooLarge):
        small.save(task_ref="t1", filename="big.png", data=b"0" * 11)


def test_tasks_are_isolated(store):
    store.save(task_ref="t1", filename="a.png", data=PNG)
    store.save(task_ref="t2", filename="b.png", data=PNG)
    assert len(store.list("t1")) == 1 and len(store.list("t2")) == 1


def test_list_of_unknown_task_is_empty(store):
    assert store.list("nope") == []


# -- handing paths to the harness ----------------------------------------------


def test_paths_feed_the_harness(store):
    store.save(task_ref="t1", filename="shot.png", data=PNG)
    paths = store.paths("t1")
    assert len(paths) == 1 and paths[0].endswith(".png")


def test_images_only_filters_non_images(store):
    store.save(task_ref="t1", filename="shot.png", data=PNG)
    store.save(task_ref="t1", filename="notes.txt", data=b"hello")
    assert len(store.paths("t1")) == 2
    assert len(store.paths("t1", images_only=True)) == 1


def test_non_image_is_stored_but_flagged(store):
    att = store.save(task_ref="t1", filename="archive.zip", data=b"PK")
    assert att.is_image is False
