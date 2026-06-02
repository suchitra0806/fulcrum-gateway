"""Tests for tool policy evaluation — fnmatch filtering and assert_tool_allowed."""

from __future__ import annotations

import pytest

from ax_cli.connectors.errors import ConnectorPolicyError
from ax_cli.connectors.filtering import (
    ToolFilterPolicy,
    assert_tool_allowed,
    filter_tools,
    from_config,
    validate_fnmatch_pattern,
    validate_policy_patterns,
)

# ── from_config ──────────────────────────────────────────────────────────────


class TestFromConfig:
    def test_empty_config(self):
        policy = from_config({})
        assert policy.allowed_tools == []
        assert policy.denied_tools == []
        assert policy.allowed_toolkits == []
        assert policy.denied_toolkits == []
        assert policy.tools_limit == 50

    def test_all_fields(self):
        policy = from_config(
            {
                "allowed_tools": ["GITHUB_*"],
                "denied_tools": ["*_DELETE_*"],
                "allowed_toolkits": ["github"],
                "denied_toolkits": ["slack"],
                "tools_limit": 100,
            }
        )
        assert policy.allowed_tools == ["GITHUB_*"]
        assert policy.denied_tools == ["*_DELETE_*"]
        assert policy.allowed_toolkits == ["github"]
        assert policy.denied_toolkits == ["slack"]
        assert policy.tools_limit == 100

    def test_limit_clamped_to_max(self):
        policy = from_config({"tools_limit": 999})
        assert policy.tools_limit == 200

    def test_limit_clamped_to_min(self):
        policy = from_config({"tools_limit": 0})
        assert policy.tools_limit == 1

    def test_limit_string(self):
        policy = from_config({"tools_limit": "75"})
        assert policy.tools_limit == 75

    def test_limit_invalid_string(self):
        policy = from_config({"tools_limit": "abc"})
        assert policy.tools_limit == 50

    def test_single_string_becomes_list(self):
        policy = from_config({"allowed_tools": "GITHUB_*"})
        assert policy.allowed_tools == ["GITHUB_*"]

    def test_rejects_unbalanced_brackets(self):
        with pytest.raises(ValueError, match="unbalanced"):
            from_config({"allowed_tools": ["[unclosed"]})

    def test_rejects_empty_pattern(self):
        with pytest.raises(ValueError, match="must not be empty"):
            from_config({"denied_tools": ["GITHUB_*", "  "]})

    def test_validate_policy_patterns_accepts_valid_config(self):
        validate_policy_patterns({"allowed_toolkits": ["github", "jira"]})


# ── fnmatch validation ───────────────────────────────────────────────────────


class TestFnmatchValidation:
    def test_validate_fnmatch_pattern_accepts_wildcard(self):
        validate_fnmatch_pattern("GITHUB_*", field="allowed_tools")

    def test_validate_fnmatch_pattern_rejects_unbalanced_brackets(self):
        with pytest.raises(ValueError, match="unbalanced"):
            validate_fnmatch_pattern("[unclosed", field="allowed_tools")


# ── filter_tools ─────────────────────────────────────────────────────────────


SAMPLE_TOOLS = [
    {"name": "GITHUB_LIST_PRS", "appName": "github", "displayName": "List PRs"},
    {"name": "GITHUB_DELETE_BRANCH", "appName": "github", "displayName": "Delete Branch"},
    {"name": "JIRA_CREATE_ISSUE", "appName": "jira", "displayName": "Create Issue"},
    {"name": "SLACK_SEND_MSG", "appName": "slack", "displayName": "Send Message"},
    {"name": "SALESFORCE_GET_LEAD", "appName": "salesforce", "displayName": "Get Lead"},
]


