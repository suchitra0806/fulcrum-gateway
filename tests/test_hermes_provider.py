"""Tests for _validate_hermes_provider and _resolve_hermes_model."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ax_cli.commands.gateway_runtime_cmd import _validate_hermes_provider


@pytest.fixture()
def auth_json(tmp_path):
    """Write a realistic ~/.hermes/auth.json and patch Path.home() to tmp_path."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    auth_path = hermes_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": "1.0",
                "active_provider": "anthropic",
                "providers": {
                    "anthropic": {"tokens": {"access_token": "sk-ant-test"}},
                    "openrouter": {"tokens": {"access_token": "sk-or-test"}},
                },
                "credential_pool": {
                    "anthropic": [{"auth_type": "api_key", "access_token": "sk-ant-test"}],
                    "openrouter": [{"auth_type": "api_key", "access_token": "sk-or-test"}],
                },
                "updated_at": "2026-06-01T00:00:00Z",
            }
        )
    )
    with patch.object(Path, "home", return_value=tmp_path):
        yield auth_path


def test_valid_provider_passes(auth_json):
    _validate_hermes_provider("anthropic")
    _validate_hermes_provider("openrouter")


def test_missing_provider_raises(auth_json):
    with pytest.raises(ValueError, match="not found in.*credential pool"):
        _validate_hermes_provider("bedrock")


def test_missing_auth_json_raises(tmp_path):
    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(ValueError, match="auth.json not found"):
            _validate_hermes_provider("anthropic")


def test_empty_credential_pool_raises(tmp_path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    auth_path = hermes_dir / "auth.json"
    auth_path.write_text(json.dumps({"credential_pool": {}}))
    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(ValueError, match="not found in.*credential pool"):
            _validate_hermes_provider("anthropic")


def test_empty_creds_list_raises(tmp_path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    auth_path = hermes_dir / "auth.json"
    auth_path.write_text(json.dumps({"credential_pool": {"anthropic": []}}))
    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(ValueError, match="no credential entries"):
            _validate_hermes_provider("anthropic")


def test_no_credential_pool_key_raises(tmp_path):
    """auth.json without credential_pool — provider check against top-level keys must NOT pass."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    auth_path = hermes_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": "1.0",
                "providers": {"anthropic": {"tokens": {"access_token": "sk-test"}}},
                "active_provider": "anthropic",
            }
        )
    )
    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(ValueError, match="not found in.*credential pool"):
            _validate_hermes_provider("anthropic")


def test_invalid_json_raises(tmp_path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    auth_path = hermes_dir / "auth.json"
    auth_path.write_text("not json")
    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(ValueError, match="Cannot read"):
            _validate_hermes_provider("anthropic")


def test_non_object_json_raises(tmp_path):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    auth_path = hermes_dir / "auth.json"
    auth_path.write_text(json.dumps([1, 2, 3]))
    with patch.object(Path, "home", return_value=tmp_path):
        with pytest.raises(ValueError, match="not a JSON object"):
            _validate_hermes_provider("anthropic")
