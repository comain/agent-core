"""Resuming a workflow after the process that ran it is gone.

The saver is LangGraph's; what belongs here is the boundary. A product passes
a path and an identity and never imports a checkpointer class, so the backend
can be replaced without touching a consumer -- and a second consumer inherits
a contract rather than a pattern.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from agent_core.workflow.checkpoints import (
    CheckpointCapabilityError,
    UnsafeCheckpointPathError,
    delete_checkpoint_lineage,
    WorkflowRunIdentity,
    open_checkpointer,
)

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures/contracts/agent_core_v0_6_11.json").read_text(
        encoding="utf-8"
    )
)


def identity(**over):
    base = dict(
        product="uta", cycle="generation-cycle", version="v1",
        task_id="task1", unit_id="batch1", workflow_run_id="run1",
    )
    base.update(over)
    return WorkflowRunIdentity(**base)


# -- identity --------------------------------------------------------------

def test_the_thread_id_carries_every_part():
    assert identity().thread_id == "uta:generation-cycle:v1:task1:batch1:run1"


def test_the_version_is_in_the_thread_id_not_a_namespace():
    """LangGraph resolves `checkpoint_ns` as a subgraph path: a version there
    makes `get_state` raise "Subgraph ... not found". Found by spike."""
    assert "v1" in identity().thread_id
    assert not hasattr(identity(), "checkpoint_ns")


def test_a_new_version_is_a_different_lineage():
    """An incompatible topology must not resume into moved nodes."""
    assert identity(version="v1").thread_id != identity(version="v2").thread_id


def test_a_clean_rerun_is_a_different_lineage():
    assert identity(workflow_run_id="run1").thread_id != identity(workflow_run_id="run2").thread_id


def test_a_retry_of_the_same_run_reuses_its_lineage():
    assert identity().thread_id == identity().thread_id


def test_different_units_of_one_task_are_separate():
    """Per-unit resume is the point; one lineage per task would defeat it."""
    assert identity(unit_id="b1").thread_id != identity(unit_id="b2").thread_id


def test_thread_ids_match_the_0_6_11_contract_fixture():
    assert CONTRACT["contract_version"] == "agent-core-0.6.11"
    assert {
        "base": identity().thread_id,
        "topology_v2": identity(version="v2").thread_id,
        "clean_rerun": identity(workflow_run_id="run2").thread_id,
        "other_unit": identity(unit_id="batch2").thread_id,
    } == CONTRACT["thread_ids"]


def test_identity_is_immutable():
    """It is a key. A mutated key silently resumes the wrong lineage."""
    with pytest.raises(Exception):
        identity().task_id = "other"


def test_invoke_config_carries_the_thread_and_the_limit():
    """Products should not know that the thread id goes under `configurable`
    while `recursion_limit` does not."""
    cfg = identity().invoke_config(recursion_limit=120)
    assert cfg["configurable"]["thread_id"] == "uta:generation-cycle:v1:task1:batch1:run1"
    assert cfg["recursion_limit"] == 120


# -- the checkpointer ------------------------------------------------------

def test_it_yields_langgraph_s_own_saver(private_root):
    """The point of the 0.7 change.

    A wrapper had to forward every method of an evolving third-party protocol
    by hand, and a method missed there failed deep inside a graph run. The
    product now gets the concrete object and passes it straight to `compile`.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    with open_checkpointer(private_root / "cp.sqlite", forbidden_roots=()) as saver:
        assert isinstance(saver, SqliteSaver)


def test_no_forwarding_wrapper_remains():
    """Named explicitly so a reintroduction is a failing test, not a review
    comment someone might miss."""
    import agent_core.workflow.checkpoints as mod

    assert not hasattr(mod, "WorkflowCheckpointer")


def test_it_creates_the_file(private_root):
    path = private_root / "cp.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        saver.setup()
    assert path.exists()


def test_it_closes_the_connection(private_root):
    """A daemon leaking one connection per run fails slowly and confusingly."""
    path = private_root / "cp.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        saver.setup()
        conn = getattr(saver, "conn", None)
    if conn is not None:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("select 1")


def test_it_closes_even_when_the_body_raises(private_root):
    path = private_root / "cp.sqlite"
    conn = None
    with pytest.raises(RuntimeError):
        with open_checkpointer(path, forbidden_roots=()) as saver:
            saver.setup()
            conn = getattr(saver, "conn", None)
            raise RuntimeError("worker died")
    if conn is not None:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("select 1")


