"""Activity attribution for connector tool invocations."""

from __future__ import annotations

import re
import uuid
from typing import Any

from .constants import MAX_ACTIVITY_ERROR_LEN
from .types import ConnectorRow

_TRUNCATED_SUFFIX = "...(truncated)"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"axp_[a-zA-Z0-9_]+\.[A-Za-z0-9_\-]+"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]+"),
    re.compile(r"sk-[A-Za-z0-9\-]+"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
)


def new_invocation_id() -> str:
    """Return a unique ID correlating started/completed/failed/denied events."""
    return str(uuid.uuid4())


def sanitize_activity_text(text: str | None) -> str | None:
    """Redact common secret shapes and bound persisted error/detail strings."""
    if text is None:
        return None
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    if len(redacted) <= MAX_ACTIVITY_ERROR_LEN:
        return redacted
    keep = MAX_ACTIVITY_ERROR_LEN - len(_TRUNCATED_SUFFIX)
    return redacted[:keep] + _TRUNCATED_SUFFIX


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
        error=sanitize_activity_text(error),
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
        policy_detail=sanitize_activity_text(policy_detail) or "",
        **extra,
    )
