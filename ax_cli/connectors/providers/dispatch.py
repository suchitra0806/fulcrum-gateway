"""Connector dispatch — routes a connector row to the appropriate provider adapter."""

from __future__ import annotations

import logging
import time
from types import ModuleType
from typing import Any

from ..activity import (
    new_invocation_id,
    record_connector_tool_completed,
    record_connector_tool_denied,
    record_connector_tool_failed,
    record_connector_tool_started,
)
from ..constants import CATALOG_PAGE_SIZE, MAX_CATALOG_PAGES
from ..errors import ConnectorPolicyError, ConnectorProviderError
from ..filtering import assert_tool_allowed, filter_tools, from_config, tool_sort_key
from ..types import ConnectorRow
from . import composio_adapter, http_mcp_adapter
from .registry import has_capability

log = logging.getLogger("connectors.dispatch")

_ADAPTERS: dict[str, ModuleType] = {
    "composio": composio_adapter,
    "http_mcp": http_mcp_adapter,
}


def _get_adapter(provider: str) -> ModuleType:
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise ConnectorProviderError(provider, f"No adapter for provider {provider!r}")
    return adapter


def list_apps(
    connector: ConnectorRow,
    auth_env: dict[str, str],
) -> list[dict[str, Any]]:
    adapter = _get_adapter(connector.provider)
    return adapter.list_apps(auth_env, connector.config, connector.name)


def initiate_connection(
    connector: ConnectorRow,
    app_name: str,
    entity_id: str,
    auth_env: dict[str, str],
) -> dict[str, Any]:
    adapter = _get_adapter(connector.provider)
    return adapter.initiate_connection(
        app_name,
        entity_id,
        auth_env,
        connector.config,
        connector.name,
    )


