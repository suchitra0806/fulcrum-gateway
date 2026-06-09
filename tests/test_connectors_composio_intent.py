"""Tests for Composio COMPOSIO_SEARCH_TOOLS intent search."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ax_cli.connectors.errors import ConnectorProviderError
from ax_cli.connectors.providers.composio_intent import (
    _extract_session_id,
    _parse_search_tools_response,
    search_tools_intent,
)


@pytest.fixture()
def auth_env() -> dict[str, str]:
    return {"COMPOSIO_API_KEY": "ak_test_key"}


@pytest.fixture()
def config() -> dict[str, Any]:
    return {"entity_id": "default"}


class TestParseSearchToolsResponse:
    def test_extracts_slugs_and_session(self):
        data = {
            "session": {"id": "sess-abc"},
            "results": [
                {"primary_tool_slugs": ["GITHUB_LIST_PRS", "GITHUB_GET_REPO"]},
            ],
        }
        items, session_id = _parse_search_tools_response(data)
        assert session_id == "sess-abc"
        assert [item["name"] for item in items] == ["GITHUB_GET_REPO", "GITHUB_LIST_PRS"]

    def test_ignores_meta_tool_slug(self):
        data = {"tool_slug": "COMPOSIO_SEARCH_TOOLS"}
        items, _ = _parse_search_tools_response(data)
        assert items == []

    def test_ignores_status_and_error_strings(self):
        data = {
            "results": [
                {
                    "primary_tool_slugs": ["GITHUB_LIST_PRS"],
                    "connection_status": "NOT_CONNECTED",
                    "auth_state": "AUTHENTICATION_REQUIRED",
                    "diagnostics": ["RATE_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR"],
                }
            ],
            "tool_schemas": {"GITHUB_LIST_PRS": {"parameters": {}}},
        }
        items, _ = _parse_search_tools_response(data)
        assert [item["name"] for item in items] == ["GITHUB_LIST_PRS"]

    def test_collects_tool_schema_keys(self):
        data = {
            "tool_schemas": {
                "GMAIL_SEND_EMAIL": {},
                "SLACK_POST_MESSAGE": {},
            }
        }
        items, _ = _parse_search_tools_response(data)
        assert [item["name"] for item in items] == ["GMAIL_SEND_EMAIL", "SLACK_POST_MESSAGE"]


class TestExtractSessionId:
    def test_session_dict(self):
        assert _extract_session_id({"session": {"id": "s1"}}) == "s1"

    def test_top_level_session_id(self):
        assert _extract_session_id({"session_id": "s2"}) == "s2"


class TestSearchToolsIntent:
    def test_executes_meta_tool_with_generated_session(self, auth_env: dict, config: dict):
        captured: dict[str, Any] = {}

        def _fake_execute(tool_slug, args, auth, cfg, name):
            captured["tool_slug"] = tool_slug
            captured["args"] = args
            return {
                "successful": True,
                "data": {
                    "session": {"id": "sess-new"},
                    "results": [{"primary_tool_slugs": ["GITHUB_LIST_PRS"]}],
                },
            }

        with patch(
            "ax_cli.connectors.providers.composio_adapter.execute_tool",
            side_effect=_fake_execute,
        ):
            result = search_tools_intent(
                "list github pull requests",
                auth_env,
                config,
                "demo",
                limit=5,
            )

        assert captured["tool_slug"] == "COMPOSIO_SEARCH_TOOLS"
        assert captured["args"]["queries"] == [{"use_case": "list github pull requests"}]
        assert captured["args"]["session"] == {"generate_id": True}
        assert result["mode"] == "intent"
        assert result["session_id"] == "sess-new"
        assert result["items"][0]["name"] == "GITHUB_LIST_PRS"

    def test_reuses_session_id_when_provided(self, auth_env: dict, config: dict):
        captured: dict[str, Any] = {}

        def _fake_execute(tool_slug, args, auth, cfg, name):
            captured["args"] = args
            return {
                "successful": True,
                "data": {"results": [{"primary_tool_slugs": ["GITHUB_LIST_PRS"]}]},
            }

        with patch(
            "ax_cli.connectors.providers.composio_adapter.execute_tool",
            side_effect=_fake_execute,
        ):
            search_tools_intent(
                "list prs",
                auth_env,
                config,
                "demo",
                session_id="sess-existing",
            )

        assert captured["args"]["session"] == {"id": "sess-existing"}

    def test_filters_by_app_prefix(self, auth_env: dict, config: dict):
        def _fake_execute(tool_slug, args, auth, cfg, name):
            return {
                "successful": True,
                "data": {
                    "results": [{"primary_tool_slugs": ["GITHUB_LIST_PRS", "GMAIL_SEND_EMAIL"]}],
                },
            }

        with patch(
            "ax_cli.connectors.providers.composio_adapter.execute_tool",
            side_effect=_fake_execute,
        ):
            result = search_tools_intent(
                "send message",
                auth_env,
                config,
                "demo",
                apps="gmail",
            )

        assert [item["name"] for item in result["items"]] == ["GMAIL_SEND_EMAIL"]

    def test_raises_on_empty_query(self, auth_env: dict, config: dict):
        with pytest.raises(ConnectorProviderError, match="query is required"):
            search_tools_intent("", auth_env, config, "demo")

    def test_raises_on_unsuccessful_response(self, auth_env: dict, config: dict):
        def _fake_execute(tool_slug, args, auth, cfg, name):
            return {"successful": False, "error": "rate limited"}

        with patch(
            "ax_cli.connectors.providers.composio_adapter.execute_tool",
            side_effect=_fake_execute,
        ):
            with pytest.raises(ConnectorProviderError, match="rate limited"):
                search_tools_intent("list prs", auth_env, config, "demo")
