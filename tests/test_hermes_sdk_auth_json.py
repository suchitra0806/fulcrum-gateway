"""Tests for auth.json credential resolution in hermes_sdk runtime.

Verifies that _resolve_credential_from_auth_json reads per-agent and global
auth.json files, and that _resolve_provider_config falls back to auth.json
when env vars are not set.
"""

from __future__ import annotations

import json

import pytest

from ax_cli.runtimes.hermes.runtimes import hermes_sdk


@pytest.fixture
def auth_json_factory(tmp_path):
    """Create an auth.json file with given credential_pool entries."""

    def _make(pool: dict, *, subdir: str = "") -> str:
        target = tmp_path / subdir if subdir else tmp_path
        target.mkdir(parents=True, exist_ok=True)
        path = target / "auth.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {},
                    "credential_pool": pool,
                }
            )
        )
        return str(target)

    return _make


class TestResolveCredentialFromAuthJson:
    def test_reads_from_hermes_home(self, auth_json_factory, monkeypatch):
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "test1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-test-key",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        result = hermes_sdk._resolve_credential_from_auth_json("anthropic")
        assert result["api_key"] == "sk-ant-test-key"

    def test_reads_base_url(self, auth_json_factory, monkeypatch):
        home = auth_json_factory(
            {
                "openrouter": [
                    {
                        "id": "test2",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "or-test-key",
                        "base_url": "https://custom.endpoint/v1",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        result = hermes_sdk._resolve_credential_from_auth_json("openrouter")
        assert result["api_key"] == "or-test-key"
        assert result["base_url"] == "https://custom.endpoint/v1"

    def test_falls_back_to_global_hermes_auth(self, auth_json_factory, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        global_auth = tmp_path / "global-hermes"
        global_auth.mkdir()
        auth_path = global_auth / "auth.json"
        auth_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {},
                    "credential_pool": {
                        "anthropic": [
                            {
                                "id": "g1",
                                "auth_type": "api_key",
                                "priority": 0,
                                "access_token": "sk-ant-global-key",
                            }
                        ]
                    },
                }
            )
        )
        monkeypatch.setattr(hermes_sdk, "HERMES_AUTH_PATH", auth_path)
        result = hermes_sdk._resolve_credential_from_auth_json("anthropic")
        assert result["api_key"] == "sk-ant-global-key"

    def test_hermes_home_takes_precedence_over_global(self, auth_json_factory, monkeypatch, tmp_path):
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "local",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-local",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        global_auth = tmp_path / "global"
        global_auth.mkdir()
        (global_auth / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {},
                    "credential_pool": {
                        "anthropic": [
                            {
                                "id": "global",
                                "auth_type": "api_key",
                                "priority": 0,
                                "access_token": "sk-ant-global",
                            }
                        ]
                    },
                }
            )
        )
        monkeypatch.setattr(hermes_sdk, "HERMES_AUTH_PATH", global_auth / "auth.json")
        result = hermes_sdk._resolve_credential_from_auth_json("anthropic")
        assert result["api_key"] == "sk-ant-local"

    def test_returns_empty_when_provider_missing(self, auth_json_factory, monkeypatch, tmp_path):
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "a1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-test",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        monkeypatch.setattr(hermes_sdk, "HERMES_AUTH_PATH", tmp_path / "nonexistent" / "auth.json")
        result = hermes_sdk._resolve_credential_from_auth_json("openrouter")
        assert result["api_key"] == ""
        assert result["base_url"] == ""

    def test_returns_empty_when_no_files_exist(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nonexistent"))
        monkeypatch.setattr(hermes_sdk, "HERMES_AUTH_PATH", tmp_path / "also-nonexistent" / "auth.json")
        result = hermes_sdk._resolve_credential_from_auth_json("anthropic")
        assert result["api_key"] == ""
        assert result["base_url"] == ""

    def test_picks_lowest_priority_entry(self, auth_json_factory, monkeypatch):
        home = auth_json_factory(
            {
                "anthropic": [
                    {"id": "high", "auth_type": "api_key", "priority": 10, "access_token": "sk-high"},
                    {"id": "low", "auth_type": "api_key", "priority": 0, "access_token": "sk-low"},
                    {"id": "mid", "auth_type": "api_key", "priority": 5, "access_token": "sk-mid"},
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        result = hermes_sdk._resolve_credential_from_auth_json("anthropic")
        assert result["api_key"] == "sk-low"


class TestResolveProviderConfigAuthJsonFallback:
    def test_anthropic_falls_back_to_auth_json(self, auth_json_factory, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "a1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-from-auth-json",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("anthropic:claude-haiku-4-5-20251001")
        assert cfg["api_key"] == "sk-ant-from-auth-json"
        assert cfg["base_url"] == "https://api.anthropic.com"
        assert cfg["provider"] == "anthropic"
        assert cfg["model"] == "claude-haiku-4-5-20251001"

    def test_anthropic_env_var_takes_precedence(self, auth_json_factory, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "a1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-from-auth-json",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("anthropic:claude-sonnet-4-6")
        assert cfg["api_key"] == "sk-ant-from-env"

    def test_anthropic_auth_json_base_url(self, auth_json_factory, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "a1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-test",
                        "base_url": "https://custom-anthropic.example.com",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("anthropic:claude-haiku-4-5-20251001")
        assert cfg["base_url"] == "https://custom-anthropic.example.com"

    def test_openrouter_falls_back_to_auth_json(self, auth_json_factory, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
        home = auth_json_factory(
            {
                "openrouter": [
                    {
                        "id": "o1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "or-from-auth-json",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("openrouter:gemini-2.5-flash")
        assert cfg["api_key"] == "or-from-auth-json"
        assert cfg["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert cfg["provider"] == "openrouter"
        assert cfg["model"] == "gemini-2.5-flash"

    def test_openrouter_env_var_takes_precedence(self, auth_json_factory, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-from-env")
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        home = auth_json_factory(
            {
                "openrouter": [
                    {
                        "id": "o1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "or-from-auth-json",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("openrouter:gemini-2.5-flash")
        assert cfg["api_key"] == "or-from-env"
        assert cfg["base_url"] == "https://openrouter.ai/api/v1"

    def test_auto_detect_claude_falls_back(self, auth_json_factory, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        home = auth_json_factory(
            {
                "anthropic": [
                    {
                        "id": "a1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "sk-ant-autodetect",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("claude-sonnet-4-6")
        assert cfg["api_key"] == "sk-ant-autodetect"
        assert cfg["provider"] == "anthropic"

    def test_env_base_url_overrides_auth_json_base_url(self, auth_json_factory, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://env-override.example.com")
        home = auth_json_factory(
            {
                "openrouter": [
                    {
                        "id": "o1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "access_token": "or-key",
                        "base_url": "https://auth-json.example.com",
                    }
                ]
            }
        )
        monkeypatch.setenv("HERMES_HOME", home)
        cfg = hermes_sdk._resolve_provider_config("openrouter:gemini-2.5-flash")
        assert cfg["api_key"] == "or-key"
        assert cfg["base_url"] == "https://env-override.example.com"
