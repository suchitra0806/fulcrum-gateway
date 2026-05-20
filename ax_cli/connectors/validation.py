"""Connector row validation."""

from __future__ import annotations

from .errors import ConnectorError
from .storage import list_connectors
from .types import ConnectorRow


def validate_new_connector(row: ConnectorRow) -> None:
    if not row.name or not row.name.strip():
        raise ConnectorError("Connector name must not be empty")

    if not row.provider or not row.provider.strip():
        raise ConnectorError("Connector provider must not be empty")

    from .providers.registry import get_provider

    if get_provider(row.provider) is None:
        from .providers.registry import list_providers

        available = ", ".join(p["name"] for p in list_providers())
        raise ConnectorError(
            f"Unknown provider {row.provider!r}. Available: {available}"
        )

    name_lower = row.name.lower()
    for existing in list_connectors():
        if existing.name.lower() == name_lower:
            raise ConnectorError(
                f"Connector name {row.name!r} already exists (id={existing.id})"
            )