class TestFilterTools:
    def test_no_policy_allows_all(self):
        policy = ToolFilterPolicy()
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 5

    def test_allowed_tools_filter(self):
        policy = ToolFilterPolicy(allowed_tools=["GITHUB_*"])
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 2
        assert all("GITHUB" in t["name"] for t in result)

    def test_denied_tools_filter(self):
        policy = ToolFilterPolicy(denied_tools=["*_DELETE_*"])
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 4
        assert all("DELETE" not in t["name"] for t in result)

    def test_deny_overrides_allow(self):
        policy = ToolFilterPolicy(
            allowed_tools=["GITHUB_*"],
            denied_tools=["GITHUB_DELETE_*"],
        )
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 1
        assert result[0]["name"] == "GITHUB_LIST_PRS"

    def test_allowed_toolkits(self):
        policy = ToolFilterPolicy(allowed_toolkits=["github", "jira"])
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 3
        names = {t["name"] for t in result}
        assert "SLACK_SEND_MSG" not in names
        assert "SALESFORCE_GET_LEAD" not in names

    def test_denied_toolkits(self):
        policy = ToolFilterPolicy(denied_toolkits=["slack"])
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 4
        assert all(t["appName"] != "slack" for t in result)

    def test_tools_limit(self):
        policy = ToolFilterPolicy(tools_limit=2)
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 2

    def test_combined_policy(self):
        policy = ToolFilterPolicy(
            allowed_tools=["GITHUB_*", "JIRA_*"],
            denied_tools=["*_DELETE_*"],
            tools_limit=10,
        )
        result = filter_tools(SAMPLE_TOOLS, policy)
        assert len(result) == 2
        names = {t["name"] for t in result}
        assert names == {"GITHUB_LIST_PRS", "JIRA_CREATE_ISSUE"}

    def test_empty_items(self):
        policy = ToolFilterPolicy(allowed_tools=["*"])
        result = filter_tools([], policy)
        assert result == []

    def test_toolkit_no_app_field(self):
        items = [{"name": "UNKNOWN_TOOL"}]
        policy = ToolFilterPolicy(allowed_toolkits=["github"])
        result = filter_tools(items, policy)
        assert len(result) == 0

    def test_toolkit_no_app_field_no_policy(self):
        items = [{"name": "UNKNOWN_TOOL"}]
        policy = ToolFilterPolicy()
        result = filter_tools(items, policy)
        assert len(result) == 1


# ── assert_tool_allowed ──────────────────────────────────────────────────────


class TestAssertToolAllowed:
    def test_no_policy_allows(self):
        policy = ToolFilterPolicy()
        assert_tool_allowed("ANYTHING", policy)

    def test_allowed_match(self):
        policy = ToolFilterPolicy(allowed_tools=["GITHUB_*"])
        assert_tool_allowed("GITHUB_LIST_PRS", policy)

    def test_allowed_no_match(self):
        policy = ToolFilterPolicy(allowed_tools=["GITHUB_*"])
        with pytest.raises(ConnectorPolicyError):
            assert_tool_allowed("SLACK_SEND_MSG", policy)

    def test_denied_match(self):
        policy = ToolFilterPolicy(denied_tools=["*_DELETE_*"])
        with pytest.raises(ConnectorPolicyError):
            assert_tool_allowed("GITHUB_DELETE_BRANCH", policy)

    def test_denied_no_match(self):
        policy = ToolFilterPolicy(denied_tools=["*_DELETE_*"])
        assert_tool_allowed("GITHUB_LIST_PRS", policy)

    def test_deny_overrides_allow(self):
        policy = ToolFilterPolicy(
            allowed_tools=["GITHUB_*"],
            denied_tools=["GITHUB_DELETE_*"],
        )
        with pytest.raises(ConnectorPolicyError):
            assert_tool_allowed("GITHUB_DELETE_BRANCH", policy)

    def test_error_includes_slug(self):
        policy = ToolFilterPolicy(denied_tools=["BAD_*"])
        with pytest.raises(ConnectorPolicyError) as exc_info:
            assert_tool_allowed("BAD_TOOL", policy)
        assert exc_info.value.tool_slug == "BAD_TOOL"

    def test_toolkit_allowed_at_execution(self):
        policy = ToolFilterPolicy(allowed_toolkits=["github"])
        assert_tool_allowed("GITHUB_LIST_PRS", policy, toolkit="github")

    def test_toolkit_denied_at_execution(self):
        policy = ToolFilterPolicy(denied_toolkits=["slack"])
        with pytest.raises(ConnectorPolicyError):
            assert_tool_allowed("SLACK_SEND_MSG", policy, toolkit="slack")

    def test_toolkit_not_in_allowlist_at_execution(self):
        policy = ToolFilterPolicy(allowed_toolkits=["github"])
        with pytest.raises(ConnectorPolicyError):
            assert_tool_allowed("SLACK_SEND_MSG", policy, toolkit="slack")

    def test_toolkit_none_with_allowlist_at_execution(self):
        policy = ToolFilterPolicy(allowed_toolkits=["github"])
        with pytest.raises(ConnectorPolicyError) as exc_info:
            assert_tool_allowed("UNKNOWN_TOOL", policy, toolkit=None)
        assert "no toolkit metadata" in exc_info.value.policy_detail

    def test_toolkit_none_without_policy_passes(self):
        policy = ToolFilterPolicy()
        assert_tool_allowed("ANYTHING", policy, toolkit=None)
