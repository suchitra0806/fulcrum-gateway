"""Tests for connector dispatch — list_tools total/matched/filtered/clipped semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ax_cli.connectors.providers import dispatch
from ax_cli.connectors.types import ConnectorRow


def _row(config=None) -> ConnectorRow:
    return ConnectorRow(id="00000000-0000-0000-0000-000000000000", name="test", provider="fake", config=config or {})


@pytest.fixture
def fake_list_adapter(monkeypatch):
    """Install a fake adapter exposing list_tools that returns N github tools."""

    def _install(n):
        tools = [{"name": f"GITHUB_T_{i:03d}", "appName": "github"} for i in range(n)]
        adapter = SimpleNamespace(list_tools=lambda auth_env, config, name: {"tools": tools})
        monkeypatch.setitem(dispatch._ADAPTERS, "fake", adapter)
        return tools

    return _install


class TestListToolsSemantics:
    def test_reports_matched_when_clipped(self, fake_list_adapter):
        fake_list_adapter(100)
        result = dispatch.list_tools(_row({"tools_limit": 50}), {})
        assert result["total"] == 100
        assert result["matched"] == 100  # all matched policy (no allow/deny)
        assert result["filtered"] == 50  # but only 50 fit under the limit
        assert result["limit"] == 50
        assert result["clipped"] is True
        assert len(result["items"]) == 50

    def test_not_clipped_when_under_limit(self, fake_list_adapter):
        fake_list_adapter(10)
        result = dispatch.list_tools(_row({"tools_limit": 50}), {})
        assert result["matched"] == 10
        assert result["filtered"] == 10
        assert result["clipped"] is False

    def test_matched_reflects_policy_not_raw_total(self, fake_list_adapter):
        fake_list_adapter(100)
        # Deny GITHUB_T_00x (10 tools: _000.._009) via name pattern
        result = dispatch.list_tools(
            _row({"tools_limit": 200, "denied_tools": ["GITHUB_T_00*"]}),
            {},
        )
        assert result["total"] == 100  # raw adapter response
        assert result["matched"] == 90  # post-policy: 10 denied (_000.._009)
        assert result["clipped"] is False

    def test_clip_is_deterministic_sorted_by_name(self, fake_list_adapter):
        # Adapter returns tools in reverse order; the clip must keep the
        # alphabetically-first ones, not whatever catalog order arrived.
        tools = [{"name": f"GITHUB_T_{i:03d}", "appName": "github"} for i in reversed(range(100))]
        adapter = SimpleNamespace(list_tools=lambda auth_env, config, name: {"tools": tools})
        dispatch._ADAPTERS["fake"] = adapter
        try:
            result = dispatch.list_tools(_row({"tools_limit": 3}), {})
        finally:
            del dispatch._ADAPTERS["fake"]
        assert [t["name"] for t in result["items"]] == ["GITHUB_T_000", "GITHUB_T_001", "GITHUB_T_002"]
