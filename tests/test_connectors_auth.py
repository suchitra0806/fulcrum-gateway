"""Tests for managed auth lifecycle, permissions, redaction, and cleanup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ax_cli.connectors.auth import (
    _parse_env,
    _serialize_env,
    auth_status,
    cleanup_auth,
    read_auth,
    write_auth,
)
from ax_cli.connectors.errors import ConnectorAuthError


@pytest.fixture()
def tmp_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    auth_dir = tmp_path / "connectors" / "auth"

    def _fake_auth_dir() -> Path:
        auth_dir.mkdir(parents=True, exist_ok=True)
        return auth_dir

    monkeypatch.setattr("ax_cli.connectors.paths.auth_dir", _fake_auth_dir)
    return tmp_path


# ── Serialization ─────────────────────────────────────────────────────────────


class TestEnvSerialization:
    def test_serialize_basic(self):
        text = _serialize_env({"KEY": "val", "ANOTHER": "x"})
        assert "ANOTHER=x\n" in text
        assert "KEY=val\n" in text

    def test_serialize_empty(self):
        assert _serialize_env({}) == ""

    def test_serialize_rejects_equals_in_key(self):
        with pytest.raises(ConnectorAuthError):
            _serialize_env({"K=EY": "val"})

    def test_serialize_rejects_newline_in_value(self):
        with pytest.raises(ConnectorAuthError):
            _serialize_env({"KEY": "line1\nline2"})

    def test_parse_basic(self):
        text = "KEY=value\nANOTHER=x\n"
        result = _parse_env(text)
        assert result == {"KEY": "value", "ANOTHER": "x"}

    def test_parse_skips_comments_and_blanks(self):
        text = "# comment\n\nKEY=val\n"
        result = _parse_env(text)
        assert result == {"KEY": "val"}

    def test_parse_quoted_values(self):
        text = "KEY=\"hello world\"\nSINGLE='quoted'\n"
        result = _parse_env(text)
        assert result["KEY"] == "hello world"
        assert result["SINGLE"] == "quoted"

    def test_parse_value_with_equals(self):
        text = "KEY=value=with=equals\n"
        result = _parse_env(text)
        assert result["KEY"] == "value=with=equals"

    def test_roundtrip(self):
        original = {"API_KEY": "sk_test123", "USER_ID": "user42"}
        text = _serialize_env(original)
        parsed = _parse_env(text)
        assert parsed == original


# ── Write / Read lifecycle ────────────────────────────────────────────────────


class TestAuthLifecycle:
    def test_write_and_read(self, tmp_gateway: Path):
        kvs = {"COMPOSIO_API_KEY": "ak_test", "COMPOSIO_ENTITY_ID": "default"}
        write_auth("conn-id-1", "test-conn", kvs)
        result = read_auth("conn-id-1", "test-conn")
        assert result["COMPOSIO_API_KEY"] == "ak_test"
        assert result["COMPOSIO_ENTITY_ID"] == "default"

    def test_write_creates_auth_dir(self, tmp_gateway: Path):
        write_auth("conn-id-2", "test", {"KEY": "val"})
        auth_dir = tmp_gateway / "connectors" / "auth"
        assert auth_dir.is_dir()

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions")
    def test_write_sets_permissions_0600(self, tmp_gateway: Path):
        path = write_auth("conn-id-3", "test", {"KEY": "val"})
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_write_merges_existing(self, tmp_gateway: Path):
        write_auth("conn-id-4", "test", {"OLD": "val1"})
        write_auth("conn-id-4", "test", {"NEW": "val2"})
        result = read_auth("conn-id-4", "test")
        assert result["OLD"] == "val1"
        assert result["NEW"] == "val2"

    def test_write_overwrites_same_key(self, tmp_gateway: Path):
        write_auth("conn-id-4b", "test", {"KEY": "original"})
        write_auth("conn-id-4b", "test", {"KEY": "updated"})
        result = read_auth("conn-id-4b", "test")
        assert result["KEY"] == "updated"

    def test_write_empty_raises(self, tmp_gateway: Path):
        with pytest.raises(ConnectorAuthError, match="No key-value pairs"):
            write_auth("conn-id-5", "test", {})

    def test_read_missing_raises(self, tmp_gateway: Path):
        with pytest.raises(ConnectorAuthError, match="No auth file found"):
            read_auth("nonexistent", "test")


# ── Auth status (redacted) ────────────────────────────────────────────────────


class TestAuthStatus:
    def test_status_existing(self, tmp_gateway: Path):
        write_auth("conn-stat-1", "test", {"KEY_A": "secret", "KEY_B": "secret"})
        status = auth_status("conn-stat-1", "test")
        assert status["exists"] is True
        assert sorted(status["keys"]) == ["KEY_A", "KEY_B"]
        assert "secret" not in str(status)

    def test_status_missing(self, tmp_gateway: Path):
        status = auth_status("nonexistent", "test")
        assert status["exists"] is False
        assert status["keys"] == []


# ── Cleanup ───────────────────────────────────────────────────────────────────


class TestAuthCleanup:
    def test_cleanup_existing(self, tmp_gateway: Path):
        write_auth("cleanup-1", "test", {"KEY": "val"})
        assert cleanup_auth("cleanup-1") is True
        path = tmp_gateway / "connectors" / "auth" / "cleanup-1.env"
        assert not path.exists()

    def test_cleanup_missing(self, tmp_gateway: Path):
        assert cleanup_auth("nonexistent") is False
