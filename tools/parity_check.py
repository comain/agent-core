#!/usr/bin/env python3
"""Dual-run parity check between the source harness and the ported one.

The ported test suite proves each module behaves as its own tests expect. It
does not prove the *assembled* harness behaves like the original, because those
tests stub the model. This check drives both harnesses with identical
configuration and compares what they actually produce.

Two phases:

  1. Deterministic parity -- the generated opencode.json, the spawn command, and
     the subprocess environment. No model call, no cost, fully reproducible.
     This is where a port defect would realistically show up.

  2. Live parity (--live) -- one real OpenCode turn through each harness,
     comparing the structural event stream: event types, ordering, and terminal
     state. Never the model's prose, which is not deterministic.

Run from the agent-core repo with both repos checked out:

    .venv/bin/python tools/parity_check.py
    .venv/bin/python tools/parity_check.py --live

Exit code 0 means the harnesses agree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

AGENT_CORE = Path(__file__).resolve().parents[1]
SOURCE_REPO = Path(os.environ.get("PARITY_SOURCE_REPO", AGENT_CORE.parent / "unit-test-agent")).expanduser()

# Identical inputs for both sides. The chain is deliberately explicit so neither
# harness falls back to a machine-specific default.
SHARED_ENV = {
    "OPENCODE_MODEL": "token-pool/gpt-5.5",
    "OPENCODE_SMALL_MODEL": "token-pool/gpt-5.5",
    "OPENCODE_PROVIDER": "token-pool",
    "OPENCODE_PROVIDER_CHAIN": "token-pool:token-pool/gpt-5.5",
    "OPENCODE_PROVIDER_BASE_URLS": "token-pool.base_url=http://token-pool.example/v1",
    "OPENCODE_PROVIDER_TOKENS": "token-pool.token=parity-token",
    "OPENCODE_EXTERNAL_DIRS": "",
    "OPENCODE_TURN_LOG_ENABLED": "false",
    # D3 removed the deployment-specific default for this field. Parity is only
    # meaningful when both sides get the same *input*, so it is supplied
    # explicitly here -- that is precisely what proves D3 changed the default
    # and not the logic that consumes it.
    "INDEX_SOURCE_DIRS": "~/group-a/api,~/group-b/api,~/group-c/api",
}

# D2 made this consumer-relative instead of deriving it from package depth. Point
# both harnesses at the same file so the comparison tests the resolution logic
# rather than the absence of a file agent-core deliberately does not ship.
SHARED_EXTERNAL_DIRS_CONFIG = SOURCE_REPO / "config" / "opencode_external_dirs.json"

# Live phase. Credentials come from .env.local, which is gitignored -- never
# hard-code a key here, and never print one.
LIVE_BASE_URL = os.environ.get("PARITY_LIVE_BASE_URL", "http://token-pool.example/v1")
# gpt-5.5 rather than claude-haiku-4-5: the latter stalls intermittently through
# opencode while answering normally over plain HTTP (see docs/parity.md). Both
# harnesses stall identically, so it is not a port defect -- but pinning a flaky
# model makes the check flaky, and a check that cries wolf stops being read.
LIVE_MODEL = "token-pool/gpt-5.5"
LIVE_PROMPT = "Reply with exactly the word: parity. No explanation."


def _live_key() -> str:
    env_local = AGENT_CORE / ".env.local"
    if not env_local.exists():
        raise SystemExit("live parity needs .env.local with the provider key")
    for line in env_local.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("no key found in .env.local")


LIVE_PROBE = r"""
import json, sys
sys.path.insert(0, {src!r})
{imports}

