"""Launcher regressions and real CLI integration with HTTP mocked, never live."""

import fcntl
import json
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap

import pytest

from agent_core.model_selection.cache import CatalogCache, CatalogUnavailable
from agent_core.model_selection.configuration import load_selection_config
from agent_core.model_selection.runtime import DiscoveryRuntime, NoAvailableModels, credential_scope
from agent_core.model_selection.sources import parse_benchmarks


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts/refresh-model-catalog"
AA_KEY = "ARTIFICIAL_ANALYSIS_API_KEY"
PROVIDER_SECRET = "$(touch NEVER_EXECUTE)"
AA_SECRET = "launcher-aa-sentinel"


@pytest.fixture
def launch_files(tmp_path):
    root = tmp_path / "paths with spaces"
    root.mkdir()
    cache = root / "cache"
    cache.mkdir()
    cache.chmod(0o2770)
    private = root / "private"
    private.mkdir(mode=0o700)
    secret = private / "credentials.json"
    secret.write_text(json.dumps({"POOL_API_KEY": PROVIDER_SECRET, AA_KEY: AA_SECRET}))
    secret.chmod(0o600)
    config = root / "config.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "cache_root": str(cache),
        "availability_db": str(cache / "availability.sqlite"),
        "providers": [{"id": "pool", "base_url": "https://provider.example/v1",
                       "credential_scope_id": "fixture", "credential_generation": "1",
                       "api_key_env": "POOL_API_KEY"}],
        "bindings": {"pool/a": {"benchmark_id": "a", "capability_approved": True}},
        "policy": {"application_id": "fixture"},
    }))
    return config, secret


