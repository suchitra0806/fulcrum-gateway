"""Tests for connector registry CRUD, validation, and atomic write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ax_cli.connectors.errors import ConnectorError, ConnectorNotFoundError
from ax_cli.connectors.storage import (
    _write_json,
    add_connector,
    find_connector,
    list_connectors,
    load_connectors_registry,
    remove_connector,
    update_connector,
)
from ax_cli.connectors.types import ConnectorRow
from ax_cli.connectors.validation import validate_new_connector


@pytest.fixture()
def tmp_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect connector paths to a temp directory."""
    monkeypatch.setattr(
        "ax_cli.connectors.storage._connectors_path",
        lambda: tmp_path / "connectors.json",
    )
    auth_dir = tmp_path / "connectors" / "auth"

    def _fake_auth_dir() -> Path:
        auth_dir.mkdir(parents=True, exist_ok=True)
        return auth_dir

    monkeypatch.setattr("ax_cli.connectors.auth._auth_dir", _fake_auth_dir)
    return tmp_path


# ── ConnectorRow ──────────────────────────────────────────────────────────────


class TestConnectorRow:
    def test_create_generates_uuid_and_timestamps(self):
        row = ConnectorRow.create("test", "composio", managed_auth=True)
        assert row.id
        assert row.name == "test"
        assert row.provider == "composio"
        assert row.enabled is True
        assert row.auth_ref == f"connectors/auth/{row.id}.env"
        assert "created_at" in row.metadata
        assert "updated_at" in row.metadata

    def test_create_without_managed_auth(self):
        row = ConnectorRow.create("test", "composio")
        assert row.auth_ref is None

    def test_roundtrip_dict(self):
        row = ConnectorRow.create("test", "composio", config={"key": "val"})
        d = row.to_dict()
        row2 = ConnectorRow.from_dict(d)
        assert row2.id == row.id
        assert row2.name == row.name
        assert row2.config == {"key": "val"}

    def test_from_dict_defaults(self):
        row = ConnectorRow.from_dict({"id": "abc", "name": "x", "provider": "composio"})
        assert row.enabled is True
        assert row.config == {}
        assert row.metadata == {}
        assert row.auth_ref is None


# ── Storage CRUD ──────────────────────────────────────────────────────────────


class TestStorage:
    def test_load_empty_registry(self, tmp_gateway: Path):
        data = load_connectors_registry()
        assert data["version"] == 1
        assert data["connectors"] == []

    def test_add_and_list(self, tmp_gateway: Path):
        row = ConnectorRow.create("test-conn", "composio")
        add_connector(row)
        rows = list_connectors()
        assert len(rows) == 1
        assert rows[0].name == "test-conn"

    def test_find_by_name(self, tmp_gateway: Path):
        row = ConnectorRow.create("My-Connector", "composio")
        add_connector(row)
        found = find_connector("my-connector")
        assert found.id == row.id

    def test_find_by_id(self, tmp_gateway: Path):
        row = ConnectorRow.create("test", "composio")
        add_connector(row)
        found = find_connector(row.id)
        assert found.name == "test"

    def test_find_not_found(self, tmp_gateway: Path):
        with pytest.raises(ConnectorNotFoundError):
            find_connector("nonexistent")

    def test_remove(self, tmp_gateway: Path):
        row = ConnectorRow.create("to-remove", "composio")
        add_connector(row)
        removed = remove_connector("to-remove")
        assert removed.name == "to-remove"
        assert list_connectors() == []

    def test_remove_not_found(self, tmp_gateway: Path):
        with pytest.raises(ConnectorNotFoundError):
            remove_connector("nonexistent")

    def test_update_config(self, tmp_gateway: Path):
        row = ConnectorRow.create("test", "composio")
        add_connector(row)
        updated = update_connector("test", {"config": {"entity_id": "user1"}})
        assert updated.config["entity_id"] == "user1"
        assert "updated_at" in updated.metadata

    def test_update_not_found(self, tmp_gateway: Path):
        with pytest.raises(ConnectorNotFoundError):
            update_connector("nonexistent", {"enabled": False})

    def test_save_and_reload(self, tmp_gateway: Path):
        row = ConnectorRow.create("persistent", "composio")
        add_connector(row)
        data = load_connectors_registry()
        assert len(data["connectors"]) == 1
        assert "saved_at" in data

    def test_schema_evolution_setdefault(self, tmp_gateway: Path):
        path = tmp_gateway / "connectors.json"
        path.write_text(json.dumps({"version": 1}))
        data = load_connectors_registry()
        assert data["connectors"] == []


# ── Atomic write ──────────────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_write_json_creates_file(self, tmp_path: Path):
        path = tmp_path / "test.json"
        _write_json(path, {"key": "value"})
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["key"] == "value"

    def test_write_json_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "nested" / "dir" / "test.json"
        _write_json(path, {"ok": True})
        assert path.exists()

    def test_write_json_permissions(self, tmp_path: Path):
        path = tmp_path / "secret.json"
        _write_json(path, {"key": "val"}, mode=0o600)
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_write_json_sorted_and_indented(self, tmp_path: Path):
        path = tmp_path / "sorted.json"
        _write_json(path, {"b": 2, "a": 1})
        text = path.read_text()
        assert text.index('"a"') < text.index('"b"')


# ── Validation ────────────────────────────────────────────────────────────────


class TestValidation:
    def test_empty_name_rejected(self, tmp_gateway: Path):
        row = ConnectorRow(id="x", name="", provider="composio")
        with pytest.raises(ConnectorError, match="name must not be empty"):
            validate_new_connector(row)

    def test_empty_provider_rejected(self, tmp_gateway: Path):
        row = ConnectorRow(id="x", name="test", provider="")
        with pytest.raises(ConnectorError, match="provider must not be empty"):
            validate_new_connector(row)

    def test_unknown_provider_rejected(self, tmp_gateway: Path):
        row = ConnectorRow(id="x", name="test", provider="unknown_provider")
        with pytest.raises(ConnectorError, match="Unknown provider"):
            validate_new_connector(row)

    def test_duplicate_name_rejected(self, tmp_gateway: Path):
        row1 = ConnectorRow.create("dup-test", "composio")
        add_connector(row1)
        row2 = ConnectorRow.create("Dup-Test", "composio")
        with pytest.raises(ConnectorError, match="already exists"):
            validate_new_connector(row2)

    def test_valid_connector_passes(self, tmp_gateway: Path):
        row = ConnectorRow.create("valid-test", "composio")
        validate_new_connector(row)
