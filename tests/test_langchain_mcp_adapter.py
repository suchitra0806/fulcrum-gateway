"""Tests for load_mcps_from_env() in ax_cli/runtimes/mcp_servers/langchain_adapter.py.

Covers the three-element return value (tools, clients, warnings) introduced
in issue #239: the `warnings` list surfaces parse/init errors that callers
(e.g. _load_mcp_tools in the LangGraph bridge) can forward as activity events
instead of silently swallowing them.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("langchain_core")

from ax_cli.runtimes.mcp_servers.langchain_adapter import load_mcps_from_env  # noqa: E402

# ── Empty / unset env var ─────────────────────────────────────────────────────


def test_returns_empty_triple_when_env_unset(monkeypatch):
    monkeypatch.delenv("AX_BRIDGE_MCP_SERVERS", raising=False)
    tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == [] and warnings == []


def test_returns_empty_triple_when_env_whitespace_only(monkeypatch):
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", "   ")
    tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == [] and warnings == []


# ── Parse failures ────────────────────────────────────────────────────────────


def test_returns_warning_on_invalid_json(monkeypatch):
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", "not-valid{{{")
    tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == []
    assert len(warnings) == 1
    assert "failed to parse" in warnings[0]
    assert "AX_BRIDGE_MCP_SERVERS" in warnings[0]


def test_returns_warning_on_missing_command(monkeypatch):
    cfg = json.dumps({"my_server": {"env": {}}})  # no "command" key
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", cfg)
    tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == []
    assert len(warnings) == 1
    assert "missing or invalid" in warnings[0]
    assert "my_server" in warnings[0]


def test_returns_warning_when_command_is_empty_list(monkeypatch):
    cfg = json.dumps({"my_server": {"command": []}})
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", cfg)
    tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == []
    assert len(warnings) == 1
    assert "my_server" in warnings[0]


# ── Server-level init failures ────────────────────────────────────────────────


def test_returns_warning_on_server_start_failure(monkeypatch):
    cfg = json.dumps({"srv": {"command": ["python", "-m", "fake_server"]}})
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", cfg)
    with patch("ax_cli.runtimes.mcp_servers.langchain_adapter.McpStdioClient") as MockClient:
        instance = MockClient.return_value
        instance.start.side_effect = RuntimeError("binary not found")
        instance.close = MagicMock()
        tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == []
    assert len(warnings) == 1
    assert "failed to initialize" in warnings[0]
    assert "srv" in warnings[0]


def test_returns_warning_when_server_exposes_no_tools(monkeypatch):
    cfg = json.dumps({"srv": {"command": ["python", "-m", "fake_server"]}})
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", cfg)
    with patch("ax_cli.runtimes.mcp_servers.langchain_adapter.McpStdioClient") as MockClient:
        instance = MockClient.return_value
        instance.start = MagicMock()
        instance.initialize = MagicMock()
        instance.list_tools.return_value = []
        instance.close = MagicMock()
        tools, clients, warnings = load_mcps_from_env()
    assert tools == [] and clients == []
    assert len(warnings) == 1
    assert "no tools" in warnings[0]
    assert "srv" in warnings[0]


# ── Multiple servers: partial failure ────────────────────────────────────────


def test_multiple_servers_partial_failure_accumulates_warnings(monkeypatch):
    """A failing server adds a warning; a succeeding server still contributes tools."""
    cfg = json.dumps({
        "bad": {"command": []},
        "good": {"command": ["python", "-m", "fake_ok"]},
    })
    monkeypatch.setenv("AX_BRIDGE_MCP_SERVERS", cfg)

    from ax_cli.runtimes.mcp_servers.mcp_client import McpToolSpec

    fake_spec = McpToolSpec(name="do_thing", description="a tool", input_schema={"properties": {}, "required": []})

    with patch("ax_cli.runtimes.mcp_servers.langchain_adapter.McpStdioClient") as MockClient:
        instance = MockClient.return_value
        instance.start = MagicMock()
        instance.initialize = MagicMock()
        instance.list_tools.return_value = [fake_spec]
        instance.close = MagicMock()
        tools, clients, warnings = load_mcps_from_env()

    assert len(warnings) == 1
    assert "bad" in warnings[0]
    assert len(tools) == 1
    assert len(clients) == 1
