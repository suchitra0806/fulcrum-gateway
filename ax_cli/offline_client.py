"""Offline stub client for platform-free development and testing.

Activated by setting AX_OFFLINE=1. Every method returns a structurally correct
response so commands complete normally without a running platform. Useful for
developing and testing gateway-side logic when platform access is unavailable.
"""

from __future__ import annotations

import sys
import threading
from uuid import uuid4

_OFFLINE_SPACE_ID = "00000000-0000-0000-0000-000000000001"
_OFFLINE_USER_ID = "00000000-0000-0000-0000-000000000002"

_warned = False


def _warn_once() -> None:
    global _warned
    if not _warned:
        _warned = True
        sys.stderr.write("\033[33m[offline mode — platform calls are stubbed]\033[0m\n")


class _OfflineHTTPResponse:
    """Minimal httpx.Response look-alike used by helpers that reach into client._http directly."""

    def __init__(self, status_code: int = 200, data: object = None):
        import json as _json

        self.status_code = status_code
        self._data = data if data is not None else {}
        self.headers: dict = {"content-type": "application/json"}
        self.text = _json.dumps(self._data)

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._data


class _OfflineHTTPMock:
    """Stand-in for AxClient._http in offline mode.

    Only needs to cover the narrow set of calls made by helpers that bypass
    the public AxClient API (e.g. _find_agent_in_space, _create_agent_in_space
    in bootstrap.py).
    """

    def get(self, path: str, **kwargs) -> _OfflineHTTPResponse:
        if "/agents" in path:
            return _OfflineHTTPResponse(200, {"agents": []})
        return _OfflineHTTPResponse(200, {})

    def post(self, path: str, **kwargs) -> _OfflineHTTPResponse:
        body = kwargs.get("json") or {}
        if "/agents" in path:
            return _OfflineHTTPResponse(201, {"id": str(uuid4()), "name": str(body.get("name") or "")})
        if "/keys" in path or "/credentials" in path:
            return _OfflineHTTPResponse(201, {"id": str(uuid4()), "token": f"axp_u_offline_{str(uuid4())[:8]}"})
        return _OfflineHTTPResponse(201, {"id": str(uuid4())})

    def put(self, path: str, **kwargs) -> _OfflineHTTPResponse:
        return _OfflineHTTPResponse(200, {})

    def patch(self, path: str, **kwargs) -> _OfflineHTTPResponse:
        return _OfflineHTTPResponse(200, {})

    def delete(self, path: str, **kwargs) -> _OfflineHTTPResponse:
        return _OfflineHTTPResponse(204, {})

    def close(self) -> None:
        pass


class _EmptySSE:
    """Context manager returned by connect_sse() in offline mode.

    Blocks iter_lines() until close() is called so the SSE listener thread
    stays parked without spinning. The daemon unblocks it by calling
    client.close() during shutdown.
    """

    status_code = 200

    def __init__(self, stop: threading.Event):
        self._stop = stop

    def iter_lines(self):
        self._stop.wait()
        return
        yield  # makes this a generator so StopIteration ends the for loop cleanly

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._stop.set()