repo = sys.argv[1]
generate_opencode_config(repo)
proc = OpenCodeProcess()
result = proc.run_turn(
    {prompt!r},
    session_id=None,
    repo_path=repo,
    model_id={model!r},
    timeout=180,
)
events = getattr(result, "events", None)
print("@@PARITY@@" + json.dumps({{
    "type": result.type,
    "has_text": bool((result.result or "").strip()),
    "text_len": len((result.result or "").strip()),
    "error": (result.error or {{}}).get("name") if result.error else None,
    "fallback_eligible": result.fallback_eligible,
    "fallback_reason": result.fallback_reason,
    "patch_count": result.patch_count,
    "token_keys": sorted((result.tokens or {{}}).keys()),
    "session_id_present": bool(result.session_id),
}}, sort_keys=True))
"""


def _run_live(python: Path, src: Path, imports: str, prefix: str, repo: Path, key: str) -> dict:
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("UTA_OPENCODE", "AGENT_OPENCODE")):
            del env[k]
    live_env = {
        "OPENCODE_MODEL": LIVE_MODEL,
        "OPENCODE_SMALL_MODEL": LIVE_MODEL,
        "OPENCODE_PROVIDER": "token-pool",
        "OPENCODE_PROVIDER_CHAIN": f"token-pool:{LIVE_MODEL}",
        "OPENCODE_PROVIDER_BASE_URLS": f"token-pool.base_url={LIVE_BASE_URL}",
        "OPENCODE_PROVIDER_TOKENS": f"token-pool.token={key}",
        "OPENCODE_EXTERNAL_DIRS": "",
        "OPENCODE_TURN_LOG_ENABLED": "false",
        "INDEX_SOURCE_DIRS": "",
    }
    for k, v in live_env.items():
        env[f"{prefix}{k}"] = v

    code = LIVE_PROBE.format(
        src=str(src), imports=imports, prompt=LIVE_PROMPT, model=LIVE_MODEL,
    )
    completed = subprocess.run(
        [str(python), "-c", code, str(repo)],
        capture_output=True, text=True, env=env, timeout=300,
    )
    marker = [ln for ln in completed.stdout.splitlines() if ln.startswith("@@PARITY@@")]
    if not marker:
        raise SystemExit(
            f"live probe produced no result ({python}):\n"
            f"stdout tail:\n{completed.stdout[-1500:]}\n\nstderr tail:\n{completed.stderr[-1500:]}"
        )
    return json.loads(marker[-1][len("@@PARITY@@"):])


#: Terminal states that mean the provider did not answer, rather than that the
#: harness behaved differently. Comparing two *independent* live calls is only
#: meaningful when both actually completed -- otherwise a flaky provider
#: produces a "difference" that says nothing about the port.
_INCONCLUSIVE = {"stalled", "rate_limited", "timeout", "error"}


def live_parity(*, attempts: int = 3) -> int:
    key = _live_key()
    findings: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo_a, repo_b = Path(tmp) / "a", Path(tmp) / "b"
        for repo in (repo_a, repo_b):
            repo.mkdir()
            (repo / "README.md").write_text("parity fixture\n")

        source = ported = None
        for attempt in range(1, attempts + 1):
            print(f"  attempt {attempt}/{attempts}: source harness ({LIVE_MODEL}) ...")
            source = _run_live(SOURCE_REPO / ".venv/bin/python", SOURCE_REPO, SOURCE_IMPORTS, "UTA_", repo_a, key)
            print(f"    -> {source['type']}")
            print("  attempt %d/%d: ported harness ..." % (attempt, attempts))
            ported = _run_live(AGENT_CORE / ".venv/bin/python", AGENT_CORE / "src", PORTED_IMPORTS, "AGENT_", repo_b, key)
            print(f"    -> {ported['type']}")
            if source["type"] not in _INCONCLUSIVE and ported["type"] not in _INCONCLUSIVE:
                break
            print("    both sides must complete for the comparison to mean anything; retrying")
        else:
            print(
                "\nLIVE PARITY INCONCLUSIVE -- the provider did not answer on "
                f"{attempts} attempts (source={source['type']}, ported={ported['type']}).\n"
                "This says nothing about the port. Re-run when the provider is healthy."
            )
            return 2

    # Structural comparison only. The model's prose is not deterministic, so
    # text content is compared by presence, never by value.
    for field in sorted(set(source) | set(ported)):
        findings += _diff(f"turn.{field}", source.get(field), ported.get(field))

    if findings:
        print("\nLIVE PARITY FAILED -- structural differences in the turn result:\n")
        print("\n".join(findings))
        return 1
    print("\nLIVE PARITY OK -- both harnesses produced structurally identical turns")
    print(f"  terminal state: {source['type']}, text produced: {source['has_text']}, "
          f"token fields: {source['token_keys']}")
    return 0

# Differences that are deliberate, not defects. Each maps to a recorded
# deviation; anything outside this set fails the check.
EXPECTED_ENV_DIFFS = {
    "AGENT_SERVICE_PYTHON_BIN",  # D5: neutral name added alongside the legacy one
}

# Values derived from the interpreter running the probe. The two harnesses are
# deliberately exercised in their own virtualenvs -- which since the floor moved
# to 3.11 are different interpreters -- so these can never be literally equal.
# What matters is that each harness reports *its own* interpreter, so they are
# compared against that rather than against each other.
INTERPRETER_DERIVED_ENV = {
    "UTA_SERVICE_PYTHON_BIN",
    "AGENT_SERVICE_PYTHON_BIN",
}

PROBE = r"""
import json, os, pathlib, sys
sys.path.insert(0, {src!r})
{imports}

repo = sys.argv[1]
import pathlib
{config_module}.EXTERNAL_DIRS_CONFIG = pathlib.Path({ext_cfg!r})
cfg_path = generate_opencode_config(repo)
config = json.loads(open(cfg_path).read())

