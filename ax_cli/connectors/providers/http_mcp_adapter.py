"""HTTP MCP adapter — JSON-RPC 2.0 over HTTP for MCP-compliant servers.

Supports ``tools/list`` and ``tools/call`` methods. No intent search
capability — that is Composio-specific.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..constants import CONNECT_TIMEOUT, READ_TIMEOUT
from ..errors import ConnectorProviderError, classify_provider_error

log = logging.getLogger("connectors.http_mcp")


def _base_url(config: dict[str, Any], connector_name: str) -> str:
    url = str(config.get("base_url") or "").strip().rstrip("/")
    if not url:
        raise ConnectorProviderError(
            "http_mcp",
            f"base_url not configured for connector {connector_name!r}. "
            "Run: ax gateway connectors set <ref> base_url <url>",
        )
    return url


def _headers(auth_env: dict[str, str], config: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = auth_env.get("HTTP_MCP_API_KEY", "").strip()
    if api_key:
        header_name = str(config.get("auth_header_name") or "Authorization")
        raw_prefix = config.get("auth_prefix")
        prefix = str(raw_prefix).strip() if raw_prefix is not None else "Bearer"
        headers[header_name] = f"{prefix} {api_key}" if prefix else api_key
    return headers


def _jsonrpc_request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        req["params"] = params
    return req


def _post(url: str, body: dict[str, Any], headers: dict[str, str], context: str) -> dict[str, Any]:
    try:
        resp = httpx.post(
            url,
            json=body,
            headers=headers,
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=CONNECT_TIMEOUT),
        )
    except httpx.TimeoutException as e:
        raise ConnectorProviderError("http_mcp", f"Timeout {context}: {e!r}") from e
    except httpx.HTTPError as e:
        raise ConnectorProviderError("http_mcp", f"HTTP error {context}: {e!r}") from e

    if not resp.is_success:
        try:
            body_data = resp.json()
        except Exception:
            body_data = {}
        detail = str(body_data.get("error", {}).get("message", "") or resp.text[:200])
        raise classify_provider_error(
            "http_mcp",
            f"{context}: {detail}",
            status_code=resp.status_code,
        )

    data = resp.json()
    if "error" in data and data["error"]:
        err = data["error"]
        msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        raise ConnectorProviderError(
            "http_mcp",
            f"JSON-RPC error {context}: {msg}" + (f" (code {code})" if code else ""),
        )
    return data.get("result", data)


def list_tools(
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
) -> dict[str, Any]:
    base = _base_url(config, connector_name)
    headers = _headers(auth_env, config)
    body = _jsonrpc_request("tools/list")
    result = _post(base, body, headers, "listing tools")
    if isinstance(result, dict) and "tools" in result:
        return result
    return {"tools": result if isinstance(result, list) else []}


def execute_tool(
    tool_slug: str,
    args: dict[str, Any],
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
) -> dict[str, Any]:
    base = _base_url(config, connector_name)
    headers = _headers(auth_env, config)
    body = _jsonrpc_request("tools/call", {"name": tool_slug, "arguments": args})
    return _post(base, body, headers, f"executing {tool_slug}")
