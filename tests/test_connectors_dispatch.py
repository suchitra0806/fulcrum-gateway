"""Tests for the connector dispatch layer.

Covers list_tools total/matched/filtered/clipped semantics (#95),
execute_tool toolkit derivation / policy enforcement integration (#128),
intent search routing, http_mcp local search fallback, and catalog pagination.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ax_cli.connectors.errors import ConnectorPolicyError
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


@pytest.fixture
def fake_catalog_adapter(monkeypatch):
    """Install a fake adapter that only exposes paginated search_tools."""

    def _install(pages):
        calls: list[str | None] = []

        def search_tools(
            query,
            auth_env,
            config,
            name,
            *,
            limit=10,
            cursor=None,
            apps=None,
        ):
            calls.append(cursor)
            return pages[len(calls) - 1]

        adapter = SimpleNamespace(search_tools=search_tools)
        monkeypatch.setitem(dispatch._ADAPTERS, "fake", adapter)
        return calls

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


class TestSearchToolsIntent:
    def test_uses_intent_adapter_for_auto_mode(self, monkeypatch):
        calls: dict[str, str] = {}

        monkeypatch.setattr(
            "ax_cli.connectors.providers.dispatch.has_capability",
            lambda provider, capability: capability == "intent_search",
        )

        def _intent(query, auth_env, config, name, *, apps=None, limit=10, session_id=None):
            calls["query"] = query
            calls["limit"] = str(limit)
            return {
                "items": [{"name": "GITHUB_LIST_PRS", "displayName": "List PRs"}],
                "mode": "intent",
                "session_id": "sess-1",
            }

        adapter = SimpleNamespace(
            search_tools_intent=_intent,
            search_tools=lambda *a, **k: {"items": []},
        )
        monkeypatch.setitem(dispatch._ADAPTERS, "fake", adapter)
        result = dispatch.search_tools(_row(), "list prs", {}, limit=3, mode="auto")
        assert calls["query"] == "list prs"
        assert result["mode"] == "intent"
        assert result["session_id"] == "sess-1"
        assert result["items"][0]["name"] == "GITHUB_LIST_PRS"

    def test_catalog_mode_uses_get_search(self, monkeypatch):
        calls: dict[str, str] = {}

        def _catalog(query, auth_env, config, name, *, apps=None, limit=10, cursor=None):
            calls["query"] = query
            return {"items": [{"name": "GITHUB_LIST_PRS", "displayName": "List PRs"}]}

        adapter = SimpleNamespace(
            search_tools=_catalog,
            search_tools_intent=lambda *a, **k: {"items": []},
        )
        monkeypatch.setitem(dispatch._ADAPTERS, "fake", adapter)
        monkeypatch.setattr(
            "ax_cli.connectors.providers.dispatch.has_capability",
            lambda provider, capability: capability == "intent_search",
        )
        result = dispatch.search_tools(_row(), "list prs", {}, mode="catalog")
        assert calls["query"] == "list prs"
        assert result["mode"] == "catalog"
        assert "session_id" not in result


class TestSearchToolsLocalFallback:
    def test_provider_without_catalog_search_filters_list_tools(self, monkeypatch):
        tools = [
            {"name": "get_weather", "displayName": "Get Weather", "description": "weather data"},
            {"name": "send_email", "displayName": "Send Email", "description": "send mail"},
        ]
        adapter = SimpleNamespace(list_tools=lambda auth_env, config, name: {"tools": tools})
        monkeypatch.setitem(dispatch._ADAPTERS, "http_mcp", adapter)
        monkeypatch.setattr(
            "ax_cli.connectors.providers.dispatch.has_capability",
            lambda provider, capability: False,
        )
        row = ConnectorRow(
            id="00000000-0000-0000-0000-000000000001",
            name="mcp-local",
            provider="http_mcp",
            config={},
        )
        result = dispatch.search_tools(row, "weather", {}, mode="auto")
        assert result["mode"] == "catalog"
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "get_weather"


class TestCatalogPagination:
    def test_drains_all_catalog_pages(self, fake_catalog_adapter):
        pages = [
            {
                "items": [{"name": "TOOL_A", "appName": "github"}],
                "next_cursor": "page-2",
                "total_items": 2,
            },
            {
                "items": [{"name": "TOOL_B", "appName": "github"}],
                "next_cursor": None,
                "total_items": 2,
            },
        ]
        calls = fake_catalog_adapter(pages)
        result = dispatch.list_tools(_row({"tools_limit": 50}), {})
        assert calls == [None, "page-2"]
        assert result["total"] == 2
        assert result["matched"] == 2
        assert [t["name"] for t in result["items"]] == ["TOOL_A", "TOOL_B"]

    def test_total_uses_provider_inventory_when_reported(self, fake_catalog_adapter):
        pages = [
            {
                "items": [{"name": f"TOOL_{i:03d}", "appName": "github"} for i in range(200)],
                "next_cursor": "page-2",
                "total_items": 450,
            },
            {
                "items": [{"name": f"TOOL_{i:03d}", "appName": "github"} for i in range(200, 400)],
                "next_cursor": "page-3",
                "total_items": 450,
            },
            {
                "items": [{"name": f"TOOL_{i:03d}", "appName": "github"} for i in range(400, 450)],
                "next_cursor": None,
                "total_items": 450,
            },
        ]
        fake_catalog_adapter(pages)
        result = dispatch.list_tools(_row({"tools_limit": 200}), {})
        assert result["total"] == 450
        assert result["matched"] == 450
        assert result["filtered"] == 200
        assert result["clipped"] is True

    def test_mid_drain_failure_returns_partial_catalog(self, monkeypatch, caplog):
        calls: list[str | None] = []

        def search_tools(
            query,
            auth_env,
            config,
            name,
            *,
            limit=10,
            cursor=None,
            apps=None,
        ):
            calls.append(cursor)
            if cursor is None:
                return {
                    "items": [{"name": "TOOL_A", "appName": "github"}],
                    "next_cursor": "page-2",
                    "total_items": 5000,
                }
            raise RuntimeError("provider timeout")

        adapter = SimpleNamespace(search_tools=search_tools)
        monkeypatch.setitem(dispatch._ADAPTERS, "fake", adapter)
        with caplog.at_level(logging.WARNING, logger="connectors.dispatch"):
            result = dispatch.list_tools(_row({"tools_limit": 50}), {})

        assert calls == [None, "page-2"]
        assert result["catalog_partial"] is True
        assert result["catalog_drained"] == 1
        assert result["catalog_drain_error"] == "provider timeout"
        assert result["total"] == 1
        assert result["matched"] == 1
        assert [t["name"] for t in result["items"]] == ["TOOL_A"]
        assert "mid-pagination" in caplog.text
        assert "provider timeout" in caplog.text

    def test_first_page_drain_failure_still_raises(self, monkeypatch):
        def search_tools(*args, **kwargs):
            raise RuntimeError("provider unavailable")

        adapter = SimpleNamespace(search_tools=search_tools)
        monkeypatch.setitem(dispatch._ADAPTERS, "fake", adapter)
        with pytest.raises(RuntimeError, match="provider unavailable"):
            dispatch.list_tools(_row({"tools_limit": 50}), {})


@pytest.fixture()
def composio_row_with_github_allowlist() -> ConnectorRow:
    return ConnectorRow(
        id="connector-1",
        name="composio-gh",
        provider="composio",
        enabled=True,
        config={"allowed_toolkits": ["github"]},
    )


@pytest.fixture()
def http_mcp_row() -> ConnectorRow:
    # http_mcp has no toolkit_from_slug helper — verifies the dispatch
    # layer doesn't blow up on providers that don't ship one.
    return ConnectorRow(
        id="connector-2",
        name="local-mcp",
        provider="http_mcp",
        enabled=True,
        config={"base_url": "http://localhost:8080"},
    )


def _stub_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True}


class TestExecuteToolToolkitDerivation:
    def test_caller_omits_toolkit_for_composio_slug_derives_and_passes_policy(
        self, composio_row_with_github_allowlist: ConnectorRow
    ):
        # Reproduces #128: Hermes _connector_call passes toolkit=None.
        # Before the fix this raised ConnectorPolicyError because
        # _toolkit_allowed(None, policy) is False when an allow-list is set.
        with patch("ax_cli.connectors.providers.composio_adapter.execute_tool", side_effect=_stub_execute):
            result = dispatch.execute_tool(
                composio_row_with_github_allowlist,
                "GITHUB_LIST_PULL_REQUESTS",
                {},
                auth_env={"COMPOSIO_API_KEY": "ak_test"},
            )
            assert result == {"ok": True}

    def test_caller_omits_toolkit_for_non_matching_slug_still_rejects(
        self, composio_row_with_github_allowlist: ConnectorRow
    ):
        # The fix must not become an allow-all — a slug whose derived
        # toolkit doesn't match the allow-list must still be rejected.
        with patch("ax_cli.connectors.providers.composio_adapter.execute_tool", side_effect=_stub_execute) as mock_exec:
            with pytest.raises(ConnectorPolicyError):
                dispatch.execute_tool(
                    composio_row_with_github_allowlist,
                    "SLACK_SEND_MESSAGE",
                    {},
                    auth_env={"COMPOSIO_API_KEY": "ak_test"},
                )
            mock_exec.assert_not_called()

    def test_explicit_toolkit_overrides_derivation(self, composio_row_with_github_allowlist: ConnectorRow):
        # When the caller knows the toolkit (e.g. from a list_tools
        # context), the explicit value wins. Passing toolkit="slack" with
        # a github allow-list must be rejected even though the slug looks
        # like a github tool.
        with patch("ax_cli.connectors.providers.composio_adapter.execute_tool", side_effect=_stub_execute):
            with pytest.raises(ConnectorPolicyError):
                dispatch.execute_tool(
                    composio_row_with_github_allowlist,
                    "GITHUB_LIST_PULL_REQUESTS",
                    {},
                    auth_env={"COMPOSIO_API_KEY": "ak_test"},
                    toolkit="slack",
                )

    def test_provider_without_derive_helper_runs_normally(self, http_mcp_row: ConnectorRow):
        # http_mcp ships no toolkit_from_slug — getattr falls through to
        # None and we proceed with toolkit=None. No allow-list is set on
        # this row, so the call should succeed.
        with patch("ax_cli.connectors.providers.http_mcp_adapter.execute_tool", side_effect=_stub_execute) as mock_exec:
            result = dispatch.execute_tool(
                http_mcp_row,
                "get_weather",
                {"city": "SF"},
                auth_env={"HTTP_MCP_API_KEY": "raw"},
            )
            assert result == {"ok": True}
            mock_exec.assert_called_once()