def execute_tool(
    connector: ConnectorRow,
    tool_slug: str,
    args: dict[str, Any],
    auth_env: dict[str, str],
    *,
    toolkit: str | None = None,
    agent_name: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    policy = from_config(connector.config)
    identity = {k: v for k, v in [("agent_name", agent_name), ("agent_id", agent_id)] if v}
    invocation_ctx = {**identity, "invocation_id": new_invocation_id()}
    # Callers that have the toolkit from list_tools context pass it in;
    # callers that only have the slug (notably the Hermes _connector_call
    # tool) get None. Let the adapter recover the toolkit from the slug
    # so allowed_toolkits / denied_toolkits policies still apply on the
    # execution path — otherwise `_toolkit_allowed(None, policy)` rejects
    # every call when an allow-list is set (#128).
    if toolkit is None:
        adapter_mod = _ADAPTERS.get(connector.provider)
        derive = getattr(adapter_mod, "toolkit_from_slug", None) if adapter_mod is not None else None
        if derive is not None:
            toolkit = derive(tool_slug)
    try:
        assert_tool_allowed(tool_slug, policy, toolkit=toolkit)
    except ConnectorPolicyError as exc:
        record_connector_tool_denied(connector, tool_slug, policy_detail=exc.policy_detail, **invocation_ctx)
        raise

    record_connector_tool_started(connector, tool_slug, **invocation_ctx)
    t0 = time.monotonic()
    try:
        adapter = _get_adapter(connector.provider)
        result = adapter.execute_tool(
            tool_slug,
            args,
            auth_env,
            connector.config,
            connector.name,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        record_connector_tool_failed(connector, tool_slug, error=str(exc), duration_ms=duration_ms, **invocation_ctx)
        raise
    duration_ms = int((time.monotonic() - t0) * 1000)
    record_connector_tool_completed(connector, tool_slug, duration_ms=duration_ms, **invocation_ctx)
    return result


def _drain_catalog_tools(
    adapter: ModuleType,
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
) -> tuple[list[dict[str, Any]], int | None, bool, str | None]:
    """Drain paginated catalog search results (Composio and similar providers).

    Mid-drain provider errors degrade with a warning: pages fetched before the
    failure are returned and ``catalog_partial`` is set (#285). Fail-closed when
    the first page fails (nothing usable to return).
    """
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    provider_total: int | None = None
    catalog_partial = False
    drain_error: str | None = None

    for page_idx in range(MAX_CATALOG_PAGES):
        try:
            result = adapter.search_tools(
                "",
                auth_env,
                config,
                connector_name,
                limit=MAX_TOOLS_LIMIT,
                cursor=cursor,
            )
        except Exception as exc:
            if not items:
                raise
            catalog_partial = True
            drain_error = str(exc)
            provider_total = None
            log.warning(
                "%r catalog drain failed mid-pagination after page %d (%d tools fetched): %s. "
                "Returning partial catalog; matched/total are lower bounds only.",
                connector_name,
                page_idx + 1,
                len(items),
                drain_error,
            )
            break

        items.extend(result.get("items", []))
        if provider_total is None and result.get("total_items") is not None:
            provider_total = int(result["total_items"])

        next_cursor = result.get("next_cursor")
        if not next_cursor:
            break
        cursor = str(next_cursor)
    else:
        log.warning(
            "Catalog drain for %r hit MAX_CATALOG_PAGES (%s); inventory may be truncated",
            connector_name,
            MAX_CATALOG_PAGES,
        )

    return items, provider_total, catalog_partial, drain_error


def list_tools(
    connector: ConnectorRow,
    auth_env: dict[str, str],
) -> dict[str, Any]:
    adapter = _get_adapter(connector.provider)
    catalog_partial = False
    catalog_drain_error: str | None = None
    if hasattr(adapter, "list_tools"):
        result = adapter.list_tools(auth_env, connector.config, connector.name)
        items = result.get("tools", [])
        provider_total = None
    else:
        items, provider_total, catalog_partial, catalog_drain_error = _drain_catalog_tools(
            adapter,
            auth_env,
            connector.config,
            connector.name,
        )
    catalog_drained = len(items)
    policy = from_config(connector.config)
    # Match the full policy first (apply_limit=False) so we can report how many
    # tools were clipped by tools_limit. Sort by name so the clip is
    # deterministic rather than dependent on catalog order.
    matched = filter_tools(items, policy, apply_limit=False)
    matched.sort(key=tool_sort_key)
    filtered = matched[: policy.tools_limit]
    if catalog_partial:
        total = catalog_drained
    else:
        total = provider_total if provider_total is not None else catalog_drained
    return {
        "items": filtered,
        "total": total,
        "matched": len(matched),
        "filtered": len(filtered),
        "limit": policy.tools_limit,
        "clipped": len(matched) > len(filtered),
        "catalog_drained": catalog_drained,
        "catalog_partial": catalog_partial,
        "catalog_drain_error": catalog_drain_error,
    }


def resolve_search_mode(provider: str, mode: str) -> str:
    normalized = str(mode or "auto").strip().lower()
    if normalized in {"catalog", "intent"}:
        return normalized
    if has_capability(provider, "intent_search"):
        return "intent"
    return "catalog"


def _filter_listed_tools(
    connector: ConnectorRow,
    query: str,
    auth_env: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    """Keyword filter over list_tools results (http_mcp and similar providers)."""
    list_result = list_tools(connector, auth_env)
    query_lower = query.lower()
    items = [
        item
        for item in list_result["items"]
        if query_lower in str(item.get("name", "")).lower()
        or query_lower in str(item.get("displayName", "")).lower()
        or query_lower in str(item.get("description", "")).lower()
    ]
    if limit > 0:
        items = items[: max(1, int(limit))]
    return items


def search_tools(
    connector: ConnectorRow,
    query: str,
    auth_env: dict[str, str],
    *,
    apps: str | None = None,
    limit: int = 10,
    mode: str = "auto",
    session_id: str | None = None,
) -> dict[str, Any]:
    resolved_mode = resolve_search_mode(connector.provider, mode)
    session_out: str | None = None

    if resolved_mode == "intent":
        if not has_capability(connector.provider, "intent_search"):
            raise ConnectorProviderError(
                connector.provider,
                f"Provider {connector.provider!r} does not support intent search. Use --mode catalog.",
            )
        adapter = _get_adapter(connector.provider)
        if not hasattr(adapter, "search_tools_intent"):
            raise ConnectorProviderError(
                connector.provider,
                f"Provider {connector.provider!r} does not implement intent search. Use --mode catalog.",
            )
        result = adapter.search_tools_intent(
            query,
            auth_env,
            connector.config,
            connector.name,
            apps=apps,
            limit=limit,
            session_id=session_id,
        )
        items = result.get("items", [])
        session_out = result.get("session_id")
    else:
        adapter = _get_adapter(connector.provider)
        if hasattr(adapter, "search_tools"):
            result = adapter.search_tools(
                query,
                auth_env,
                connector.config,
                connector.name,
                apps=apps,
                limit=limit,
            )
            items = result.get("items", [])
        else:
            items = _filter_listed_tools(connector, query, auth_env, limit)

    policy = from_config(connector.config)
    filtered = filter_tools(items, policy, apply_limit=False)
    if limit > 0:
        filtered = filtered[: max(1, int(limit))]
    payload: dict[str, Any] = {"items": filtered, "mode": resolved_mode}
    if session_out:
        payload["session_id"] = session_out
    return payload
