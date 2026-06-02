"""Activity attribution for connector tool invocations."""

from __future__ import annotations

from typing import Any

from .types import ConnectorRow


def record_connector_tool_started(
    connector: ConnectorRow,
    tool_slug: str,
    **extra: Any,
) -> dict[str, Any]:
    from ax_cli.gateway import record_gateway_activity

    return record_gateway_activity(
        "connector_tool_started",
        tool_name=f"{connector.provider}/{tool_slug}",
        connector_name=connector.name,
        connector_id=connector.id,
        provider=connector.provider,
        **extra,
    )


def record_connector_tool_completed(
    connector: ConnectorRow,
    tool_slug: str,
    *,
    duration_ms: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    from ax_cli.gateway import record_gateway_activity

    return record_gateway_activity(
        "connector_tool_completed",
        tool_name=f"{connector.provider}/{tool_slug}",
        connector_name=connector.name,
        connector_id=connector.id,
        provider=connector.provider,
        duration_ms=duration_ms,
        **extra,
    )


def record_connector_tool_failed(
    connector: ConnectorRow,
    tool_slug: str,
    *,
    error: str | None = None,
    duration_ms: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    from ax_cli.gateway import record_gateway_activity

    return record_gateway_activity(
        "connector_tool_failed",
        tool_name=f"{connector.provider}/{tool_slug}",
        connector_name=connector.name,
        connector_id=connector.id,
        provider=connector.provider,
        error=error,
        duration_ms=duration_ms,
        **extra,
    )


def record_connector_tool_denied(
    connector: ConnectorRow,
    tool_slug: str,
    *,
    policy_detail: str,
    **extra: Any,
) -> dict[str, Any]:
    from ax_cli.gateway import record_gateway_activity

    return record_gateway_activity(
        "connector_tool_denied",
        tool_name=f"{connector.provider}/{tool_slug}",
        connector_name=connector.name,
        connector_id=connector.id,
        provider=connector.provider,
        policy_detail=policy_detail,
        **extra,
    )