def test_it_makes_parent_directories(private_root):
    """The state directory may not exist on a fresh node."""
    path = private_root / "nested" / "deeper" / "cp.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        saver.setup()
    assert path.exists()


def test_a_missing_extra_says_what_to_install(monkeypatch, private_root):
    """At open, with the command -- not at the first resume, in production."""
    import agent_core.workflow.checkpoints as mod

    monkeypatch.setattr(mod, "_saver_factory", None)
    with pytest.raises(ImportError) as caught:
        with open_checkpointer(private_root / "cp.sqlite", forbidden_roots=()):
            pass
    assert "agent-core[langgraph]" in str(caught.value)


# -- the path is task data -------------------------------------------------

def test_created_directories_and_the_database_are_owner_only(private_root):
    path = private_root / "made" / "cp.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        saver.setup()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_shared_parent_is_refused_not_repaired(tmp_path):
    """The window in which it was readable has already happened; silently
    chmodding it hides that from whoever has to decide whether it mattered."""
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)

    with pytest.raises(UnsafeCheckpointPathError) as caught:
        with open_checkpointer(shared / "cp.sqlite", forbidden_roots=()):
            pass

    assert str(shared) in str(caught.value)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755, "it was repaired anyway"


def test_a_symlinked_root_is_refused(tmp_path, private_root):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    link = private_root / "link"
    link.symlink_to(elsewhere)

    with pytest.raises(UnsafeCheckpointPathError, match="symlink"):
        with open_checkpointer(link / "cp.sqlite", forbidden_roots=()):
            pass


def test_a_symlinked_database_is_refused(tmp_path, private_root):
    """Otherwise the graph state is written wherever the link points, with
    whatever permissions that file already had."""
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"")
    link = private_root / "cp.sqlite"
    link.symlink_to(outside)

    with pytest.raises(UnsafeCheckpointPathError, match="symlink"):
        with open_checkpointer(link, forbidden_roots=()):
            pass


def test_state_inside_the_repository_being_edited_is_refused(private_root):
    """A workspace reset would delete the checkpoint, or the agent would commit
    it. Either way the run is not resumable, which is the whole point."""
    repo = private_root / "repo"
    repo.mkdir(mode=0o700)

    with pytest.raises(UnsafeCheckpointPathError, match="forbidden root"):
        with open_checkpointer(repo / ".state" / "cp.sqlite", forbidden_roots=[repo]):
            pass


def test_a_root_containing_the_repository_is_refused(private_root):
    """Overlap is rejected in both directions: a root above the repo makes the
    repo deletable by artifact retention."""
    repo = private_root / "repo"
    repo.mkdir(mode=0o700)

    with pytest.raises(UnsafeCheckpointPathError, match="forbidden root"):
        with open_checkpointer(private_root / "cp.sqlite", forbidden_roots=[repo]):
            pass


def test_a_refused_path_leaves_nothing_behind(tmp_path):
    """Validation happens before creation, so the next run does not inherit a
    directory that looks like it was already accepted."""
    repo = tmp_path / "repo"
    repo.mkdir(mode=0o700)
    target = repo / "state" / "cp.sqlite"

    with pytest.raises(UnsafeCheckpointPathError):
        with open_checkpointer(target, forbidden_roots=[repo]):
            pass

    assert not target.parent.exists()


# -- what it is all for ----------------------------------------------------

def _cycle():
    """A two-node graph where the first node is the expensive one."""
    from langgraph.graph import END, StateGraph
    from typing_extensions import TypedDict

    class S(TypedDict, total=False):
        trail: list
        boom: bool

    ran = []

    def expensive(state):
        ran.append("expensive")
        return {"trail": state.get("trail", []) + ["expensive"]}

    def cheap(state):
        ran.append("cheap")
        if state.get("boom"):
            raise RuntimeError("worker died")
        return {"trail": state["trail"] + ["cheap"]}

    def build():
        g = StateGraph(S)
        g.add_node("expensive", expensive)
        g.add_node("cheap", cheap)
        g.set_entry_point("expensive")
        g.add_edge("expensive", "cheap")
        g.add_edge("cheap", END)
        return g

    return build, ran


