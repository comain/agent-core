import os

import pytest


@pytest.fixture(autouse=True)
def _pin_opencode_runtime(monkeypatch):
    """Keep harness tests on OpenCode 1.x unless a test pins 2.x itself.

    Auto-detect would otherwise follow whatever `opencode` is on PATH.
    """
    from agent_core.harness.opencode_runtime import reset_runtime_cache

    reset_runtime_cache()
    monkeypatch.setattr("agent_core.config.settings.opencode_major", 1)
    yield
    reset_runtime_cache()


@pytest.fixture
def fixtures_dir():
    return os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def private_root(tmp_path):
    """An owner-only directory, because that is what the secure paths require.

    `tmp_path` itself is created with the umask's mode (0755 here), and the
    checkpoint and artifact stores refuse a shared root rather than chmodding
    it. Every test that needs a root gets a private one from here, which also
    keeps the contract visible: a product supplies a private application-state
    directory, it does not get one repaired for it.
    """
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root
