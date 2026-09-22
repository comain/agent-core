#!/usr/bin/env python3
"""Remove tests that do not test the harness.

Every exclusion here is a test that exercises the *consumer's* code or data
rather than the harness: its settings model, its build tooling, or a data file
containing developer-specific absolute paths. Porting them would require
agent-core to reimplement or ship things it deliberately does not own.

This is not a way to make failing tests go away. Anything excluded must be
justified by what it tests, not by whether it passes.
"""
import pathlib
import re
import sys

EXCLUSIONS = {
    "test_opencode_config.py": {
        # Asserts the consumer repo's shipped data file exists and contains a
        # developer's absolute home path. Repo data, not
        # harness behaviour, and unshippable in a neutral package.
        "test_external_directory_config_file_exists",
        # Asserts settings.maven_bin. Maven is build tooling the harness never
        # reads; the field does not exist in HarnessConfig by design.
        "test_settings_accepts_maven_bin_env",
        # Asserts a historical typo alias (UTA_BAS_URL, missing the "E") that the
        # consumer added to tolerate a misspelled env var. D4 drops product-branded
        # credential aliases; carrying another product's typo into a shared package
        # is not defensible. Env-prefix binding is covered by test_config_scoping.
        "test_settings_accepts_uta_base_url_aliases",
    },
}


def main() -> int:
    for filename, names in EXCLUSIONS.items():
        path = pathlib.Path("tests") / filename
        text = path.read_text()
        for name in sorted(names):
            new_text = re.sub(rf"\n\ndef {name}\(.*?(?=\n\ndef |\Z)", "", text, flags=re.S)
            if new_text == text:
                print(f"WARNING: {filename}::{name} not found -- exclusion is stale", file=sys.stderr)
            text = new_text
        text = text.replace(
            "from agent_core.harness.config import CURSOR_PLUGIN_NAME, EXTERNAL_DIRS_CONFIG, generate_opencode_config",
            "from agent_core.harness.config import CURSOR_PLUGIN_NAME, generate_opencode_config",
        )
        path.write_text(text)
        print(f"{filename}: excluded {len(names)} non-harness tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
