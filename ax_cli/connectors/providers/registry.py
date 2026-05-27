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
        "capabilities": ["execute", "list_tools", "intent_search"],
        "default_config": {
            "composio_base_url": "https://backend.composio.dev/api/v3",
            "entity_id": "default",
            "connected_account_id": None,
            "app_name": None,
            "classification": None,
        },
    },
    "http_mcp": {
        "name": "http_mcp",
        "display_name": "HTTP MCP",
        "description": "Generic JSON-RPC adapter for any MCP-compliant server (GovCloud, self-hosted).",
        "required_auth_keys": [],
        "optional_auth_keys": ["HTTP_MCP_API_KEY"],
        "capabilities": ["execute", "list_tools"],
        "default_config": {
            "base_url": None,
            "auth_header_name": "Authorization",
            "auth_prefix": "Bearer",
            "classification": None,
        },
    },
}


def get_provider(name: str) -> dict[str, Any] | None:
    return PROVIDERS.get(name.lower())


def list_providers() -> list[dict[str, Any]]:
    return list(PROVIDERS.values())


def has_capability(provider_name: str, capability: str) -> bool:
    provider = get_provider(provider_name)
    if provider is None:
        return False
    return capability in provider.get("capabilities", [])
