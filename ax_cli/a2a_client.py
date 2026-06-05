"""A2A (Agent2Agent Protocol) client for Fulcrum Defense Platform.

Discovers the platform's Agent Card, then uses JSON-RPC 2.0 to delegate
tasks and poll results.  Auth reuses the existing TokenExchanger (PAT→JWT).

Usage:
    from ax_cli.a2a_client import A2AClient

    a2a = A2AClient("http://localhost:8001", exchanger=exchanger)
    card = a2a.discover()
    task = a2a.send_task("Summarize today's alerts")
    result = a2a.get_task(task["id"])
"""

import json
import logging
import uuid

import httpx

logger = logging.getLogger(__name__)


class A2ADiscoveryError(Exception):
    """Raised when Agent Card discovery fails."""


class A2AError(Exception):
    """Raised on JSON-RPC errors from the A2A endpoint."""

    def __init__(self, code: int, message: str, data=None):
        self.code = code
        self.rpc_message = message
        self.data = data
        super().__init__(f"A2A error {code}: {message}")


class A2AClient:
    """Client for the A2A JSON-RPC 2.0 protocol.

    Parameters
    ----------
    base_url : str
        Platform base URL (e.g. "http://localhost:8001").
    exchanger : TokenExchanger | None
        If provided, used to get fresh JWTs for auth.  If None,
        ``raw_token`` is sent as a Bearer token directly.
    raw_token : str | None
        Fallback token when no exchanger is available.
    agent_id : str | None
        Agent ID for agent_access token class exchanges.
    timeout : float
        HTTP timeout in seconds (default 30).
    """

    def __init__(
        self,
        base_url: str,
        *,
        exchanger=None,
        raw_token: str | None = None,
        agent_id: str | None = None,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._exchanger = exchanger
        self._raw_token = raw_token
        self._agent_id = agent_id
        self._timeout = timeout
        self._card: dict | None = None
        self._a2a_url: str | None = None

    def _get_jwt(self) -> str:
        if self._exchanger:
            return self._exchanger.get_token(
                token_class="agent_access" if self._agent_id else "user_access",
                agent_id=self._agent_id,
            )
        if self._raw_token:
            return self._raw_token
        raise A2AError(-32000, "No auth credentials available")

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Content-Type": "application/json",
        }

    def discover(self, *, force: bool = False) -> dict:
        """Fetch the platform's Agent Card from /.well-known/agent.json.

        Caches the result for the session.  Pass ``force=True`` to refetch.

        Returns
        -------
        dict
            The Agent Card (name, url, capabilities, skills, authentication).

        Raises
        ------
        A2ADiscoveryError
            If the Agent Card endpoint is unreachable or returns an error.
        """
        if self._card and not force:
            return self._card

        url = f"{self._base_url}/.well-known/agent.json"
        try:
            r = httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise A2ADiscoveryError(f"Failed to reach Agent Card at {url}: {exc}") from exc

        if r.status_code == 401:
            try:
                r = httpx.get(url, headers=self._auth_headers(), timeout=self._timeout)
            except httpx.HTTPError as exc:
                raise A2ADiscoveryError(f"Agent Card auth failed: {exc}") from exc

        if r.status_code != 200:
            raise A2ADiscoveryError(f"Agent Card returned {r.status_code}: {r.text}")

        self._card = r.json()
        self._a2a_url = self._card.get("url", f"{self._base_url}/a2a")
        logger.info("Discovered agent: %s at %s", self._card.get("name"), self._a2a_url)
        return self._card

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC 2.0 request to the A2A endpoint."""
        if not self._a2a_url:
            self.discover()

        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
        }
        if params is not None:
            body["params"] = params

        r = httpx.post(
            self._a2a_url,
            json=body,
            headers=self._auth_headers(),
            timeout=self._timeout,
        )

        if r.status_code == 401:
            if self._exchanger:
                self._exchanger.clear_cache()
            r = httpx.post(
                self._a2a_url,
                json=body,
                headers=self._auth_headers(),
                timeout=self._timeout,
            )

        if r.status_code != 200:
            raise A2AError(-32000, f"HTTP {r.status_code}", r.text)

        data = r.json()
        if "error" in data:
            err = data["error"]
            raise A2AError(err.get("code", -32000), err.get("message", "Unknown error"), err.get("data"))

        return data.get("result", {})

    def send_task(
        self,
        text: str,
        *,
        role: str = "user",
        metadata: dict | None = None,
    ) -> dict:
        """Create a new A2A task with a text message.

        Parameters
        ----------
        text : str
            The task instruction / prompt.
        role : str
            Message role (default "user").
        metadata : dict | None
            Optional task metadata.

        Returns
        -------
        dict
            The created task object with id, status, messages, artifacts.
        """
        params = {
            "message": {
                "role": role,
                "parts": [{"type": "text", "text": text}],
            },
        }
        if metadata:
            params["metadata"] = metadata
        return self._rpc("tasks/send", params)

    def get_task(self, task_id: str) -> dict:
        """Retrieve a task by ID, including history."""
        return self._rpc("tasks/get", {"id": task_id})

    def cancel_task(self, task_id: str) -> dict:
        """Cancel a running task."""
        return self._rpc("tasks/cancel", {"id": task_id})

    def subscribe_task(
        self,
        text: str,
        *,
        role: str = "user",
        metadata: dict | None = None,
    ):
        """Create a task and stream status updates via SSE.

        Yields
        ------
        dict
            Parsed SSE event data dicts (task, status events).
        """
        if not self._a2a_url:
            self.discover()

        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tasks/sendSubscribe",
            "params": {
                "message": {
                    "role": role,
                    "parts": [{"type": "text", "text": text}],
                },
            },
        }
        if metadata:
            body["params"]["metadata"] = metadata

        with httpx.stream(
            "POST",
            self._a2a_url,
            json=body,
            headers=self._auth_headers(),
            timeout=httpx.Timeout(self._timeout, read=300.0),
        ) as response:
            if response.status_code != 200:
                raise A2AError(-32000, f"HTTP {response.status_code}")

            event_type = None
            data_buf = []

            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_buf.append(line[6:])
                elif line == "" and data_buf:
                    raw = "".join(data_buf)
                    data_buf.clear()
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    yield {"event": event_type, "data": parsed}
                    event_type = None