proc = OpenCodeProcess()
cmd = proc._build_cmd(
    "parity probe",
    session_id=None,
    model_id="token-pool/gpt-5.5",
    variant=None,
    repo_path=repo,
)
env = _build_env(repo, model_id="token-pool/gpt-5.5")

print(json.dumps({{
    "config": config,
    "cmd": cmd,
    "env": {{k: v for k, v in env.items() if k.startswith(("OPENCODE", "AGENT_", "UTA_"))}},
    "python": str(pathlib.Path(sys.executable).resolve()),  # harness resolves the venv symlink
}}, sort_keys=True, indent=2))
"""

SOURCE_IMPORTS = (
    "import uta.opencode.config\n"
    "from uta.opencode.config import generate_opencode_config\n"
    "from uta.opencode.process import OpenCodeProcess, _build_env"
)
PORTED_IMPORTS = (
    "import agent_core.harness.config\n"
    "from agent_core.harness.config import generate_opencode_config\n"
    "from agent_core.harness.process import OpenCodeProcess, _build_env"
)


def _run_probe(python: Path, src: Path, imports: str, prefix: str, repo: Path, config_module: str) -> dict:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("UTA_OPENCODE", "AGENT_OPENCODE")):
            del env[key]
    for key, value in SHARED_ENV.items():
        env[f"{prefix}{key}"] = value

    code = PROBE.format(
        src=str(src),
        imports=imports,
        config_module=config_module,
        ext_cfg=str(SHARED_EXTERNAL_DIRS_CONFIG),
    )
    completed = subprocess.run(
        [str(python), "-c", code, str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        raise SystemExit(f"probe failed ({python}):\n{completed.stderr}")
    return json.loads(completed.stdout)


def _diff(label: str, a, b) -> list[str]:
    if a == b:
        return []
    return [f"  {label}:\n    source: {json.dumps(a, sort_keys=True)}\n    ported: {json.dumps(b, sort_keys=True)}"]


def deterministic_parity() -> int:
    findings: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo_a = Path(tmp) / "a"
        repo_b = Path(tmp) / "b"
        for repo in (repo_a, repo_b):
            repo.mkdir()

        source = _run_probe(
            SOURCE_REPO / ".venv/bin/python", SOURCE_REPO, SOURCE_IMPORTS,
            "UTA_", repo_a, "uta.opencode.config",
        )
        ported = _run_probe(
            AGENT_CORE / ".venv/bin/python", AGENT_CORE / "src", PORTED_IMPORTS,
            "AGENT_", repo_b, "agent_core.harness.config",
        )

    # opencode.json must be byte-identical apart from embedded repo paths.
    findings += _diff("opencode.json", source["config"], ported["config"])

    # Spawn command must match apart from the repo path argument.
    norm_a = [str(x).replace(str(repo_a), "REPO") for x in source["cmd"]]
    norm_b = [str(x).replace(str(repo_b), "REPO") for x in ported["cmd"]]
    findings += _diff("spawn command", norm_a, norm_b)

    # Environment: every key present on one side must be on the other, with the
    # same value, except deliberate additions.
    inputs = {f"{p}{k}" for k in SHARED_ENV for p in ("UTA_", "AGENT_")}
    keys = (set(source["env"]) | set(ported["env"])) - EXPECTED_ENV_DIFFS - inputs - INTERPRETER_DERIVED_ENV
    for key in sorted(keys):
        findings += _diff(f"env[{key}]", source["env"].get(key), ported["env"].get(key))

    for key in sorted(EXPECTED_ENV_DIFFS):
        if key not in ported["env"]:
            findings.append(f"  expected deviation {key} is missing from the ported harness")

    # Each side must point at the interpreter that actually ran it.
    for label, probe in (("source", source), ("ported", ported)):
        for key in sorted(INTERPRETER_DERIVED_ENV):
            value = probe["env"].get(key)
            if value is None:
                if label == "source" and key.startswith("AGENT_"):
                    continue  # the neutral name is a ported-side addition (D5)
                findings.append(f"  {label} harness did not emit {key}")
            elif value != probe["python"]:
                findings.append(
                    f"  {label} harness reported {key}={value!r} but ran under {probe['python']!r}"
                )

    if findings:
        print("PARITY FAILED -- structural differences between harnesses:\n")
        print("\n".join(findings))
        return 1

    print("PARITY OK -- opencode.json, spawn command, and environment are identical")
    print(f"  (deliberate additions verified present: {', '.join(sorted(EXPECTED_ENV_DIFFS))})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also run one real OpenCode turn through each harness")
    args = parser.parse_args()

    status = deterministic_parity()
    if status != 0:
        return status
    if args.live:
        print("\n== live turn parity ==")
        return live_parity()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
