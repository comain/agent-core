"""Environment isolation at agent launch boundaries.

Production AA credentials belong to a refresh-only OS identity and secret file
inaccessible to the worker. Filtering environment variables does not isolate
worker-readable files.
"""

from collections.abc import Mapping


def sanitize_agent_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy the fully merged child environment without benchmark credentials."""
    sanitized = dict(env)
    sanitized.pop("ARTIFICIAL_ANALYSIS_API_KEY", None)
    sanitized.pop("ARTIFICAL_ANALYSIS_KEY", None)
    return sanitized
