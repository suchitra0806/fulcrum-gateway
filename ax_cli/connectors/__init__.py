"""Outbound connectors — gateway-managed tool providers.

Public API re-exports for convenience.
"""

from .activity import (
    record_connector_tool_completed,
    record_connector_tool_denied,
    record_connector_tool_failed,
    record_connector_tool_started,
)
from .api import (
    ConnectorToolCallResult,
    ConnectorToolMatch,
    ConnectorToolSearchResult,
    execute_connector_tool,
    search_connector_tools,
)
from .auth import auth_status, cleanup_auth, read_auth, write_auth
from .errors import (
    ConnectorAuthError,
    ConnectorAuthHTTPError,
    ConnectorNotFoundError,
    ConnectorPolicyError,
    ConnectorProviderError,
    ConnectorRateLimitError,
    ConnectorTransientError,
)
from .filtering import ToolFilterPolicy, assert_tool_allowed, filter_tools, from_config
from .paths import auth_dir, auth_path, connectors_dir, connectors_registry_path
from .providers.dispatch import execute_tool, initiate_connection, list_apps, list_tools, search_tools
from .storage import (
    add_connector,
    find_connector,
    list_connectors,
    load_connectors_registry,
    remove_connector,
    save_connectors_registry,
    update_connector,
)
from .types import ConnectorRow
from .validation import validate_new_connector

__all__ = [
    "ConnectorToolCallResult",
    "ConnectorToolMatch",
    "ConnectorToolSearchResult",
    "ConnectorAuthError",
    "ConnectorAuthHTTPError",
    "ConnectorNotFoundError",
    "ConnectorPolicyError",
    "ConnectorProviderError",
    "ConnectorRateLimitError",
    "ConnectorTransientError",
    "ConnectorRow",
    "ToolFilterPolicy",
    "add_connector",
    "assert_tool_allowed",
    "auth_dir",
    "auth_path",
    "auth_status",
    "cleanup_auth",
    "connectors_dir",
    "connectors_registry_path",
    "execute_connector_tool",
    "execute_tool",
    "filter_tools",
    "find_connector",
    "from_config",
    "initiate_connection",
    "list_apps",
    "list_connectors",
    "list_tools",
    "load_connectors_registry",
    "read_auth",
    "record_connector_tool_completed",
    "record_connector_tool_denied",
    "record_connector_tool_failed",
    "record_connector_tool_started",
    "remove_connector",
    "save_connectors_registry",
    "search_connector_tools",
    "search_tools",
    "update_connector",
    "validate_new_connector",
    "write_auth",
]
