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

        def _intent(query, auth_env, config, name, *, apps=None, limit=10, session_id=None, known_fields=None):
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