class OfflineAxClient:
    """Drop-in replacement for AxClient that never touches the platform."""

    def __init__(
        self,
        *,
        base_url: str = "",
        token: str = "offline",
        agent_name: str | None = None,
        agent_id: str | None = None,
    ):
        import os as _os
        _warn_once()
        self.base_url = base_url or _os.environ.get("AX_LOCAL_GATEWAY_URL") or "http://localhost:8765"
        self.token = token
        self.agent_name = agent_name
        self.agent_id = agent_id
        self._use_exchange = False
        self._base_headers: dict = {}
        self._sse_stop = threading.Event()
        self._http = _OfflineHTTPMock()

    def _parse_json(self, r: _OfflineHTTPResponse) -> object:
        return r.json()

    # --- Identity ---

    def whoami(self) -> dict:
        return {
            "id": _OFFLINE_USER_ID,
            "email": "offline@localhost",
            "name": "Offline User",
            "space_id": _OFFLINE_SPACE_ID,
            "resolved_space_id": _OFFLINE_SPACE_ID,
        }

    # --- Spaces ---

    def list_spaces(self) -> list[dict]:
        return [{"id": _OFFLINE_SPACE_ID, "name": "offline", "slug": "offline"}]

    def get_space(self, space_id: str) -> dict:
        return {"id": space_id, "name": "offline"}

    def create_space(self, name: str, *, description: str | None = None, visibility: str = "private") -> dict:
        return {"id": str(uuid4()), "name": name, "visibility": visibility}

    def list_space_members(self, space_id: str) -> list[dict]:
        return []

    # --- Messages ---

    def send_heartbeat(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        note: str | None = None,
        cadence_seconds: int | None = None,
    ) -> dict:
        return {"status": "ok"}

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        agent_id: str | None = None,
        channel: str = "main",
        parent_id: str | None = None,
        attachments: list | None = None,
        metadata: dict | None = None,
        message_type: str = "text",
    ) -> dict:
        import httpx
        body: dict = {"content": content, "space_id": space_id, "channel": channel}
        if parent_id:
            body["parent_id"] = parent_id
        if self.agent_name:
            body["author"] = self.agent_name
        try:
            r = httpx.post(f"{self.base_url}/api/v1/messages", json=body, timeout=5.0)
            if r.status_code in (200, 201):
                data = r.json()
                return data.get("message", data)
        except Exception:
            pass
        return {"id": str(uuid4()), "content": content, "space_id": space_id, "channel": channel}

    def set_agent_processing_status(self, message_id: str, status: str, **kwargs) -> dict:
        return {"status": "ok"}

    def record_tool_call(self, *, tool_name: str, tool_call_id: str, **kwargs) -> dict:
        return {"id": str(uuid4()), "tool_name": tool_name, "status": "success"}

    def upload_file(self, file_path: str, *, space_id: str | None = None) -> dict:
        return {"id": str(uuid4()), "filename": file_path}

    def list_messages(
        self,
        limit: int = 20,
        channel: str = "main",
        *,
        space_id: str | None = None,
        agent_id: str | None = None,
        unread_only: bool = False,
        mark_read: bool = False,
    ) -> dict:
        return {"messages": [], "total": 0}

    def mark_message_read(self, message_id: str) -> dict:
        return {"ok": True}

    def mark_all_messages_read(self) -> dict:
        return {"ok": True}

    def get_message(self, message_id: str) -> dict:
        return {"id": message_id, "content": "[offline]"}

    def edit_message(self, message_id: str, content: str) -> dict:
        return {"id": message_id, "content": content}

    def delete_message(self, message_id: str) -> int:
        return 204

    def add_reaction(self, message_id: str, emoji: str) -> dict:
        return {"ok": True}

    def list_replies(self, message_id: str) -> dict:
        return {"replies": [], "total": 0}

    # --- Tasks ---

    def create_task(
        self,
        space_id: str,
        title: str,
        *,
        description: str | None = None,
        priority: str = "medium",
        assignee_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        return {"id": str(uuid4()), "title": title, "space_id": space_id, "priority": priority}

    def create_task_auth_contract(
        self,
        title: str,
        *,
        description: str | None = None,
        priority: str = "medium",
        requirements: dict | None = None,
        deadline: str | None = None,
        expected_space_id: str | None = None,
    ) -> dict:
        sid = expected_space_id or _OFFLINE_SPACE_ID
        return {"id": str(uuid4()), "title": title, "space_id": sid, "priority": priority}

    def list_tasks(self, limit: int = 20, *, agent_id: str | None = None, space_id: str | None = None) -> list:
        return []

    def get_task(self, task_id: str) -> dict:
        return {"id": task_id, "title": "[offline]", "status": "open"}

    def update_task(self, task_id: str, **fields) -> dict:
        return {"id": task_id, **fields}

    # --- Agents ---

    def create_agent(self, name: str, **kwargs) -> dict:
        return {"id": str(uuid4()), "name": name, **{k: v for k, v in kwargs.items() if v is not None}}

    def list_agents(self, *, space_id: str | None = None, limit: int | None = None) -> list:
        return []

    def get_agent(self, identifier: str) -> dict:
        return {"id": identifier, "name": identifier}

    def update_agent(self, identifier: str, **fields) -> dict:
        return {"id": identifier, **fields}

    def delete_agent(self, identifier: str) -> dict:
        return {"ok": True}

    def get_agents_presence(self) -> dict:
        return {}

    def list_agents_availability(
        self,
        *,
        space_id: str | None = None,
        connection_path: str | None = None,
        badge_state: str | None = None,
        filter_: str | None = None,
    ) -> list:
        return []

    def get_agent_presence(self, agent_id_or_name: str, *, space_id: str | None = None) -> dict:
        return {"status": "offline", "agent_id": agent_id_or_name}

    def get_agent_placement(self, agent_id_or_name: str) -> dict:
        return {"agent_id": agent_id_or_name, "space_id": None, "pinned": False, "_record": {}}

    def set_agent_placement(self, agent_id_or_name: str, *, space_id: str, pinned: bool = False) -> dict:
        return {"ok": True}

    def get_agent_tools(self, space_id: str, agent_id: str) -> dict:
        return {"agent_id": agent_id, "enabled_tools": None}

    # --- Context ---

    def set_context(self, space_id: str, key: str, value: str, *, ttl: int | None = None) -> dict:
        return {"key": key, "value": value, "space_id": space_id}

    def get_context(self, key: str, *, space_id: str | None = None) -> dict:
        return {"key": key, "value": None}

    def list_context(self, prefix: str | None = None, *, space_id: str | None = None) -> dict:
        return {"items": []}

    def delete_context(self, key: str, *, space_id: str | None = None) -> int:
        return 204

    def promote_context(self, space_id: str, key: str, *, artifact_type: str = "RESEARCH", agent_id: str | None = None) -> dict:
        return {"ok": True}

    # --- Search ---

    def search_messages(self, query: str, limit: int = 20, *, agent_id: str | None = None) -> dict:
        return {"results": [], "total": 0}

    # --- Keys ---

    def create_key(
        self,
        name: str,
        *,
        allowed_agent_ids: list | None = None,
        bound_agent_id: str | None = None,
        audience: str | None = None,
        scopes: list | None = None,
        space_id: str | None = None,
    ) -> dict:
        return {"id": str(uuid4()), "name": name, "token": f"axp_u_offline_{str(uuid4())[:8]}"}

    def list_keys(self) -> list[dict]:
        return []

    def revoke_key(self, credential_id: str) -> int:
        return 204

    def rotate_key(self, credential_id: str) -> dict:
        return {"id": credential_id, "token": f"axp_u_offline_{str(uuid4())[:8]}"}

    # --- SSE ---

    def connect_sse(self, *, space_id: str | None = None, timeout=None):
        import httpx
        token = f"offline-{self.agent_name}" if self.agent_name else "offline-unknown"
        params: dict = {"token": token}
        if space_id:
            params["space_id"] = space_id
        client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0))
        return client.stream("GET", "/api/v1/sse/messages", params=params)

    # --- Management ---

    def mgmt_create_agent(self, name: str, **kwargs) -> dict:
        return self.create_agent(name, **kwargs)

    def mgmt_list_agents(self) -> list[dict]:
        return []

    def mgmt_update_agent(self, agent_id: str, **fields) -> dict:
        return {"id": agent_id, **fields}

    def mgmt_issue_agent_pat(
        self, agent_id: str, *, name: str | None = None, expires_in_days: int = 90, audience: str = "cli"
    ) -> dict:
        return {"id": str(uuid4()), "agent_id": agent_id, "token": f"axp_a_offline_{str(uuid4())[:8]}"}

    def mgmt_issue_enrollment(self, *, name: str | None = None, expires_in_hours: int = 1, audience: str = "cli") -> dict:
        return {"code": f"offline-enrollment-{str(uuid4())[:8]}"}

    def mgmt_revoke_credential(self, credential_id: str) -> dict:
        return {"ok": True}

    def mgmt_list_credentials(self) -> list[dict]:
        return []

    def close(self):
        self._sse_stop.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
