"""Gateway UI /api/connectors handlers — mirrors CLI connector commands."""

from __future__ import annotations

import json
from typing import Any

from .auth import auth_status, cleanup_auth, read_auth, write_auth
from .errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPolicyError,
    ConnectorProviderError,
)
from .providers.dispatch import execute_tool, initiate_connection, list_apps, search_tools
from .providers.registry import get_provider, list_providers
from .storage import add_connector, find_connector, list_connectors, remove_connector, update_connector
from .types import ConnectorRow
from .validation import validate_new_connector

_POLICY_LIST_KEYS = frozenset({"allowed_tools", "denied_tools", "allowed_toolkits", "denied_toolkits"})


def _connector_summary(row: ConnectorRow) -> dict[str, Any]:
    payload = row.to_dict()
    if row.auth_ref:
        status = auth_status(row.id, row.name)
        payload["auth_status"] = {
            "exists": bool(status.get("exists")),
            "keys": list(status.get("keys") or []),
            "permissions": status.get("permissions"),
        }
    else:
        payload["auth_status"] = {"exists": False, "keys": [], "permissions": None}
    return payload


def connectors_list_payload() -> dict[str, Any]:
    rows = list_connectors()
    return {
        "connectors": [_connector_summary(row) for row in rows],
        "count": len(rows),
        "enabled_count": sum(1 for row in rows if row.enabled),
    }


def connectors_providers_payload() -> dict[str, Any]:
    providers = list_providers()
    return {"providers": providers, "count": len(providers)}


def connector_detail_payload(ref: str) -> dict[str, Any]:
    row = find_connector(ref)
    return {"connector": _connector_summary(row)}