def test_a_restart_does_not_repeat_the_expensive_phase(private_root):
    """The reason this module exists.

    A worker dies after the expensive node. A *new* process rebuilds the graph
    from the same identity and must continue, not start again.
    """
    build, ran = _cycle()
    path = private_root / "cp.sqlite"
    run = identity()

    with open_checkpointer(path, forbidden_roots=()) as saver:
        graph = build().compile(checkpointer=saver)
        with pytest.raises(RuntimeError):
            graph.invoke({"boom": True}, config=run.invoke_config(recursion_limit=50))
    assert ran == ["expensive", "cheap"]

    ran.clear()
    with open_checkpointer(path, forbidden_roots=()) as saver:          # a new process
        graph = build().compile(checkpointer=saver)
        state = graph.get_state(run.invoke_config(recursion_limit=50))
        assert state.values["trail"] == ["expensive"]
        assert state.next == ("cheap",)
        graph.update_state(run.invoke_config(recursion_limit=50), {"boom": False})
        out = graph.invoke(None, config=run.invoke_config(recursion_limit=50))

    assert ran == ["cheap"], "the expensive phase was repeated"
    assert out["trail"] == ["expensive", "cheap"]


def test_a_different_version_starts_clean(private_root):
    """An incompatible topology must not resume into nodes that moved."""
    build, ran = _cycle()
    path = private_root / "cp.sqlite"

    with open_checkpointer(path, forbidden_roots=()) as saver:
        build().compile(checkpointer=saver).invoke({}, config=identity(version="v1").invoke_config(recursion_limit=50))

    with open_checkpointer(path, forbidden_roots=()) as saver:
        graph = build().compile(checkpointer=saver)
        assert graph.get_state(identity(version="v2").invoke_config(recursion_limit=50)).next == ()


# -- retention -------------------------------------------------------------

def _write_lineage(graph, run):
    graph.invoke({}, config=run.invoke_config(recursion_limit=50))


def test_deleting_one_lineage_leaves_the_others(private_root):
    """Per-unit retention is the point: one task's units share a database, and
    dropping one must not cost the rest their resume."""
    build, _ = _cycle()
    path = private_root / "cp.sqlite"
    doomed, kept = identity(unit_id="batch1"), identity(unit_id="batch2")

    with open_checkpointer(path, forbidden_roots=()) as saver:
        graph = build().compile(checkpointer=saver)
        _write_lineage(graph, doomed)
        _write_lineage(graph, kept)

        delete_checkpoint_lineage(saver, doomed)

        assert graph.get_state(doomed.invoke_config(recursion_limit=50)).values == {}
        assert graph.get_state(kept.invoke_config(recursion_limit=50)).values["trail"]


def test_a_deleted_lineage_starts_clean_afterwards(private_root):
    build, ran = _cycle()
    path = private_root / "cp.sqlite"
    run = identity()

    with open_checkpointer(path, forbidden_roots=()) as saver:
        build().compile(checkpointer=saver).invoke(
            {}, config=run.invoke_config(recursion_limit=50)
        )
        delete_checkpoint_lineage(saver, run)

    ran.clear()
    with open_checkpointer(path, forbidden_roots=()) as saver:
        build().compile(checkpointer=saver).invoke(
            {}, config=run.invoke_config(recursion_limit=50)
        )
    assert ran == ["expensive", "cheap"], "the lineage was not really gone"


def test_a_saver_without_the_capability_says_so_and_changes_nothing():
    class NoDeletion:
        pass

    with pytest.raises(CheckpointCapabilityError, match="NoDeletion"):
        delete_checkpoint_lineage(NoDeletion(), identity())


def test_an_inherited_unimplemented_delete_is_not_treated_as_support():
    """`BaseCheckpointSaver.delete_thread` exists and raises, so `hasattr` is
    not evidence. A saver that never overrode it must fail, not appear to
    succeed while retention quietly deletes nothing."""
    class InheritsTheStub:
        def delete_thread(self, thread_id):
            raise NotImplementedError

    with pytest.raises(CheckpointCapabilityError, match="inherits"):
        delete_checkpoint_lineage(InheritsTheStub(), identity())


def test_deletion_never_writes_langgraph_s_own_tables():
    """The previous revision fell back to `DELETE FROM checkpoints` through the
    saver's connection. That schema is not ours; a rename in a minor release
    turns retention into a silent no-op."""
    import agent_core.workflow.checkpoints as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "DELETE FROM" not in source.upper()
    assert ".conn" not in source, "it reached for the saver's connection again"