@pytest.fixture
def python_shim(tmp_path):
    # -I must import the installed editable package, not pytest's injected src path.
    probe = subprocess.run(
        [sys.executable, "-I", "-c", "import agent_core; print(agent_core.__file__)"],
        capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0, "Run with a venv containing agent-core installed editable"
    assert Path(probe.stdout.strip()).resolve() == ROOT / "src/agent_core/__init__.py"
    shim = tmp_path / "fixture-python"
    shim.write_text(f"#!{sys.executable} -I\n" + textwrap.dedent('''\
        import fcntl
        import json
        import os
        from pathlib import Path
        import runpy
        import sys

        if sys.argv[1:3] == ['-I', '-']:
            os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
        if sys.argv[1:5] != ['-I', '-m', 'agent_core.model_selection', 'refresh']:
            sys.exit(90)
        fixture_path = Path(__file__).with_suffix('.json')
        fixture = json.loads(fixture_path.read_text())
        config_path = sys.argv[6]
        config = json.loads(Path(config_path).read_text())
        if os.environ.get('AGENT_MODEL_SELECTION_CONFIG') != config_path:
            sys.exit(91)
        if (os.environ.get('POOL_API_KEY') != fixture['provider_secret']
                or os.environ.get('ARTIFICIAL_ANALYSIS_API_KEY') != fixture['aa_secret']):
            sys.exit(92)
        if any(key in os.environ for key in ('UNDECLARED_SECRET', 'PYTHONPATH', 'ARTIFICAL_ANALYSIS_KEY')):
            sys.exit(93)
        evidence = {'argv': sys.argv[1:], 'isolated': sys.flags.isolated,
                    'umask': os.umask(0o007), 'cwd': os.getcwd(),
                    'config': config, 'calls': [],
                    'threshold': os.environ.get('AGENT_MODEL_CODING_INDEX_MIN')}
        # Assert lock survives exec without emitting credential values.
        with (Path(config['cache_root']) / 'refresh-launcher.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                evidence['locked'] = True
            else:
                evidence['locked'] = False

        def save_evidence():
            fixture_path.with_suffix('.result.json').write_text(json.dumps(evidence))

        if not fixture['real_cli']:
            save_evidence()
            sys.exit(fixture.get('exit_code', 0))

        # Only replace the HTTP transport. CLI parsing, sources, refresh, locks,
        # status, availability and atomic catalog publication are production code.
        import httpx
        def handle(request):
            if str(request.url) == 'https://artificialanalysis.ai/api/v2/data/llms/models':
                if request.headers.get('x-api-key') != fixture['aa_secret']:
                    sys.exit(94)
                evidence['calls'].append('benchmark')
                return httpx.Response(200, json={'data': [{
                    'id': 'a', 'name': 'a', 'slug': 'a',
                    'evaluations': {'artificial_analysis_coding_index': 80},
                    'untrusted_secret': fixture['aa_secret'],
                }]})
            if str(request.url) == 'https://openrouter.ai/api/v1/models':
                if request.headers.get('Authorization'):
                    sys.exit(97)
                evidence['calls'].append('pricing')
                return httpx.Response(200, json={'data': [
                    {'id': 'vendor/a', 'pricing': {'prompt': '0.000001',
                                                   'completion': '0.000003'}}]})
            if str(request.url) != 'https://provider.example/v1/models':
                sys.exit(95)
            if request.headers.get('Authorization') != 'Bearer ' + fixture['provider_secret']:
                sys.exit(96)
            evidence['calls'].append('inventory')
            return httpx.Response(fixture.get('provider_status', 200), json={'data': [{'id': 'a'}]})

        class FixtureClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs['transport'] = httpx.MockTransport(handle)
                super().__init__(*args, **kwargs)

        httpx.AsyncClient = FixtureClient
        sys.argv = ['agent_core.model_selection', *sys.argv[4:]]
        try:
            runpy.run_module('agent_core.model_selection', run_name='__main__')
        finally:
            save_evidence()
    '''))
    shim.chmod(0o700)
    settings = {"real_cli": False, "provider_secret": PROVIDER_SECRET, "aa_secret": AA_SECRET}
    settings_path = shim.with_suffix(".json")
    settings_path.write_text(json.dumps(settings))
    settings_path.chmod(0o600)
    return shim


def update_json(path, **updates):
    data = json.loads(path.read_text())
    data.update(updates)
    path.write_text(json.dumps(data))


def launch(files, python, *, args=None, environ=None):
    config, secret = files
    arguments = args if args is not None else [
        "--python", str(python), "--config", str(config), "--secret-env-file", str(secret),
    ]
    result = subprocess.run(
        [str(LAUNCHER), *arguments], cwd=config.parent, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(config.parent),
             "UNDECLARED_SECRET": "unrelated-sentinel", AA_KEY: "inherited-wrong-key",
             **(environ or {})}, timeout=15,
    )
    # Test output is always sanitized, including failure diagnostics.
    for value in (PROVIDER_SECRET, AA_SECRET, "rotated-provider-sentinel",
                  "unrelated-sentinel", "inherited-wrong-key"):
        if value in result.stdout + result.stderr:
            pytest.fail("launcher subprocess leaked a credential sentinel", pytrace=False)
    return result


def test_launcher_contract_lock_and_environment(launch_files, python_shim):
    result = launch(launch_files, python_shim, environ={"AGENT_MODEL_CODING_INDEX_MIN": "0"})
    assert result.returncode == 0, result.stderr
    evidence = json.loads(python_shim.with_suffix(".result.json").read_text())
    assert evidence["argv"] == ["-I", "-m", "agent_core.model_selection", "refresh", "--config", str(launch_files[0])]
    assert evidence["isolated"] == 1
    assert evidence["umask"] == 0o007
    assert evidence["cwd"] == "/"
    assert evidence["threshold"] == "0"
    assert evidence["locked"]
    assert not (launch_files[0].parent / "NEVER_EXECUTE").exists()
    assert (launch_files[0].parent / "cache/refresh-launcher.lock").stat().st_mode & 0o777 == 0o660


def test_alias_is_normalized(launch_files, python_shim):
    launch_files[1].write_text(json.dumps({"POOL_API_KEY": PROVIDER_SECRET, "ARTIFICAL_ANALYSIS_KEY": AA_SECRET}))
    assert launch(launch_files, python_shim).returncode == 0


