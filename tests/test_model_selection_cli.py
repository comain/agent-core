"""Subprocess integration checks, separate from in-process mutation tests."""

import os
import subprocess
import sys

from test_model_configuration import write_config


def test_cli_missing_cache_is_actionable_and_secret_free(tmp_path):
    path = write_config(tmp_path)
    env = {**os.environ, "PYTHONPATH": "src", "ARTIFICIAL_ANALYSIS_API_KEY": "secret-sentinel"}
    result = subprocess.run([sys.executable, "-m", "agent_core.model_selection", "explain", "--config", str(path)],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "catalog missing" in result.stderr
    assert "secret-sentinel" not in result.stdout + result.stderr


def test_cli_requires_explicit_absolute_config():
    result = subprocess.run([sys.executable, "-m", "agent_core.model_selection", "explain", "--config", "relative.json"],
                            env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "absolute" in result.stderr