def connector_create(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    provider = str(body.get("provider") or "").strip()
    managed_auth = bool(body.get("managed_auth", True))
    if not name:
        raise ValueError("name is required")
    if not provider:
        raise ValueError("provider is required")
    provider_info = get_provider(provider)
    config = dict(provider_info["default_config"]) if provider_info else {}
    row = ConnectorRow.create(name, provider, managed_auth=managed_auth, config=config)
    validate_new_connector(row)
    add_connector(row)
    return {"connector": _connector_summary(row)}


def _parse_config_value(key: str, value: Any) -> Any:
    if key not in _POLICY_LIST_KEYS:
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
            return [str(parsed)]
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
    return [str(value)]


def connector_update(ref: str, body: dict[str, Any]) -> dict[str, Any]:
    row = find_connector(ref)
    updates: dict[str, Any] = {}
    if "enabled" in body:
        updates["enabled"] = bool(body.get("enabled"))
    config_updates = body.get("config")
    if isinstance(config_updates, dict) and config_updates:
        config = dict(row.config)
        for key, value in config_updates.items():
            config[str(key)] = _parse_config_value(str(key), value)
        updates["config"] = config
    for key, value in body.items():
        if key in {"enabled", "config", "name", "provider"}:
            continue
        config = dict(updates.get("config") or row.config)
        config[str(key)] = _parse_config_value(str(key), value)
        updates["config"] = config
    if not updates:
        raise ValueError("No supported fields to update (enabled, config, or config keys)")
    updated = update_connector(row.name, updates)
    return {"connector": _connector_summary(updated)}


def connector_remove(ref: str) -> dict[str, Any]:
    row = find_connector(ref)
    removed = remove_connector(ref)
    if removed.auth_ref:
        cleanup_auth(removed.id)
    return {"removed": _connector_summary(row)}


def connector_auth_status_payload(ref: str) -> dict[str, Any]:
    row = find_connector(ref)
    status = auth_status(row.id, row.name)
    return {"connector": row.name, "auth": status}


def connector_auth_write(ref: str, body: dict[str, Any]) -> dict[str, Any]:
    row = find_connector(ref)
    if not row.auth_ref:
        raise ValueError(f"Connector {row.name!r} does not use managed auth")
    raw_keys = body.get("keys") if isinstance(body.get("keys"), dict) else body
    parsed: dict[str, str] = {}
    for key, value in raw_keys.items():
        if key in {"keys", "connector"}:
            continue
        key_name = str(key).strip()
        if not key_name:
            continue
        parsed[key_name] = str(value)
    if not parsed:
        raise ValueError("At least one auth key is required")
    write_auth(row.id, row.name, parsed)
    return {"connector": row.name, "auth": auth_status(row.id, row.name)}


def connector_auth_clear(ref: str) -> dict[str, Any]:
    row = find_connector(ref)
    removed = cleanup_auth(row.id)
    return {"connector": row.name, "auth_removed": removed}


def _read_connector_auth(row: ConnectorRow) -> dict[str, str]:
    if not row.auth_ref:
        return {}
    return read_auth(row.id, row.name)


def connector_apps_payload(ref: str) -> dict[str, Any]:
    row = find_connector(ref)
    auth_env = _read_connector_auth(row)
    items = list_apps(row, auth_env)
    return {
        "connector": row.name,
        "apps": [
            {
                "app": item.get("appName"),
                "status": item.get("status"),
                "entity_id": item.get("clientUniqueUserId"),
            }
            for item in items
        ],
        "count": len(items),
    }


def connector_connect(ref: str, body: dict[str, Any]) -> dict[str, Any]:
    row = find_connector(ref)
    app = str(body.get("app") or "").strip()
    if not app:
        raise ValueError("app is required")
    auth_env = _read_connector_auth(row)
    entity_id = str(row.config.get("entity_id") or "default")
    result = initiate_connection(row, app, entity_id, auth_env)
    return {
        "connector": row.name,
        "app": app,
        "connection_status": result.get("connectionStatus"),
        "redirect_url": result.get("redirectUrl") or "",
    }


def connector_search(ref: str, body: dict[str, Any]) -> dict[str, Any]:
    row = find_connector(ref)
    if not row.enabled:
        raise ValueError(f"Connector {row.name!r} is disabled")
    query = str(body.get("query") or body.get("use_case") or "").strip()
    if not query:
        raise ValueError("query is required")
    mode = str(body.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "intent", "catalog"}:
        raise ValueError("mode must be auto, intent, or catalog")
    limit = int(body.get("limit") or 10)
    app = str(body.get("app") or "").strip() or None
    auth_env = _read_connector_auth(row)
    result = search_tools(row, query, auth_env, apps=app, limit=limit, mode=mode)
    items = result.get("items", [])
    return {
        "connector": row.name,
        "query": query,
        "mode": mode,
        "tools": items,
        "count": len(items),
    }


def connector_call(ref: str, body: dict[str, Any]) -> dict[str, Any]:
    row = find_connector(ref)
    if not row.enabled:
        raise ValueError(f"Connector {row.name!r} is disabled")
    tool = str(body.get("tool") or "").strip()
    if not tool:
        raise ValueError("tool is required")
    raw_args = body.get("args")
    if raw_args is None:
        raw_args = body.get("args_json")
    if isinstance(raw_args, str):
        args = json.loads(raw_args) if raw_args.strip() else {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("args must be a JSON object")
    dry_run = bool(body.get("dry_run", False))
    auth_env = _read_connector_auth(row)
    if dry_run:
        return {
            "connector": row.name,
            "provider": row.provider,
            "tool": tool,
            "args": args,
            "auth_keys": sorted(auth_env.keys()),
            "dry_run": True,
        }
    result = execute_tool(row, tool, args, auth_env)
    return {"connector": row.name, "tool": tool, "result": result}


def parse_connector_api_path(path: str) -> tuple[str, str, str]:
    """Return (kind, ref, suffix) for /api/connectors routes.

    kind is one of: list, providers, detail, auth, apps, connect, tools_search, tools_call.
    """
    prefix = "/api/connectors"
    if path == prefix:
        return "list", "", ""
    if path == f"{prefix}/providers":
        return "providers", "", ""
    if not path.startswith(f"{prefix}/"):
        return "", "", ""
    remainder = path.removeprefix(f"{prefix}/")
    if remainder == "providers":
        return "providers", "", ""
    if "/tools/search" in remainder:
        ref = remainder.removesuffix("/tools/search")
        return "tools_search", unquote_ref(ref), "/tools/search"
    if "/tools/call" in remainder:
        ref = remainder.removesuffix("/tools/call")
        return "tools_call", unquote_ref(ref), "/tools/call"
    if remainder.endswith("/auth"):
        ref = remainder.removesuffix("/auth")
        return "auth", unquote_ref(ref), "/auth"
    if remainder.endswith("/apps"):
        ref = remainder.removesuffix("/apps")
        return "apps", unquote_ref(ref), "/apps"
    if remainder.endswith("/connect"):
        ref = remainder.removesuffix("/connect")
        return "connect", unquote_ref(ref), "/connect"
    return "detail", unquote_ref(remainder), ""


def unquote_ref(raw: str) -> str:
    from urllib.parse import unquote

    return unquote(raw.strip().strip("/"))


__all__ = [
    "ConnectorAuthError",
    "ConnectorError",
    "ConnectorNotFoundError",
    "ConnectorPolicyError",
    "ConnectorProviderError",
    "connector_apps_payload",
    "connector_auth_clear",
    "connector_auth_status_payload",
    "connector_auth_write",
    "connector_call",
    "connector_connect",
    "connector_create",
    "connector_detail_payload",
    "connector_remove",
    "connector_search",
    "connector_update",
    "connectors_list_payload",
    "connectors_providers_payload",
    "parse_connector_api_path",
]