def test_child_failure_exit_is_preserved(launch_files, python_shim):
    update_json(python_shim.with_suffix(".json"), exit_code=23)
    assert launch(launch_files, python_shim).returncode == 23


def test_overlap_does_not_invoke_cli(launch_files, python_shim):
    with (launch_files[0].parent / "cache/refresh-launcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert launch(launch_files, python_shim).returncode == 75
    assert not python_shim.with_suffix(".result.json").exists()


@pytest.mark.parametrize("content", [
    "export POOL_API_KEY=not-json", '{"PATH":"evil"}', '{"POOL_API_KEY":123}',
    '{"POOL_API_KEY":"x","POOL_API_KEY":"y"}', '[]', '{}',
    '{"POOL_API_KEY":"x","ARTIFICIAL_ANALYSIS_API_KEY":"a","ARTIFICAL_ANALYSIS_KEY":"b"}',
])
def test_reject_unsafe_credentials(launch_files, python_shim, content):
    launch_files[1].write_text(content)
    assert launch(launch_files, python_shim).returncode == 2
    assert not python_shim.with_suffix(".result.json").exists()


@pytest.mark.parametrize("key", ["PATH", "PYTHONPATH", "LD_PRELOAD", "AGENT_MODEL_SELECTION_CONFIG"])
def test_reject_declared_runtime_controls(launch_files, python_shim, key):
    config = json.loads(launch_files[0].read_text())
    config["providers"][0]["api_key_env"] = key
    launch_files[0].write_text(json.dumps(config))
    launch_files[1].write_text(json.dumps({key: "injection", AA_KEY: AA_SECRET}))
    assert launch(launch_files, python_shim).returncode == 2


@pytest.mark.parametrize("unsafe", ["file-mode", "directory-mode", "symlink"])
def test_reject_unsafe_secret_permissions(launch_files, python_shim, unsafe):
    secret = launch_files[1]
    if unsafe == "file-mode":
        secret.chmod(0o640)
    elif unsafe == "directory-mode":
        secret.parent.chmod(0o750)
    else:
        actual = secret.with_suffix(".actual")
        secret.rename(actual)
        secret.symlink_to(actual)
    assert launch(launch_files, python_shim).returncode == 2


@pytest.mark.parametrize("index", [1, 3, 5])
def test_reject_relative_paths(launch_files, python_shim, index):
    args = ["--python", str(python_shim), "--config", str(launch_files[0]), "--secret-env-file", str(launch_files[1])]
    args[index] = "relative"
    assert launch(launch_files, python_shim, args=args).returncode == 2


def test_cron_schedule_and_absolute_paths():
    lines = (ROOT / "scripts/model-catalog.cron.example").read_text().splitlines()
    jobs = [shlex.split(line) for line in lines if line and not line.startswith("#") and "=" not in line]
    assert len(jobs) == 1
    job = jobs[0]
    assert job[:6] == ["15", "3", "*", "*", "*", "agent-model-refresh"]
    assert all(Path(job[index]).is_absolute() for index in (6, 8, 10, 12, 14))
    assert not any(line.startswith("CRON_TZ=") for line in lines)


def test_documented_config_matches_actual_schema(tmp_path):
    usage = (ROOT / "docs/usage-model-discovery.md").read_text()
    example = json.loads(usage.split("```json\n", 1)[1].split("```", 1)[0])
    path = tmp_path / "documented-config.json"
    path.write_text(json.dumps(example))
    config = load_selection_config(path, environ={})
    assert config.bindings == {}
    assert config.providers[0].credential_generation == "1"


@pytest.mark.parametrize("invalid", [None, "missing", "disabled", "mismatch"])
@pytest.mark.parametrize("example,variant", [(2, "careful"), (3, "high")])
def test_documented_variant_registration(launch_files, invalid, example, variant):
    usage = (ROOT / "docs/usage-model-discovery.md").read_text()
    mapping = json.loads(usage.split("```json\n")[example].split("```", 1)[0])
    config_path = launch_files[0]
    update_json(config_path, **mapping)
    config = load_selection_config(config_path, environ={})
    records = parse_benchmarks({"data": [{
        "id": "aa-model-a-high", "name": "Model A (high)", "slug": "model-a-high",
        "evaluations": {"artificial_analysis_coding_index": 80},
    }]})
    CatalogCache(config).publish(inventory={"pool": ["model-a"]},
                                 benchmarks={"records": [r.model_dump() for r in records]})
    if invalid:
        options = {"missing": {}, "disabled": {"disabled": True},
                   "mismatch": {"reasoningEffort": "low"}}[invalid]
        config = config.model_copy(update={"variant_options": {"pool/model-a": options}})
    runtime = DiscoveryRuntime(config, environ={"POOL_API_KEY": "fixture-provider"})
    if invalid:
        reason = "variant_effort_mismatch" if invalid == "mismatch" else "variant_mapping_required"
        with pytest.raises(NoAvailableModels, match=reason):
            runtime.resolve()
    else:
        selection = runtime.resolve()
        updates = runtime.config_updates(selection, "pool/model-a")
        assert updates["opencode_variant"] == variant
        assert updates["opencode_discovery_variants"] == {
            "pool/model-a": {variant: {"reasoningEffort": "high"}},
        }


def test_actual_cli_refresh_worker_restart_rotation_and_failure(launch_files, python_shim):
    update_json(python_shim.with_suffix(".json"), real_cli=True)
    config_path, secret_path = launch_files
    config = load_selection_config(config_path, environ={})
    cache = CatalogCache(config)
    for generation in ("1", "2"):
        raw = json.loads(config_path.read_text())
        raw["providers"][0]["credential_generation"] = generation
        config_path.write_text(json.dumps(raw))
        if generation == "2":
            rotated = "rotated-provider-sentinel"
            secret_path.write_text(json.dumps({"POOL_API_KEY": rotated, AA_KEY: AA_SECRET}))
            update_json(python_shim.with_suffix(".json"), provider_secret=rotated)
            config = load_selection_config(config_path, environ={})
            with pytest.raises(CatalogUnavailable, match="scope changed"):
                CatalogCache(config).load()
        result = launch(launch_files, python_shim)
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["last_error_code"] is None
        assert status["provider_models"] == status["benchmark_models"] == 1
        evidence = json.loads(python_shim.with_suffix(".result.json").read_text())
        assert evidence["calls"] == ["benchmark", "inventory", "pricing"]
        assert evidence["locked"]
        cache = CatalogCache(config)
        assert cache.load()["inventory"] == {"pool": ["a"]}
        # Fresh worker instances read the same trusted file/cache with no AA key.
        worker_env = {"POOL_API_KEY": json.loads(secret_path.read_text())["POOL_API_KEY"]}
        for _ in range(2):
            worker_config = load_selection_config(config_path, environ={})
            worker = DiscoveryRuntime(worker_config, environ=worker_env)
            assert worker_config.cache_root == config.cache_root
            assert credential_scope(worker_config.providers[0]) == credential_scope(config.providers[0])
            assert [c.identity for c in worker.resolve().candidates] == ["pool/a"]
    original = cache.path.read_bytes()
    update_json(python_shim.with_suffix(".json"), provider_status=401)
    result = launch(launch_files, python_shim)
    assert result.returncode == 1
    assert "auth_error" in result.stderr
    assert cache.path.read_bytes() == original
    status = json.loads((config.cache_root / "refresh-status.json").read_text())
    assert status["last_error_code"] == "auth_error"
    assert status["last_success_at"] is not None
    assert not DiscoveryRuntime(config, environ={}).is_healthy("pool/a")
    update_json(python_shim.with_suffix(".json"), provider_status=200)
    assert launch(launch_files, python_shim).returncode == 0
    assert DiscoveryRuntime(config, environ={}).is_healthy("pool/a")
    for path in config.cache_root.iterdir():
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o660
            data = path.read_bytes()
            if any(value.encode() in data for value in (PROVIDER_SECRET, AA_SECRET, "rotated-provider-sentinel")):
                pytest.fail("operational state leaked a credential sentinel", pytrace=False)
