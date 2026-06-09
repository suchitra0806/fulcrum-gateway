"""Connector dispatch — routes a connector row to the appropriate provider adapter."""

from __future__ import annotations

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
from ..constants import MAX_TOOLS_LIMIT
from ..errors import ConnectorPolicyError, ConnectorProviderError
from ..filtering import assert_tool_allowed, filter_tools, from_config, tool_sort_key
from ..types import ConnectorRow
from . import composio_adapter, http_mcp_adapter
from .registry import has_capability

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


def list_tools(
    connector: ConnectorRow,
    auth_env: dict[str, str],
) -> dict[str, Any]:
    adapter = _get_adapter(connector.provider)
    if hasattr(adapter, "list_tools"):
        result = adapter.list_tools(auth_env, connector.config, connector.name)
        items = result.get("tools", [])
    else:
        # Catalog providers (e.g. Composio) have no list endpoint; we page a
        # single empty search capped at MAX_TOOLS_LIMIT. Providers with more
        # than MAX_TOOLS_LIMIT tools are silently truncated here — there is no
        # pagination yet, so `total` reflects what this one page returned, not
        # everything the provider offers.
        result = adapter.search_tools(
            "",
            auth_env,
            connector.config,
            connector.name,
            limit=MAX_TOOLS_LIMIT,
        )
        items = result.get("items", [])
    policy = from_config(connector.config)
    # Match the full policy first (apply_limit=False) so we can report how many
    # tools were clipped by tools_limit. Sort by name so the clip is
    # deterministic rather than dependent on catalog order.
    matched = filter_tools(items, policy, apply_limit=False)
    matched.sort(key=tool_sort_key)
    filtered = matched[: policy.tools_limit]
    return {
        "items": filtered,
        "total": len(items),
        "matched": len(matched),
        "filtered": len(filtered),
        "limit": policy.tools_limit,
        "clipped": len(matched) > len(filtered),
    }


def search_tools(
    connector: ConnectorRow,
    query: str,
    auth_env: dict[str, str],
    *,
    apps: str | None = None,
    limit: int = 10,
    mode: str = "auto",
) -> dict[str, Any]:
    if mode == "intent" or (mode == "auto" and has_capability(connector.provider, "intent_search")):
        if not has_capability(connector.provider, "intent_search"):
            raise ConnectorProviderError(
                connector.provider,
                f"Provider {connector.provider!r} does not support intent search. Use --mode catalog.",
            )
        adapter = _get_adapter(connector.provider)
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
        list_result = list_tools(connector, auth_env)
        query_lower = query.lower()
        items = [
            item
            for item in list_result["items"]
            if query_lower in str(item.get("name", "")).lower()
            or query_lower in str(item.get("displayName", "")).lower()
            or query_lower in str(item.get("description", "")).lower()
        ]
        items = items[:limit]

    policy = from_config(connector.config)
    filtered = filter_tools(items, policy)
    return {"items": filtered}
