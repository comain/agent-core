#!/usr/bin/env bash
# Port a harness test file from unit-test-agent to agent-core.
#
# The port is deliberately a pure string substitution. If a ported test needs
# anything beyond these rewrites to pass, that is a defect in the port of the
# module under test -- not a test to edit. See the port-defect rule in the spec.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_REPO="${SRC_REPO:-$ROOT/../unit-test-agent}"
DST_REPO="${DST_REPO:-$ROOT}"

for name in "$@"; do
  src="$SRC_REPO/tests/$name"
  dst="$DST_REPO/tests/$name"
  [ -f "$src" ] || { echo "missing: $src" >&2; exit 1; }
  sed \
    -e 's/from uta\.opencode\./from agent_core.harness./g' \
    -e 's/from uta\.opencode import/from agent_core.harness import/g' \
    -e 's/import uta\.opencode\./import agent_core.harness./g' \
    -e 's/"uta\.opencode\./"agent_core.harness./g' \
    -e 's/from uta\.config import settings/from agent_core.config import settings/g' \
    -e 's/from uta\.config import Settings/from agent_core.config import HarnessConfig as Settings/g' \
    -e 's/"uta\.config\.settings\./"agent_core.config.settings./g' \
    -e "s/'uta\\.config\\.settings\\./'agent_core.config.settings./g" \
    -e 's/uta_debug_log_dir/debug_log_dir/g' \
    -e 's/UTA_OPENCODE_/AGENT_OPENCODE_/g' \
    -e 's/UTA_OPENAI_API_KEY/AGENT_OPENAI_API_KEY/g' \
    -e 's/UTA_OPENAI_KEY/AGENT_OPENAI_KEY/g' \
    -e 's/UTA_BASE_URL/AGENT_BASE_URL/g' \
    "$src" > "$dst"
  echo "ported: $name"
done
