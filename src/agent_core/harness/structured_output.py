"""Extract structured JSON objects from agent prose."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional


_OPEN_TO_CLOSE = {"{": "}", "[": "]"}
_CLOSE_TO_OPEN = {"}": "{", "]": "["}


def extract_json_object(
    text: str,
    *,
    required_keys: Iterable[str] = (),
) -> Dict[str, Any]:
    """Return the last usable JSON object embedded in an agent answer.

    Progress prose and earlier draft objects are tolerated. When an otherwise
    valid object has adjacent closing braces and brackets swapped, repair that
    narrow formatting error before rejecting the answer.
    """
    decoder = json.JSONDecoder()
    content = text or "{}"
    required = set(required_keys)
    candidates: list[tuple[int, int, Dict[str, Any]]] = []
    for index, char in enumerate(content):
        if char != "{":
            continue
        if any(start < index < end for start, end, _value in candidates):
            continue
        try:
            value, end = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((index, index + end, value))
    fallback = candidates[-1][2] if candidates else None
    matching = [value for _start, _end, value in candidates if required.issubset(value)]
    match = matching[-1] if matching else None
    if required and match is not None:
        return match
    if not required and fallback is not None:
        return fallback
    repaired_match = _extract_repaired_json_object(content, required=required)
    if repaired_match is not None:
        return repaired_match
    if fallback is None:
        raise json.JSONDecodeError("no JSON object found", content, 0)
    raise json.JSONDecodeError(
        "no JSON object matching required keys found",
        content,
        0,
    )


def _extract_repaired_json_object(
    content: str,
    *,
    required: set[str],
) -> Optional[Dict[str, Any]]:
    for index, char in enumerate(content):
        if char != "{":
            continue
        candidate = _find_brace_balanced_object(content, index)
        if candidate is None:
            continue
        repaired = _repair_swapped_closing_delimiters(candidate)
        if repaired == candidate:
            continue
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (not required or required.issubset(value)):
            return value
    return None


def _find_brace_balanced_object(content: str, start: int) -> Optional[str]:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    return None


def _repair_swapped_closing_delimiters(candidate: str) -> str:
    repaired: list[str] = []
    stack: list[str] = []
    in_string = False
    escape = False
    changed = False
    index = 0
    while index < len(candidate):
        char = candidate[index]
        if in_string:
            repaired.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            repaired.append(char)
            index += 1
            continue
        if char in _OPEN_TO_CLOSE:
            stack.append(char)
            repaired.append(char)
            index += 1
            continue
        if char in _CLOSE_TO_OPEN:
            if not stack:
                return candidate
            expected = _OPEN_TO_CLOSE[stack[-1]]
            if char == expected:
                stack.pop()
                repaired.append(char)
                index += 1
                continue
            if index + 1 < len(candidate) and candidate[index + 1] == expected:
                stack.pop()
                if stack and char == _OPEN_TO_CLOSE[stack[-1]]:
                    repaired.append(expected)
                    repaired.append(char)
                    stack.pop()
                    changed = True
                    if index + 2 < len(candidate) and candidate[index + 2] == char:
                        index += 3
                    else:
                        index += 2
                    continue
            return candidate
        repaired.append(char)
        index += 1
    if stack:
        return candidate
    return "".join(repaired) if changed else candidate
