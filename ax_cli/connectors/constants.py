"""Shared constants for the connectors package."""

from __future__ import annotations

DEFAULT_COMPOSIO_BASE_URL = "https://backend.composio.dev/api/v3"
DEFAULT_ENTITY_ID = "default"

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0

DEFAULT_TOOLS_LIMIT = 50
MAX_TOOLS_LIMIT = 200

# Config key names for tool policy fields
KEY_ALLOWED_TOOLS = "allowed_tools"
KEY_DENIED_TOOLS = "denied_tools"
KEY_ALLOWED_TOOLKITS = "allowed_toolkits"
KEY_DENIED_TOOLKITS = "denied_toolkits"
KEY_TOOLS_LIMIT = "tools_limit"
