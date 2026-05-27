"""Tool policy evaluation — fnmatch-based allow/deny filtering."""

from __future__ import annotations

import dataclasses
from fnmatch import fnmatch
from typing import Any

from .constants import (
    DEFAULT_TOOLS_LIMIT,
    KEY_ALLOWED_TOOLKITS,
    KEY_ALLOWED_TOOLS,
    KEY_DENIED_TOOLKITS,
    KEY_DENIED_TOOLS,
    KEY_TOOLS_LIMIT,
    MAX_TOOLS_LIMIT,
)
from .errors import ConnectorPolicyError


@dataclasses.dataclass(frozen=True)
class ToolFilterPolicy:
    allowed_tools: list[str] = dataclasses.field(default_factory=list)
    denied_tools: list[str] = dataclasses.field(default_factory=list)
    allowed_toolkits: list[str] = dataclasses.field(default_factory=list)
    denied_toolkits: list[str] = dataclasses.field(default_factory=list)
    tools_limit: int = DEFAULT_TOOLS_LIMIT


def from_config(config: dict[str, Any]) -> ToolFilterPolicy:
    limit = config.get(KEY_TOOLS_LIMIT, DEFAULT_TOOLS_LIMIT)
    if isinstance(limit, str):
        try:
            limit = int(limit)
        except ValueError:
            limit = DEFAULT_TOOLS_LIMIT
    limit = max(1, min(int(limit), MAX_TOOLS_LIMIT))
    return ToolFilterPolicy(
        allowed_tools=_as_list(config.get(KEY_ALLOWED_TOOLS)),
        denied_tools=_as_list(config.get(KEY_DENIED_TOOLS)),
        allowed_toolkits=_as_list(config.get(KEY_ALLOWED_TOOLKITS)),
        denied_toolkits=_as_list(config.get(KEY_DENIED_TOOLKITS)),
        tools_limit=limit,
    )


def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, list):
        return [str(v) for v in val if v]
    return []


def _matches_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatch(value, pat) for pat in patterns)


def _tool_allowed_by_name(name: str, policy: ToolFilterPolicy) -> bool:
    if policy.denied_tools and _matches_any(name, policy.denied_tools):
        return False
    if policy.allowed_tools and not _matches_any(name, policy.allowed_tools):
        return False
    return True


def _toolkit_allowed(toolkit: str | None, policy: ToolFilterPolicy) -> bool:
    if not toolkit:
        return not policy.allowed_toolkits
    if policy.denied_toolkits and _matches_any(toolkit, policy.denied_toolkits):
        return False
    if policy.allowed_toolkits and not _matches_any(toolkit, policy.allowed_toolkits):
        return False
    return True


def filter_tools(items: list[dict[str, Any]], policy: ToolFilterPolicy) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or item.get("enum") or "")
        toolkit = str(item.get("appName") or item.get("toolkit") or "") or None
        if not _tool_allowed_by_name(name, policy):
            continue
        if not _toolkit_allowed(toolkit, policy):
            continue
        result.append(item)
        if len(result) >= policy.tools_limit:
            break
    return result


def assert_tool_allowed(tool_slug: str, policy: ToolFilterPolicy, *, toolkit: str | None = None) -> None:
    if policy.denied_tools and _matches_any(tool_slug, policy.denied_tools):
        raise ConnectorPolicyError(tool_slug, f"matched denied pattern in {policy.denied_tools}")
    if policy.allowed_tools and not _matches_any(tool_slug, policy.allowed_tools):
        raise ConnectorPolicyError(tool_slug, f"did not match any allowed pattern in {policy.allowed_tools}")
    if not _toolkit_allowed(toolkit, policy):
        detail = f"toolkit {toolkit!r}" if toolkit else "no toolkit"
        if policy.denied_toolkits and toolkit and _matches_any(toolkit, policy.denied_toolkits):
            raise ConnectorPolicyError(
                tool_slug, f"{detail} matched denied toolkit pattern in {policy.denied_toolkits}"
            )
        raise ConnectorPolicyError(tool_slug, f"{detail} not in allowed toolkits {policy.allowed_toolkits}")
