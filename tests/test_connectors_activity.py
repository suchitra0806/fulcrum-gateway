"""Tests for connector activity event emission and redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ax_cli.connectors.activity import (
    record_connector_tool_completed,
    record_connector_tool_denied,
    record_connector_tool_failed,
    record_connector_tool_started,
)
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
