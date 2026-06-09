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
) -> dict[str, Any]:
    """Run Composio intent search and return catalog-shaped tool items."""
    needle = str(query or "").strip()
    if not needle:
        raise ConnectorProviderError("composio", "query is required for intent search")

    arguments: dict[str, Any] = {
        "queries": [{"use_case": needle}],
        "session": {"id": str(session_id).strip()} if session_id else {"generate_id": True},
    }

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


def _is_tool_slug(value: str) -> bool:
    slug = value.strip()
    return bool(_SLUG_RE.match(slug)) and slug != _COMPOSIO_SEARCH_TOOL


def _add_slug(value: object, out: set[str]) -> None:
    if isinstance(value, str) and _is_tool_slug(value):
        out.add(value.strip())


def _add_slug_list(values: object, out: set[str]) -> None:
    if not isinstance(values, list):
        return
    for item in values:
        _add_slug(item, out)


def _collect_slugs_from_results(results: object, out: set[str]) -> None:
    if not isinstance(results, list):
        return
    for entry in results:
        if not isinstance(entry, dict):
            continue
        _add_slug_list(entry.get("primary_tool_slugs"), out)
        _add_slug_list(entry.get("related_tool_slugs"), out)
        _add_slug(entry.get("tool_slug"), out)
        _add_slug(entry.get("slug"), out)


def _parse_search_tools_response(data: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Extract tool slugs from documented COMPOSIO_SEARCH_TOOLS response fields."""
    session_id = _extract_session_id(data) if isinstance(data, dict) else None
    slugs: set[str] = set()
    if isinstance(data, dict):
        _collect_slugs_from_results(data.get("results"), slugs)
        _add_slug_list(data.get("primary_tool_slugs"), slugs)
        _add_slug_list(data.get("related_tool_slugs"), slugs)
        tool_schemas = data.get("tool_schemas")
        if isinstance(tool_schemas, dict):
            for key in tool_schemas:
                _add_slug(str(key), slugs)
    items = [
        {"name": slug, "displayName": slug, "description": ""}
        for slug in sorted(slugs)
    ]
    return items, session_id


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
