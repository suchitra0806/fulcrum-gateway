"""Tests for runtime module helpers — registry, base types, token parsing."""

import json
from pathlib import Path

from ax_cli.runtimes.hermes.runtimes import (
    REGISTRY,
    BaseRuntime,
    RuntimeResult,
    StreamCallback,
    get_runtime,
    list_runtimes,
    register,
)
from ax_cli.runtimes.hermes.runtimes.hermes_sdk import (
    _extract_access_token_from_auth_json,
    _read_token_file,
    _resolve_codex_token,
    _resolve_provider_config,
)

# ---- RuntimeResult ----


def test_runtime_result_defaults():
    r = RuntimeResult(text="hello")
    assert r.text == "hello"
    assert r.session_id is None
    assert r.tool_count == 0
    assert r.files_written == []
    assert r.exit_reason == "done"
    assert r.elapsed_seconds == 0


def test_runtime_result_custom():
    r = RuntimeResult(text="done", session_id="s1", tool_count=3, exit_reason="timeout", elapsed_seconds=120)
    assert r.session_id == "s1"
    assert r.tool_count == 3
    assert r.exit_reason == "timeout"


# ---- StreamCallback ----


def test_stream_callback_methods():
    cb = StreamCallback()
    cb.on_text_delta("chunk")
    cb.on_text_complete("full text")
    cb.on_tool_start("shell", "running ls")
    cb.on_tool_end("shell", "done")
    cb.on_status("thinking")


# ---- Registry ----


def test_register_decorator():
    @register("test_runtime")
    class TestRuntime(BaseRuntime):
        def execute(self, message, **kw):
            return RuntimeResult(text="test")

    assert "test_runtime" in REGISTRY
    assert TestRuntime.name == "test_runtime"
    del REGISTRY["test_runtime"]


def test_get_runtime_unknown():
    import pytest

    with pytest.raises(ValueError, match="Unknown runtime"):
        get_runtime("nonexistent_runtime_xyz")


def test_list_runtimes():
    result = list_runtimes()
    assert isinstance(result, list)


# ---- hermes_sdk token helpers ----


def test_read_token_file(tmp_path):
    f = tmp_path / "token"
    f.write_text("  secret123  \n")
    f.chmod(0o600)
    assert _read_token_file(f) == "secret123"


def test_read_token_file_missing(tmp_path):
    assert _read_token_file(tmp_path / "missing") == ""


def test_read_token_file_loose_permissions(tmp_path):
    f = tmp_path / "token"
    f.write_text("secret")
    f.chmod(0o644)
    result = _read_token_file(f)
    assert result == "secret"


def test_extract_hermes_format(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "active_provider": "openai-codex",
                "providers": {"openai-codex": {"tokens": {"access_token": "hermes-token"}}},
            }
        )
    )
    assert _extract_access_token_from_auth_json(auth) == "hermes-token"


def test_extract_codex_cli_format(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "codex-token"}}))
    assert _extract_access_token_from_auth_json(auth) == "codex-token"


def test_extract_legacy_format(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"token": "legacy-token"}))
    assert _extract_access_token_from_auth_json(auth) == "legacy-token"


def test_extract_invalid_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text("not json")
    assert _extract_access_token_from_auth_json(auth) == ""


def test_extract_missing_file(tmp_path):
    assert _extract_access_token_from_auth_json(tmp_path / "missing.json") == ""


def test_extract_empty_providers(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"providers": {}}))
    assert _extract_access_token_from_auth_json(auth) == ""


# ---- _resolve_codex_token ----


def test_resolve_codex_token_env(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "env-codex-key")
    assert _resolve_codex_token() == "env-codex-key"


def test_resolve_codex_token_env_rejects_axp(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "axp_u_bad")
    import ax_cli.runtimes.hermes.runtimes.hermes_sdk as mod

    monkeypatch.setattr(mod, "HERMES_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_SHARED_TOKEN_PATH", Path("/nonexistent/token"))
    result = _resolve_codex_token()
    assert result == ""


