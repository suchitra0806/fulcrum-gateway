"""Composio intent search via the ``COMPOSIO_SEARCH_TOOLS`` meta-tool."""

from __future__ import annotations

import re
from typing import Any

from ..errors import ConnectorProviderError

_COMPOSIO_SEARCH_TOOL = "COMPOSIO_SEARCH_TOOLS"
_SLUG_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def search_tools_intent(
    query: str,
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
    *,
    apps: str | None = None,
    limit: int = 10,
    session_id: str | None = None,
    known_fields: str | None = None,
) -> dict[str, Any]:
    """Run Composio intent search and return catalog-shaped tool items."""
    needle = str(query or "").strip()
    if not needle:
        raise ConnectorProviderError("composio", "query is required for intent search")

    query_payload: dict[str, Any] = {"use_case": needle}
    if known_fields:
        query_payload["known_fields"] = str(known_fields).strip()

    arguments: dict[str, Any] = {"queries": [query_payload]}
    if session_id:
        arguments["session"] = {"id": str(session_id).strip()}
    else:
        arguments["session"] = {"generate_id": True}

    # Lazy import avoids composio_adapter ↔ composio_intent circular load.
    from . import composio_adapter

    raw = composio_adapter.execute_tool(
        _COMPOSIO_SEARCH_TOOL,
        arguments,
        auth_env,
        config,
        connector_name,
    )

    successful = raw.get("successful")
    if successful is None:
        successful = raw.get("status", "").lower() not in {"failed", "error"}
    if not successful:
        err = raw.get("error") or raw.get("message") or "Composio intent search failed"
        raise ConnectorProviderError("composio", str(err))

    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    items, parsed_session = _parse_search_tools_response(data)
    if not parsed_session:
        parsed_session = _extract_session_id(raw)

    if apps:
        app_prefix = str(apps).strip().upper().replace("-", "_")
        if app_prefix:
            items = [
                item
                for item in items
                if str(item.get("name", "")).upper().startswith(f"{app_prefix}_")
                or str(item.get("appName", "")).lower() == str(apps).strip().lower()
            ]

    effective_limit = max(1, int(limit)) if limit else len(items)
    items = items[:effective_limit]

    payload: dict[str, Any] = {"items": items, "mode": "intent"}
    if parsed_session:
        payload["session_id"] = parsed_session
    return payload


def _parse_search_tools_response(data: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Best-effort extraction of tool slugs from COMPOSIO_SEARCH_TOOLS output."""
    session_id = _extract_session_id(data) if isinstance(data, dict) else None
    slugs: set[str] = set()
    _collect_slugs(data, slugs)
    items = [
        {"name": slug, "displayName": slug, "description": ""}
        for slug in sorted(slugs)
        if _SLUG_RE.match(slug) and slug != _COMPOSIO_SEARCH_TOOL
    ]
    return items, session_id


def _collect_slugs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key in ("tool_slug", "slug", "primary_tool_slugs", "related_tool_slugs"):
            val = node.get(key)
            if isinstance(val, str) and _SLUG_RE.match(val.strip()):
                out.add(val.strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and _SLUG_RE.match(item.strip()):
                        out.add(item.strip())
        for value in node.values():
            _collect_slugs(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_slugs(item, out)
    elif isinstance(node, str) and _SLUG_RE.match(node.strip()) and len(node.strip()) > 8:
        out.add(node.strip())


def _extract_session_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    session = data.get("session")
    if isinstance(session, dict):
        sid = session.get("id") or session.get("session_id")
        if sid:
            return str(sid).strip()
    for key in ("session_id", "sessionId"):
        val = data.get(key)
        if val:
            return str(val).strip()
    return None
