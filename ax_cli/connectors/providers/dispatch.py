"""Connector dispatch — routes a connector row to the appropriate provider adapter."""

from __future__ import annotations

from typing import Any

from ..errors import ConnectorProviderError
from ..types import ConnectorRow
from . import composio_adapter


def execute_tool(
    connector: ConnectorRow,
    tool_slug: str,
    args: dict[str, Any],
    auth_env: dict[str, str],
) -> dict[str, Any]:
    if connector.provider == "composio":
        return composio_adapter.execute_tool(
            tool_slug, args, auth_env, connector.config, connector.name,
        )
    raise ConnectorProviderError(
        connector.provider, f"No adapter for provider {connector.provider!r}"
    )


def search_tools(
    connector: ConnectorRow,
    query: str,
    auth_env: dict[str, str],
    *,
    limit: int = 10,
) -> dict[str, Any]:
    if connector.provider == "composio":
        return composio_adapter.search_tools(
            query, auth_env, connector.config, connector.name, limit=limit,
        )
    raise ConnectorProviderError(
        connector.provider, f"No search adapter for provider {connector.provider!r}"
    )
