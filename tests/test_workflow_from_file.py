"""Loading a workflow from a file rather than a Python literal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.workflow import END, NodeRegistry, WorkflowSpec, WorkflowSpecError
from agent_core.workflow.graph import build_graph

FLOW = """
name: review
# The comment is the point of YAML over JSON: a flow encodes decisions, and
# the reason a step runs where it does belongs next to it.
nodes:
  - prepare
  - {name: light, uses: review_step, config: {depth: 1}}
  - report
entry: prepare
edges:
  - [prepare, light]
  - [light, report]
  - [report, __end__]
"""


def test_a_workflow_can_be_written_in_yaml(tmp_path: Path):
    path = tmp_path / "flow.yaml"
    path.write_text(FLOW, encoding="utf-8")

    spec = WorkflowSpec.from_file(path)

    assert spec.name == "review"
    assert [n.name for n in spec.nodes] == ["prepare", "light", "report"]
    assert spec.node("light").uses == "review_step"
    assert spec.node("light").config == {"depth": 1}


def test_yml_is_accepted_too(tmp_path: Path):
    path = tmp_path / "flow.yml"
    path.write_text(FLOW, encoding="utf-8")
    assert WorkflowSpec.from_file(path).name == "review"


def test_a_workflow_can_be_written_in_json(tmp_path: Path):
    path = tmp_path / "flow.json"
    path.write_text(json.dumps({"name": "review", "nodes": ["a", "b"]}), encoding="utf-8")
    assert [n.name for n in WorkflowSpec.from_file(path).nodes] == ["a", "b"]


def test_a_loaded_workflow_builds_a_graph_that_runs(tmp_path: Path):
    """The file has to produce the same thing a literal does, not just parse."""
    path = tmp_path / "flow.yaml"
    path.write_text(FLOW, encoding="utf-8")

    registry = NodeRegistry()
    for name in ("prepare", "report"):
        registry.add_node(name, lambda state, n=name: {"trace": [*state.get("trace", []), n]})
    registry.add_node(
        "review_step",
        lambda state, config: {"trace": [*state.get("trace", []), f"review:{config['depth']}"]},
    )

    final = build_graph(WorkflowSpec.from_file(path), registry).invoke({})
    assert final["trace"] == ["prepare", "review:1", "report"]


# -- what a bad file says --------------------------------------------------------


def test_a_missing_file_names_the_path(tmp_path: Path):
    with pytest.raises(WorkflowSpecError, match="cannot read workflow"):
        WorkflowSpec.from_file(tmp_path / "nope.yaml")


def test_an_unknown_extension_says_what_is_supported(tmp_path: Path):
    path = tmp_path / "flow.toml"
    path.write_text("name = 'x'", encoding="utf-8")
    with pytest.raises(WorkflowSpecError, match="use .yaml, .yml or .json"):
        WorkflowSpec.from_file(path)


def test_malformed_json_names_the_file(tmp_path: Path):
    path = tmp_path / "flow.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkflowSpecError, match="invalid JSON in workflow"):
        WorkflowSpec.from_file(path)


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path):
    path = tmp_path / "flow.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(WorkflowSpecError, match="must be a mapping"):
        WorkflowSpec.from_file(path)


def test_a_validation_error_names_the_file_it_came_from(tmp_path: Path):
    """Otherwise the message describes a dict that could be from anywhere."""
    path = tmp_path / "broken.yaml"
    path.write_text("name: x\nnodes: [a]\nedges: [[a, ghost]]\n", encoding="utf-8")

    with pytest.raises(WorkflowSpecError, match=r"broken\.yaml: .*unknown node"):
        WorkflowSpec.from_file(path)
