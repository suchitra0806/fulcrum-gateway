"""Composio HTTP adapter — no SDK dependency.

All Composio API calls go through httpx. The adapter resolves settings
from the connector config dict and the managed auth env dict.

Targets the Composio v3 API (https://backend.composio.dev/api/v3).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..constants import CONNECT_TIMEOUT, DEFAULT_COMPOSIO_BASE_URL, READ_TIMEOUT
from ..errors import ConnectorAuthError, ConnectorProviderError, classify_provider_error

log = logging.getLogger("connectors.composio")

DEFAULT_BASE_URL = DEFAULT_COMPOSIO_BASE_URL


def _base_url(config: dict[str, Any]) -> str:
    url = str(config.get("composio_base_url") or DEFAULT_BASE_URL).rstrip("/")
    if url.endswith("/v2"):
        url = url[:-3] + "/v3"
    return url


def _api_key(auth_env: dict[str, str], connector_name: str) -> str:
    key = auth_env.get("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise ConnectorAuthError(
            connector_name,
            "COMPOSIO_API_KEY not found in managed auth. "
            "Run: ax gateway connectors auth write <ref> COMPOSIO_API_KEY=<key>",
        )
    return key


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=CONNECT_TIMEOUT)


def _handle_error_response(resp: httpx.Response, context: str) -> None:
    if resp.is_success:
        return
    content_type = resp.headers.get("content-type", "")
    is_json = "application/json" in content_type
    body: dict[str, str] = {}
    if is_json:
        try:
            body = resp.json()
        except Exception:
            pass
    if not body:
        text = resp.text[:200].strip()
        if "<html" in text.lower() or "<!doctype" in text.lower():
            detail = f"{context}: received HTML instead of JSON (CDN/proxy intercept?)"
        else:
            detail = f"{context}: {text}" if text else f"{context}: empty response"
    else:
        detail = f"{context}: {body.get('error') or body.get('message') or str(body)}"
    request_id = body.get("requestId") if body else None
    raise classify_provider_error(
        "composio",
        detail,
        status_code=resp.status_code,
        request_id=request_id,
    )


def search_tools(
    query: str,
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
    *,
    apps: str | None = None,
    limit: int = 10,
    cursor: str | None = None,
) -> dict[str, Any]:
    base = _base_url(config)
    key = _api_key(auth_env, connector_name)
    params: dict[str, Any] = {"query": query, "limit": limit}
    if apps:
        params["toolkit_slug"] = apps
    if cursor:
        params["cursor"] = cursor

    try:
        resp = httpx.get(
            f"{base}/tools",
            params=params,
            headers=_headers(key),
            timeout=_timeout(),
        )
    except httpx.TimeoutException as e:
        raise ConnectorProviderError("composio", f"Timeout searching tools: {e!r}") from e
    except httpx.HTTPError as e:
        raise ConnectorProviderError("composio", f"HTTP error searching tools: {e!r}") from e

    _handle_error_response(resp, "Search tools")
    return resp.json()


def list_apps(
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
) -> list[dict[str, Any]]:
    key = _api_key(auth_env, connector_name)
    base = _base_url(config)
    try:
        resp = httpx.get(
            f"{base}/connected_accounts",
            params={"statuses": "ACTIVE"},
            headers=_headers(key),
            timeout=_timeout(),
        )
    except httpx.HTTPError as e:
        raise ConnectorProviderError("composio", f"HTTP error listing apps: {e!r}") from e
    _handle_error_response(resp, "List connected apps")
    return resp.json().get("items", [])


def initiate_connection(
    app_name: str,
    entity_id: str,
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
) -> dict[str, Any]:
    key = _api_key(auth_env, connector_name)
    hdrs = _headers(key)
    timeout = _timeout()
    base = _base_url(config)

    # Look up the toolkit to get its supported auth scheme
    toolkit_resp = httpx.get(
        f"{base}/toolkits/{app_name}",
        headers=hdrs,
        timeout=timeout,
    )
    _handle_error_response(toolkit_resp, f"Look up toolkit {app_name}")
    toolkit_data = toolkit_resp.json()

    auth_schemes = toolkit_data.get("auth_schemes") or toolkit_data.get("composio_managed_auth_schemes") or []
    if auth_schemes:
        first = auth_schemes[0]
        auth_mode = first.get("auth_mode", "OAUTH2") if isinstance(first, dict) else str(first)
    else:
        auth_mode = "OAUTH2"

    # Find or create an auth_config for this toolkit
    configs_resp = httpx.get(
        f"{base}/auth_configs",
        params={"toolkit_slug": app_name},
        headers=hdrs,
        timeout=timeout,
    )
    _handle_error_response(configs_resp, "List auth configs")
    items = configs_resp.json().get("items", [])
    if not items:
        auth_config_body: dict[str, Any] = {
            "toolkit_slug": app_name,
            "name": f"ax-{app_name}",
            "auth_scheme": auth_mode,
        }
        if auth_mode == "OAUTH2":
            auth_config_body["use_composio_auth"] = True
        create_resp = httpx.post(
            f"{base}/auth_configs",
            json=auth_config_body,
            headers=hdrs,
            timeout=timeout,
        )
        _handle_error_response(create_resp, f"Create auth config for {app_name}")
        auth_config_id = create_resp.json().get("id")
        if not auth_config_id:
            raise ConnectorProviderError("composio", f"Failed to create auth config for {app_name!r}")
    else:
        auth_config_id = items[0]["id"]

    # Build the connected-account payload (v3 schema)
    account_body: dict[str, Any] = {
        "auth_config": {"id": auth_config_id},
        "connection": {
            "user_id": entity_id,
            "state": {"authScheme": auth_mode, "val": {}},
        },
    }
    if auth_mode == "API_KEY":
        app_key_var = f"{app_name.upper()}_API_KEY"
        app_key = auth_env.get(app_key_var, "").strip()
        if not app_key:
            raise ConnectorProviderError(
                "composio",
                f"{app_name!r} uses API-key auth. "
                f"Set the key first:\n"
                f"  ax gateway connectors auth write {connector_name} {app_key_var}=<your key>",
            )
        field_names = (
            [f.get("name") for f in auth_schemes[0].get("fields", []) if f.get("name")] if auth_schemes else []
        )
        if field_names:
            account_body["connection"]["state"]["val"] = {field_names[0]: app_key}
        else:
            account_body["connection"]["state"]["val"] = {"api_key": app_key}

    try:
        resp = httpx.post(
            f"{base}/connected_accounts",
            json=account_body,
            headers=hdrs,
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise ConnectorProviderError("composio", f"HTTP error initiating connection: {e!r}") from e
    _handle_error_response(resp, f"Initiate {app_name} connection")
    return resp.json()


def execute_tool(
    tool_slug: str,
    args: dict[str, Any],
    auth_env: dict[str, str],
    config: dict[str, Any],
    connector_name: str,
) -> dict[str, Any]:
    base = _base_url(config)
    key = _api_key(auth_env, connector_name)

    body: dict[str, Any] = {"arguments": args}

    connected_account_id = (
        auth_env.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "").strip()
        or str(config.get("connected_account_id") or "").strip()
    )
    if connected_account_id:
        body["connected_account_id"] = connected_account_id
    else:
        entity_id = auth_env.get("COMPOSIO_ENTITY_ID", "").strip() or str(config.get("entity_id") or "default").strip()
        body["entity_id"] = entity_id

    try:
        resp = httpx.post(
            f"{base}/tools/execute/{tool_slug}",
            json=body,
            headers=_headers(key),
            timeout=_timeout(),
        )
    except httpx.TimeoutException as e:
        raise ConnectorProviderError("composio", f"Timeout executing {tool_slug}: {e!r}") from e
    except httpx.HTTPError as e:
        raise ConnectorProviderError("composio", f"HTTP error executing {tool_slug}: {e!r}") from e

    _handle_error_response(resp, f"Execute {tool_slug}")
    return resp.json()
