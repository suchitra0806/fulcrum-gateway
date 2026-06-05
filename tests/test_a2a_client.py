"""Tests for A2A (Agent2Agent Protocol) client."""


import httpx
import pytest

from ax_cli.a2a_client import A2AClient, A2ADiscoveryError, A2AError

AGENT_CARD = {
    "name": "Fulcrum Defense Platform",
    "description": "Multi-agent coordination platform",
    "version": "0.1.0",
    "url": "http://localhost:8001/a2a",
    "capabilities": {"streaming": True, "pushNotifications": False},
    "authentication": {"schemes": ["bearer"], "credentials": "http://localhost:8001/auth/exchange"},
    "skills": [
        {"id": "task-management", "name": "Task Management", "description": "Manage tasks"},
    ],
}

TASK_RESULT = {
    "id": "task-001",
    "status": {"state": "submitted", "timestamp": "2026-06-05T00:00:00Z"},
    "messages": [{"role": "user", "parts": [{"type": "text", "text": "hello"}]}],
    "artifacts": [],
    "metadata": {},
    "created_at": "2026-06-05T00:00:00Z",
    "updated_at": "2026-06-05T00:00:00Z",
}


def _rpc_response(result=None, error=None, req_id="test"):
    body = {"jsonrpc": "2.0", "id": req_id}
    if error:
        body["error"] = error
    else:
        body["result"] = result
    return httpx.Response(200, json=body)


def _card_response():
    return httpx.Response(200, json=AGENT_CARD)


class TestDiscover:
    def test_discover_success(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        card = client.discover()
        assert card["name"] == "Fulcrum Defense Platform"
        assert card["url"] == "http://localhost:8001/a2a"

    def test_discover_caches(self, monkeypatch):
        call_count = 0

        def mock_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return _card_response()

        monkeypatch.setattr(httpx, "get", mock_get)
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        client.discover()
        client.discover()
        assert call_count == 1

    def test_discover_force_refetch(self, monkeypatch):
        call_count = 0

        def mock_get(*a, **kw):
            nonlocal call_count
            call_count += 1
            return _card_response()

        monkeypatch.setattr(httpx, "get", mock_get)
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        client.discover()
        client.discover(force=True)
        assert call_count == 2

    def test_discover_404(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(404, text="Not Found"))
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        with pytest.raises(A2ADiscoveryError, match="404"):
            client.discover()

    def test_discover_auth_gated(self, monkeypatch):
        calls = []

        def mock_get(url, **kw):
            calls.append(kw)
            if "headers" in kw and "Authorization" in kw["headers"]:
                return _card_response()
            return httpx.Response(401, text="Unauthorized")

        monkeypatch.setattr(httpx, "get", mock_get)
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        card = client.discover()
        assert card["name"] == "Fulcrum Defense Platform"
        assert len(calls) == 2


class TestSendTask:
    def _mock_client(self, monkeypatch, rpc_result):
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: _rpc_response(result=rpc_result))
        return A2AClient("http://localhost:8001", raw_token="test-jwt")

    def test_send_task(self, monkeypatch):
        client = self._mock_client(monkeypatch, TASK_RESULT)
        task = client.send_task("hello agent")
        assert task["id"] == "task-001"
        assert task["status"]["state"] == "submitted"

    def test_send_task_with_metadata(self, monkeypatch):
        captured = {}

        def mock_post(url, **kw):
            captured.update(kw)
            return _rpc_response(result=TASK_RESULT)

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", mock_post)
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        client.send_task("test", metadata={"priority": "high"})
        body = captured["json"]
        assert body["params"]["metadata"] == {"priority": "high"}

    def test_send_task_auto_discovers(self, monkeypatch):
        discover_calls = 0

        def mock_get(*a, **kw):
            nonlocal discover_calls
            discover_calls += 1
            return _card_response()

        monkeypatch.setattr(httpx, "get", mock_get)
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: _rpc_response(result=TASK_RESULT))
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        client.send_task("hello")
        assert discover_calls == 1


class TestGetTask:
    def test_get_task(self, monkeypatch):
        task_with_history = {**TASK_RESULT, "history": [{"state": "submitted", "timestamp": "2026-06-05T00:00:00Z"}]}
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: _rpc_response(result=task_with_history))
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        task = client.get_task("task-001")
        assert task["id"] == "task-001"
        assert "history" in task


class TestCancelTask:
    def test_cancel_task(self, monkeypatch):
        canceled = {**TASK_RESULT, "status": {"state": "canceled", "timestamp": "2026-06-05T00:01:00Z"}}
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: _rpc_response(result=canceled))
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        task = client.cancel_task("task-001")
        assert task["status"]["state"] == "canceled"


class TestRPCErrors:
    def test_rpc_method_not_found(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **kw: _rpc_response(error={"code": -32601, "message": "Method not found"}),
        )
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        with pytest.raises(A2AError, match="-32601"):
            client.send_task("test")

    def test_rpc_invalid_params(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **kw: _rpc_response(error={"code": -32602, "message": "Missing param"}),
        )
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        with pytest.raises(A2AError, match="-32602"):
            client.send_task("test")

    def test_http_error(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(500, text="Internal Server Error"))
        client = A2AClient("http://localhost:8001", raw_token="test-jwt")
        with pytest.raises(A2AError, match="500"):
            client.send_task("test")


class TestAuth:
    def test_uses_raw_token(self, monkeypatch):
        captured = {}

        def mock_post(url, **kw):
            captured.update(kw)
            return _rpc_response(result=TASK_RESULT)

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", mock_post)
        client = A2AClient("http://localhost:8001", raw_token="my-jwt-token")
        client.send_task("test")
        assert captured["headers"]["Authorization"] == "Bearer my-jwt-token"

    def test_uses_exchanger(self, monkeypatch):
        captured = {}

        class FakeExchanger:
            def get_token(self, **kw):
                return "exchanged-jwt"

            def clear_cache(self):
                pass

        def mock_post(url, **kw):
            captured.update(kw)
            return _rpc_response(result=TASK_RESULT)

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", mock_post)
        client = A2AClient("http://localhost:8001", exchanger=FakeExchanger())
        client.send_task("test")
        assert captured["headers"]["Authorization"] == "Bearer exchanged-jwt"

    def test_401_retry_with_fresh_jwt(self, monkeypatch):
        call_count = 0

        class FakeExchanger:
            def get_token(self, **kw):
                return "fresh-jwt"

            def clear_cache(self):
                pass

        def mock_post(url, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(401, text="Unauthorized")
            return _rpc_response(result=TASK_RESULT)

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _card_response())
        monkeypatch.setattr(httpx, "post", mock_post)
        client = A2AClient("http://localhost:8001", exchanger=FakeExchanger())
        task = client.send_task("test")
        assert task["id"] == "task-001"
        assert call_count == 2

    def test_no_credentials_raises(self):
        client = A2AClient("http://localhost:8001")
        with pytest.raises(A2AError, match="No auth credentials"):
            client._get_jwt()
