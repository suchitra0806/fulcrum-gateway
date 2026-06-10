"""Regression tests for sentinel connector tool functions in
ax_cli/runtimes/hermes/tools/__init__.py.

Key regression: the sentinel sets AX_CONFIG_DIR to the agent workdir's .ax/
directory, which caused connector registry lookups to resolve against the
agent workdir instead of ~/.ax/gateway/. _gateway_config_ctx() fixes this
by temporarily restoring AX_CONFIG_DIR to the global config root before any
connector operation.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from ax_cli.runtimes.hermes.tools import (
    _gateway_config_ctx,
    execute_tool,
)

# ── _gateway_config_ctx ───────────────────────────────────────────────────


class TestGatewayConfigCtx:
    def test_restores_global_ax_dir_when_ax_config_dir_overridden(self, tmp_path, monkeypatch):
        """AX_CONFIG_DIR pointing at agent workdir is temporarily replaced with ~/.ax."""
        agent_workdir_ax = str(tmp_path / ".ax")
        monkeypatch.setenv("AX_CONFIG_DIR", agent_workdir_ax)
        monkeypatch.delenv("AX_GATEWAY_DIR", raising=False)

        captured = {}
        with _gateway_config_ctx():
            captured["inside"] = os.environ.get("AX_CONFIG_DIR")
        captured["after"] = os.environ.get("AX_CONFIG_DIR")

        expected_global = str(os.path.expanduser("~/.ax"))
        assert captured["inside"] == expected_global
        assert captured["after"] == agent_workdir_ax  # restored

    def test_restores_original_when_no_ax_config_dir(self, monkeypatch):
        """When AX_CONFIG_DIR is unset, it should be unset again after the ctx exits."""
        monkeypatch.delenv("AX_CONFIG_DIR", raising=False)
        monkeypatch.delenv("AX_GATEWAY_DIR", raising=False)

        with _gateway_config_ctx():
            assert os.environ.get("AX_CONFIG_DIR") is not None  # set inside

        assert os.environ.get("AX_CONFIG_DIR") is None  # removed after

    def test_uses_ax_gateway_dir_parent_when_set(self, tmp_path, monkeypatch):
        """When AX_GATEWAY_DIR is set, derive global ax dir from its parent."""
        gw_dir = tmp_path / "gateway"
        gw_dir.mkdir()
        monkeypatch.setenv("AX_GATEWAY_DIR", str(gw_dir))
        monkeypatch.delenv("AX_CONFIG_DIR", raising=False)

        with _gateway_config_ctx():
            inside = os.environ.get("AX_CONFIG_DIR")

        assert inside == str(tmp_path)  # parent of gateway dir

    def test_restores_even_on_exception(self, monkeypatch):
        """AX_CONFIG_DIR is restored even if an exception is raised inside the ctx."""
        original = "/original/.ax"
        monkeypatch.setenv("AX_CONFIG_DIR", original)
        monkeypatch.delenv("AX_GATEWAY_DIR", raising=False)

        with pytest.raises(ValueError):
            with _gateway_config_ctx():
                raise ValueError("boom")

        assert os.environ.get("AX_CONFIG_DIR") == original


# ── connector_apps uses _gateway_config_ctx ───────────────────────────────


class TestConnectorAppsConfigCtx:
    def test_connector_apps_restores_ax_config_dir(self, tmp_path, monkeypatch):
        """connector_apps must restore AX_CONFIG_DIR after execution."""
        agent_ax = str(tmp_path / ".ax")
        monkeypatch.setenv("AX_CONFIG_DIR", agent_ax)
        monkeypatch.delenv("AX_GATEWAY_DIR", raising=False)

        mock_row = MagicMock()
        mock_row.id = "abc"
        mock_row.name = "composio-main"
        mock_row.auth_ref = None

        with (
            patch("ax_cli.connectors.find_connector", return_value=mock_row) as mock_find,
            patch("ax_cli.connectors.list_apps", return_value=[]),
        ):
            execute_tool("connector_apps", {"connector": "composio-main"}, str(tmp_path))
            mock_find.assert_called_once()

        assert os.environ.get("AX_CONFIG_DIR") == agent_ax

    def test_connector_apps_not_found_with_workdir_ax_config(self, tmp_path, monkeypatch):
        """Regression: connector_apps returned 'Connector not found' when AX_CONFIG_DIR
        pointed at the agent workdir instead of the gateway's global config root."""
        agent_ax = str(tmp_path / ".ax")
        monkeypatch.setenv("AX_CONFIG_DIR", agent_ax)
        monkeypatch.delenv("AX_GATEWAY_DIR", raising=False)

        from ax_cli.connectors.storage import ConnectorNotFoundError

        with patch("ax_cli.connectors.find_connector", side_effect=ConnectorNotFoundError("composio-main")):
            result = execute_tool("connector_apps", {"connector": "composio-main"}, str(tmp_path))

        assert result.is_error
        assert "Connector not found" in result.output


# ── connector_call uses _gateway_config_ctx ───────────────────────────────


class TestConnectorCallConfigCtx:
    def test_connector_call_restores_ax_config_dir(self, tmp_path, monkeypatch):
        """connector_call must restore AX_CONFIG_DIR after execution."""
        agent_ax = str(tmp_path / ".ax")
        monkeypatch.setenv("AX_CONFIG_DIR", agent_ax)
        monkeypatch.delenv("AX_GATEWAY_DIR", raising=False)

        mock_row = MagicMock()
        mock_row.id = "abc"
        mock_row.name = "composio-main"
        mock_row.enabled = True
        mock_row.auth_ref = None

        with (
            patch("ax_cli.connectors.find_connector", return_value=mock_row),
            patch("ax_cli.connectors.execute_tool", return_value={"ok": True}),
        ):
            execute_tool(
                "connector_call",
                {"connector": "composio-main", "tool": "SLACK_CHAT_POST_MESSAGE", "args": {}},
                str(tmp_path),
            )

        assert os.environ.get("AX_CONFIG_DIR") == agent_ax
