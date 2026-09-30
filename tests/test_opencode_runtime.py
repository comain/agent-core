"""OpenCode 1.x / 2.x runtime mapping."""

import base64

from agent_core.harness import opencode_runtime as runtime


def test_parse_major_reads_1_and_2():
    assert runtime.parse_major("1.14.40") == 1
    assert runtime.parse_major("opencode 1.18.31") == 1
    assert runtime.parse_major("2.0.11") == 2
    assert runtime.parse_major("opencode v2.0.6") == 2
    assert runtime.parse_major("@opencode/cli/2.0.5") == 2
    assert runtime.parse_major("not a version") is None


def test_major_uses_configured_pin(monkeypatch):
    monkeypatch.setattr("agent_core.config.settings.opencode_major", 2)
    assert runtime.major() == 2
    assert runtime.is_v2()
    monkeypatch.setattr("agent_core.config.settings.opencode_major", 1)
    assert runtime.major() == 1
    assert not runtime.is_v2()


def test_detect_major_is_cached(monkeypatch):
    runtime.reset_runtime_cache()
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1

        class Result:
            stdout = "2.0.11"
            stderr = ""

        return Result()

    monkeypatch.setattr("agent_core.config.settings.opencode_major", None)
    monkeypatch.setattr("agent_core.harness.opencode_runtime.subprocess.run", fake_run)
    assert runtime.detect_major("/tmp/opencode") == 2
    assert runtime.detect_major("/tmp/opencode") == 2
    assert calls["n"] == 1


def test_detect_major_fails_closed_to_v1(monkeypatch):
    runtime.reset_runtime_cache()
    monkeypatch.setattr("agent_core.config.settings.opencode_major", None)

    def boom(*args, **kwargs):
        raise FileNotFoundError("opencode")

    monkeypatch.setattr("agent_core.harness.opencode_runtime.subprocess.run", boom)
    assert runtime.detect_major("missing-bin") == 1


def test_detect_major_probes_configured_bin_not_path_name(monkeypatch):
    runtime.reset_runtime_cache()
    monkeypatch.setattr("agent_core.config.settings.opencode_major", None)
    monkeypatch.setattr(
        "agent_core.config.settings.opencode_bin",
        "/root/.opencode/bin/opencode",
    )
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args[0])

        class Result:
            stdout = "opencode v2.0.6" if args[0] == "/root/.opencode/bin/opencode" else "1.14.40"
            stderr = ""

        return Result()

    monkeypatch.setattr("agent_core.harness.opencode_runtime.subprocess.run", fake_run)
    assert runtime.detect_major() == 2
    assert runtime.keep_pure() is False
    assert seen == ["/root/.opencode/bin/opencode"]


def test_v2_http_paths_and_unwrap():
    import agent_core.config as config

    config.settings.opencode_major = 2
    try:
        assert runtime.health_path() == "/api/info"
        assert runtime.session_path() == "/api/session"
        assert runtime.prompt_path("ses_1") == "/api/session/ses_1/prompt"
        assert runtime.init_path("ses_1") == "/api/session/ses_1/command"
        assert runtime.provider_path() == "/api/provider"
        assert runtime.unwrap({"data": {"id": "ses_1"}}) == {"id": "ses_1"}
        assert runtime.session_id_of({"data": {"id": "ses_1"}}) == "ses_1"
        assert runtime.model_ref("token-pool", "gpt-5.5", variant="low") == {
            "providerID": "token-pool",
            "id": "gpt-5.5",
            "variant": "low",
        }
    finally:
        config.settings.opencode_major = 1


def test_v1_http_paths_stay_unprefixed():
    import agent_core.config as config

    config.settings.opencode_major = 1
    assert runtime.health_path() == "/session"
    assert runtime.prompt_path("ses_1") == "/session/ses_1/message"
    assert runtime.init_path("ses_1") == "/session/ses_1/init"
    assert runtime.unwrap({"id": "ses_1"}) == {"id": "ses_1"}
    assert runtime.model_ref("token-pool", "gpt-5.5") == {
        "providerID": "token-pool",
        "modelID": "gpt-5.5",
    }


