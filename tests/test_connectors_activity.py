"""Tests for connector activity event emission and redaction."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ax_cli.connectors.activity import (
    new_invocation_id,
    record_connector_tool_completed,
    record_connector_tool_denied,
    record_connector_tool_failed,
    record_connector_tool_started,
    sanitize_activity_text,
)
from ax_cli.connectors.constants import MAX_ACTIVITY_ERROR_LEN
from ax_cli.connectors.errors import ConnectorPolicyError
from ax_cli.connectors.providers.dispatch import execute_tool
from ax_cli.connectors.types import ConnectorRow


@pytest.fixture()
def connector() -> ConnectorRow:
    return ConnectorRow.create("test-conn", "composio", config={"entity_id": "default"})


@pytest.fixture()
def tmp_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    activity_file = tmp_path / "activity.jsonl"
    monkeypatch.setattr("ax_cli.gateway.activity_log_path", lambda: activity_file)
    registry_data = {"gateway": {"gateway_id": "gw-test-123"}}
    monkeypatch.setattr("ax_cli.gateway.load_gateway_registry", lambda: registry_data)
    return activity_file


# ── Event shape ──────────────────────────────────────────────────────────────


class TestEventShape:
    def test_started_event(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_started(connector, "GITHUB_LIST_PRS")
        assert record["event"] == "connector_tool_started"
        assert record["tool_name"] == "composio/GITHUB_LIST_PRS"
        assert record["connector_name"] == "test-conn"
        assert record["connector_id"] == connector.id
        assert record["provider"] == "composio"
        assert "phase" in record

    def test_completed_event(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_completed(
            connector,
            "GITHUB_LIST_PRS",
            duration_ms=150,
        )
        assert record["event"] == "connector_tool_completed"
        assert record["duration_ms"] == 150
        assert record["tool_name"] == "composio/GITHUB_LIST_PRS"

    def test_failed_event(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_failed(
            connector,
            "GITHUB_LIST_PRS",
            error="Timeout",
            duration_ms=30000,
        )
        assert record["event"] == "connector_tool_failed"
        assert record["error"] == "Timeout"
        assert record["duration_ms"] == 30000

    def test_denied_event(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_denied(
            connector,
            "GITHUB_DELETE_REPO",
            policy_detail="matched denied pattern in ['GITHUB_DELETE_*']",
        )
        assert record["event"] == "connector_tool_denied"
        assert record["tool_name"] == "composio/GITHUB_DELETE_REPO"
        assert record["connector_name"] == "test-conn"
        assert record["policy_detail"] == "matched denied pattern in ['GITHUB_DELETE_*']"

    def test_extra_fields(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_started(
            connector,
            "SLACK_SEND_MSG",
            agent_name="my-agent",
        )
        assert record["agent_name"] == "my-agent"

    def test_identity_fields(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_started(
            connector,
            "GITHUB_LIST_PRS",
            agent_name="sentinel-agent",
            agent_id="abc-123",
        )
        assert record["agent_name"] == "sentinel-agent"
        assert record["agent_id"] == "abc-123"


# ── Redaction ────────────────────────────────────────────────────────────────


class TestRedaction:
    def test_no_auth_in_events(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_started(connector, "GITHUB_LIST_PRS")
        serialized = json.dumps(record)
        assert "api_key" not in serialized.lower()
        assert "ak_" not in serialized
        assert "secret" not in serialized.lower()

    def test_no_config_in_events(self, connector: ConnectorRow, tmp_activity: Path):
        record = record_connector_tool_completed(
            connector,
            "GITHUB_LIST_PRS",
            duration_ms=100,
        )
        serialized = json.dumps(record)
        assert "entity_id" not in serialized


# ── JSONL persistence ────────────────────────────────────────────────────────


class TestPersistence:
    def test_events_written_to_file(self, connector: ConnectorRow, tmp_activity: Path):
        record_connector_tool_started(connector, "TOOL_A")
        record_connector_tool_completed(connector, "TOOL_A", duration_ms=50)

        lines = tmp_activity.read_text().strip().splitlines()
        assert len(lines) == 2
        event1 = json.loads(lines[0])
        event2 = json.loads(lines[1])
        assert event1["event"] == "connector_tool_started"
        assert event2["event"] == "connector_tool_completed"

    def test_file_permissions(self, connector: ConnectorRow, tmp_activity: Path):
        record_connector_tool_started(connector, "TOOL_A")
        assert oct(tmp_activity.stat().st_mode & 0o777) == "0o600"


# ── Error sanitization ───────────────────────────────────────────────────────


class TestErrorSanitization:
    def test_truncates_long_error(self, connector: ConnectorRow, tmp_activity: Path):
        long_err = "x" * 5000
        record = record_connector_tool_failed(connector, "TOOL", error=long_err)
        assert record["error"].endswith("...(truncated)")
        assert len(record["error"]) == MAX_ACTIVITY_ERROR_LEN

    def test_redacts_api_key_in_error(self, connector: ConnectorRow, tmp_activity: Path):
        err = "Invalid API key: sk_live_abc123secret"
        record = record_connector_tool_failed(connector, "TOOL", error=err)
        assert "sk_live" not in record["error"]
        assert "<redacted>" in record["error"]

    def test_redacts_bearer_token(self, connector: ConnectorRow, tmp_activity: Path):
        err = "Unauthorized: Bearer eyJhbGciOiJIUzI1NiJ9.payload"
        record = record_connector_tool_failed(connector, "TOOL", error=err)
        assert "eyJhbGci" not in record["error"]
        assert "<redacted>" in record["error"]

    def test_sanitize_activity_text_none(self):
        assert sanitize_activity_text(None) is None

    def test_denied_policy_detail_sanitized(self, connector: ConnectorRow, tmp_activity: Path):
        detail = "token=super-secret-value"
        record = record_connector_tool_denied(connector, "TOOL", policy_detail=detail)
        assert "super-secret-value" not in record["policy_detail"]
        assert "<redacted>" in record["policy_detail"]


# ── Invocation correlation ───────────────────────────────────────────────────


class TestInvocationCorrelation:
    def test_new_invocation_id_is_uuid(self):
        iid = new_invocation_id()
        assert len(iid) == 36
        assert iid.count("-") == 4

    def test_invocation_id_persisted_on_extra_fields(self, connector: ConnectorRow, tmp_activity: Path):
        iid = new_invocation_id()
        record = record_connector_tool_started(connector, "TOOL", invocation_id=iid)
        assert record["invocation_id"] == iid

    def test_execute_tool_correlates_success_lifecycle(self, connector: ConnectorRow, tmp_activity: Path):
        with patch("ax_cli.connectors.providers.dispatch._get_adapter") as mock_get:
            mock_adapter = MagicMock()
            mock_adapter.execute_tool.return_value = {"ok": True}
            mock_get.return_value = mock_adapter

            execute_tool(connector, "GITHUB_LIST_PRS", {}, {})

        events = [json.loads(line) for line in tmp_activity.read_text().strip().splitlines()]
        assert len(events) == 2
        assert events[0]["event"] == "connector_tool_started"
        assert events[1]["event"] == "connector_tool_completed"
        assert events[0]["invocation_id"] == events[1]["invocation_id"]

    def test_execute_tool_correlates_failed_lifecycle(self, connector: ConnectorRow, tmp_activity: Path):
        err = "Provider failure: " + ("x" * 5000)
        with patch("ax_cli.connectors.providers.dispatch._get_adapter") as mock_get:
            mock_adapter = MagicMock()
            mock_adapter.execute_tool.side_effect = RuntimeError(err)
            mock_get.return_value = mock_adapter

            with pytest.raises(RuntimeError):
                execute_tool(connector, "GITHUB_LIST_PRS", {}, {})

        events = [json.loads(line) for line in tmp_activity.read_text().strip().splitlines()]
        assert len(events) == 2
        assert events[0]["event"] == "connector_tool_started"
        assert events[1]["event"] == "connector_tool_failed"
        assert events[0]["invocation_id"] == events[1]["invocation_id"]
        assert events[1]["error"].endswith("...(truncated)")
        assert len(events[1]["error"]) == MAX_ACTIVITY_ERROR_LEN

    def test_execute_tool_correlates_denied_lifecycle(self, connector: ConnectorRow, tmp_activity: Path):
        connector = ConnectorRow.create(
            "test-conn",
            "composio",
            config={"denied_tools": ["GITHUB_DELETE_*"]},
        )
        with pytest.raises(ConnectorPolicyError):
            execute_tool(connector, "GITHUB_DELETE_REPO", {}, {})

        events = [json.loads(line) for line in tmp_activity.read_text().strip().splitlines()]
        assert len(events) == 1
        assert events[0]["event"] == "connector_tool_denied"
        assert "invocation_id" in events[0]
