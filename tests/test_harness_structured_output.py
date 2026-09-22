"""Tests for extracting structured objects from agent prose."""

import json

import pytest

from agent_core.harness import extract_json_object


def test_extract_json_object_uses_the_last_object_with_required_keys():
    payload = extract_json_object(
        'progress {"status":"draft"}\nfinal {"status":"done","items":[]}',
        required_keys=("status", "items"),
    )

    assert payload == {"status": "done", "items": []}


def test_extract_json_object_repairs_swapped_closing_delimiters():
    text = (
        "analysis\n"
        '{"accepted":[],"rejected":[{"candidate":{"name":"value",'
        '"detail":"quoted \\\"} and { braces"}]}],"rationale":""}'
    )

    with pytest.raises(json.JSONDecodeError):
        json.loads(text[text.index("{") :])

    payload = extract_json_object(text, required_keys=("accepted", "rejected"))

    assert payload["accepted"] == []
    assert payload["rationale"] == ""
    assert payload["rejected"][0]["candidate"] == {
        "name": "value",
        "detail": 'quoted "} and { braces',
    }


def test_extract_json_object_returns_the_last_object_without_a_schema():
    assert extract_json_object('draft {"n":1}\nfinal {"n":2}') == {"n": 2}


def test_extract_json_object_keeps_the_outer_response_object():
    payload = extract_json_object(
        '{"facts":[{"domain":"inventory"}],"meta":{"count":1}}'
    )

    assert payload == {
        "facts": [{"domain": "inventory"}],
        "meta": {"count": 1},
    }


def test_extract_json_object_treats_empty_text_as_an_empty_object():
    assert extract_json_object("") == {}


@pytest.mark.parametrize(
    "text",
    [
        "plain prose without an object",
        '{"items":[1}',
        '{"items":[nope},]}',
        '{"items":[]]]}',
        '{"items":',
    ],
)
def test_extract_json_object_rejects_unrepairable_answers(text):
    with pytest.raises(json.JSONDecodeError):
        extract_json_object(text, required_keys=("items",))


def test_extract_json_object_rejects_objects_without_required_keys():
    with pytest.raises(
        json.JSONDecodeError,
        match="no JSON object matching required keys found",
    ):
        extract_json_object('{"status":"done"}', required_keys=("items",))