def test_v2_cli_isolation_and_model_variant():
    import agent_core.config as config

    config.settings.opencode_major = 2
    try:
        assert runtime.run_isolation_args() == ["--standalone"]
        assert runtime.run_isolation_args(attach_url="http://127.0.0.1:4096") == [
            "--server",
            "http://127.0.0.1:4096",
        ]
        assert runtime.skip_permission_args() == ["--auto"]
        assert runtime.keep_pure() is False
        assert runtime.keep_print_logs() is True
        assert runtime.keep_project_dir_flag() is False
        assert runtime.model_cli_values("token-pool/gpt-5.5", "low") == (
            "token-pool/gpt-5.5#low",
            None,
        )
    finally:
        config.settings.opencode_major = 1


def test_permission_rules_from_v1_rename_actions_and_globs():
    rules = runtime.permission_rules_from_v1(
        {
            "edit": "deny",
            "bash": {"git push *": "ask"},
            "external_directory": {"/tmp/**": "allow"},
        }
    )
    assert {"action": "edit", "resource": "*", "effect": "deny"} in rules
    assert {"action": "shell", "resource": "git push *", "effect": "ask"} in rules
    assert {"action": "external_directory", "resource": "/tmp/*", "effect": "allow"} in rules


def test_providers_from_v1_uses_v2_native_packages():
    providers = runtime.providers_from_v1(
        {
            "tencent": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Tencent TokenHub",
                "options": {"baseURL": "https://example/v1"},
                "models": {
                    "glm-5": {
                        "name": "glm-5",
                        "options": {"num_ctx": 8},
                        "variants": {"medium": {"reasoningEffort": "medium"}},
                    }
                },
            }
        }
    )
    assert providers["tencent"]["package"] == "@opencode/ai/providers/openai-compatible"
    assert providers["tencent"]["settings"]["baseURL"] == "https://example/v1"
    assert providers["tencent"]["models"]["glm-5"]["settings"]["num_ctx"] == 8
    assert providers["tencent"]["models"]["glm-5"]["variants"] == [
        {"id": "medium", "settings": {"reasoningEffort": "medium"}}
    ]


def test_providers_from_v1_preserves_v2_variant_arrays():
    variants = [{"id": "medium", "settings": {"reasoningEffort": "medium"}}]
    providers = runtime.providers_from_v1(
        {"custom": {"models": {"coder": {"variants": variants}}}}
    )
    assert providers["custom"]["models"]["coder"]["variants"] == variants


def test_providers_from_v1_preserves_custom_packages():
    providers = runtime.providers_from_v1(
        {"custom": {"npm": "@acme/opencode-provider", "models": {"coder": {}}}}
    )
    assert providers["custom"]["package"] == "@acme/opencode-provider"


def test_v1_cli_isolation_keeps_attach_and_variant():
    import agent_core.config as config

    config.settings.opencode_major = 1
    assert runtime.run_isolation_args() == []
    assert runtime.run_isolation_args(attach_url="http://localhost:4096") == [
        "--attach",
        "http://localhost:4096",
    ]
    assert runtime.skip_permission_args() == ["--dangerously-skip-permissions"]
    assert runtime.keep_pure() is True
    assert runtime.model_cli_values("token-pool/gpt-5.5", "low") == (
        "token-pool/gpt-5.5",
        "low",
    )


def test_v2_auth_headers_use_basic_auth():
    import agent_core.config as config

    config.settings.opencode_major = 2
    config.settings.opencode_server_password = "secret"
    config.settings.opencode_server_username = "opencode"
    try:
        token = base64.b64encode(b"opencode:secret").decode("ascii")
        assert runtime.auth_headers() == {"Authorization": f"Basic {token}"}
    finally:
        config.settings.opencode_major = 1
        config.settings.opencode_server_password = ""
        config.settings.opencode_server_username = "opencode"