def test_resolve_codex_token_hermes_auth(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    auth = tmp_path / "hermes_auth.json"
    auth.write_text(
        json.dumps(
            {
                "active_provider": "openai-codex",
                "providers": {"openai-codex": {"tokens": {"access_token": "hermes-tok"}}},
            }
        )
    )
    import ax_cli.runtimes.hermes.runtimes.hermes_sdk as mod

    monkeypatch.setattr(mod, "HERMES_AUTH_PATH", auth)
    monkeypatch.setattr(mod, "CODEX_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_SHARED_TOKEN_PATH", Path("/nonexistent/token"))
    assert _resolve_codex_token() == "hermes-tok"


def test_resolve_codex_token_codex_cli(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    auth = tmp_path / "codex_auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "codex-tok"}}))
    import ax_cli.runtimes.hermes.runtimes.hermes_sdk as mod

    monkeypatch.setattr(mod, "HERMES_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_AUTH_PATH", auth)
    monkeypatch.setattr(mod, "CODEX_SHARED_TOKEN_PATH", Path("/nonexistent/token"))
    assert _resolve_codex_token() == "codex-tok"


def test_resolve_codex_token_legacy_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    token_file = tmp_path / "codex-token"
    token_file.write_text("plain-text-token\n")
    import ax_cli.runtimes.hermes.runtimes.hermes_sdk as mod

    monkeypatch.setattr(mod, "HERMES_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_SHARED_TOKEN_PATH", token_file)
    assert _resolve_codex_token() == "plain-text-token"


def test_resolve_codex_token_legacy_rejects_axp(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    token_file = tmp_path / "codex-token"
    token_file.write_text("axp_u_bad_token\n")
    import ax_cli.runtimes.hermes.runtimes.hermes_sdk as mod

    monkeypatch.setattr(mod, "HERMES_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_AUTH_PATH", Path("/nonexistent/auth.json"))
    monkeypatch.setattr(mod, "CODEX_SHARED_TOKEN_PATH", token_file)
    assert _resolve_codex_token() == ""


# ---- _resolve_provider_config ----


def test_provider_config_codex_explicit():
    cfg = _resolve_provider_config("codex:gpt-5.4")
    assert cfg["provider"] == "openai-codex"
    assert cfg["api_mode"] == "codex_responses"
    assert cfg["model"] == "gpt-5.4"


def test_provider_config_anthropic():
    cfg = _resolve_provider_config("anthropic:claude-sonnet-4.6")
    assert cfg["provider"] == "anthropic"
    assert cfg["api_mode"] == "anthropic_messages"
    assert cfg["model"] == "claude-sonnet-4.6"


def test_provider_config_bedrock(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    cfg = _resolve_provider_config("bedrock:claude-sonnet-4.6")
    assert cfg["provider"] == "anthropic"
    assert "us-east-1" in cfg["base_url"]
    assert cfg["_bedrock"] is True


def test_provider_config_openrouter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    cfg = _resolve_provider_config("openrouter:anthropic/claude-sonnet-4.6")
    assert cfg["provider"] == "openrouter"
    assert cfg["api_mode"] == "chat_completions"
    assert "openrouter" in cfg["base_url"]


def test_provider_config_auto_detect_gpt():
    cfg = _resolve_provider_config("gpt-5.4")
    assert cfg["provider"] == "openai-codex"


def test_provider_config_auto_detect_claude():
    cfg = _resolve_provider_config("claude-sonnet-4.6")
    assert cfg["provider"] == "anthropic"


def test_provider_config_default_none():
    cfg = _resolve_provider_config(None)
    assert cfg["model"] == "gpt-5.4"


def test_provider_config_unknown_model():
    cfg = _resolve_provider_config("llama-3")
    assert cfg["provider"] == "openai-codex"
