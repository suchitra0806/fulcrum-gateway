"""Provider catalog — available connector provider types."""

from __future__ import annotations

from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    "composio": {
        "name": "composio",
        "display_name": "Composio",
        "description": "HTTP adapter for Composio's 500+ SaaS integrations (IL2/development). No SDK dependency.",
        "required_auth_keys": ["COMPOSIO_API_KEY"],
        "optional_auth_keys": ["COMPOSIO_ENTITY_ID", "COMPOSIO_CONNECTED_ACCOUNT_ID"],
        "default_config": {
            "composio_base_url": "https://backend.composio.dev/api/v2",
            "entity_id": "default",
            "connected_account_id": None,
            "app_name": None,
            "classification": None,
        },
    },
}


def get_provider(name: str) -> dict[str, Any] | None:
    return PROVIDERS.get(name.lower())


def list_providers() -> list[dict[str, Any]]:
    return list(PROVIDERS.values())
