"""Rendering for shared UI surfaces.

Pure functions from data to strings; products supply routing and responses. See
:mod:`agent_core.ui.inbox`.
"""

from agent_core.ui.inbox import (
    gate_to_dict,
    humanize_wait,
    is_stale,
    render_gate,
    render_inbox,
    render_prompt,
)

__all__ = [
    "render_inbox",
    "render_gate",
    "render_prompt",
    "gate_to_dict",
    "humanize_wait",
    "is_stale",
]
