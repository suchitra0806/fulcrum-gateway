"""High-level connector API for bridges and runtimes.

Wraps registry lookup, auth, dispatch, and policy into stable entry points
used by ``langgraph_composio_bridge`` and other Gateway-managed agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .auth import read_auth
from .errors import ConnectorProviderError
from .providers.dispatch import execute_tool, search_tools
from .providers.registry import has_capability
from .storage import find_connector


@dataclass(frozen=True)
class ConnectorToolMatch:
    slug: str
    name: str
    description: str = ""


@dataclass
class ConnectorToolSearchResult:
    mode: str
    successful: bool
    tools: list[ConnectorToolMatch] = field(default_factory=list)
    session_id: str | None = None
    error: str | None = None


@dataclass
class ConnectorToolCallResult:
    successful: bool
    error: str | None = None
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "successful": self.successful,
            "error": self.error,
            "data": self.data,
        }


def _tool_match_from_item(item: dict[str, Any]) -> ConnectorToolMatch:
    slug = str(item.get("enum") or item.get("slug") or item.get("name") or "").strip()
    if not slug:
        slug = "UNKNOWN"
    name = str(item.get("displayName") or item.get("display_name") or item.get("name") or slug).strip()
    description = str(item.get("description") or item.get("human_description") or "").strip()
    return ConnectorToolMatch(slug=slug, name=name, description=description)


def _resolve_row_and_auth(connector_ref: str):
    row = find_connector(connector_ref)
    if not row.enabled:
        raise ConnectorProviderError(
            row.provider,
            f"connector {row.name!r} is disabled. Run: ax gateway connectors enable {row.name}",
        )
    auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    return row, auth_env


def _effective_search_mode(provider: str, mode: str) -> str:
    normalized = str(mode or "auto").strip().lower()
    if normalized in {"catalog", "intent"}:
        return normalized
    if has_capability(provider, "intent_search"):
        return "intent"
    return "catalog"


def search_connector_tools(
    connector_ref: str,
    use_case: str,
    *,
    mode: str = "auto",
    limit: int = 10,
    apps: str | None = None,
) -> ConnectorToolSearchResult:
    """Search tools for a connector by natural-language use case."""
    row, auth_env = _resolve_row_and_auth(connector_ref)
    effective_mode = _effective_search_mode(row.provider, mode)
    try:
        raw = search_tools(
            row,
            use_case,
            auth_env,
            apps=apps,
            limit=limit,
            mode=effective_mode if mode != "auto" else mode,
        )
    except ConnectorProviderError as exc:
        return ConnectorToolSearchResult(
            mode=effective_mode,
            successful=False,
            tools=[],
            error=str(exc),
        )
    items = raw.get("items", []) if isinstance(raw, dict) else []
    if not isinstance(items, list):
        items = []
    tools = [_tool_match_from_item(item) for item in items if isinstance(item, dict)]
    return ConnectorToolSearchResult(
        mode=effective_mode,
        successful=True,
        tools=tools,
        session_id=raw.get("session_id") if isinstance(raw, dict) else None,
        error=None,
    )


def execute_connector_tool(
    connector_ref: str,
    tool_slug: str,
    arguments: dict[str, Any],
    *,
    toolkit: str | None = None,
    agent_name: str | None = None,
    agent_id: str | None = None,
) -> ConnectorToolCallResult:
    """Execute a tool slug via a registered connector (policy + activity applied)."""
    row, auth_env = _resolve_row_and_auth(connector_ref)
    try:
        result = execute_tool(
            row,
            tool_slug,
            arguments,
            auth_env,
            toolkit=toolkit,
            agent_name=agent_name,
            agent_id=agent_id,
        )
    except Exception as exc:
        return ConnectorToolCallResult(successful=False, error=str(exc), data=None)

    if not isinstance(result, dict):
        return ConnectorToolCallResult(successful=True, data=result)

    error = result.get("error") or result.get("message")
    if isinstance(error, dict):
        error = error.get("message") or str(error)
    elif error is not None:
        error = str(error).strip() or None

    successful = result.get("successful")
    if successful is None:
        successful = result.get("successfull")  # Composio typo in some responses
    if successful is None:
        successful = error is None and result.get("status", "").lower() not in {"failed", "error"}

    data = result.get("data")
    if data is None and successful:
        data = {k: v for k, v in result.items() if k not in {"error", "message", "successful", "successfull"}}

    return ConnectorToolCallResult(
        successful=bool(successful),
        error=error,
        data=data,
    )
