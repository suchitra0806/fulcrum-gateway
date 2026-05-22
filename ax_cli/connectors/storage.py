"""Atomic read/write of the connectors registry (connectors.json)."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConnectorError, ConnectorNotFoundError
from .types import ConnectorRow

_REGISTRY_SCHEMA_VERSION = 1


def _default_registry() -> dict[str, Any]:
    return {"version": _REGISTRY_SCHEMA_VERSION, "connectors": []}


def _connectors_path() -> Path:
    from ax_cli.gateway import gateway_dir

    return gateway_dir() / "connectors.json"


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
    try:
        path.chmod(mode)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connectors_registry_path() -> Path:
    return _connectors_path()


def load_connectors_registry() -> dict[str, Any]:
    data = _read_json(_connectors_path(), default=_default_registry())
    data.setdefault("version", _REGISTRY_SCHEMA_VERSION)
    data.setdefault("connectors", [])
    return data


def save_connectors_registry(data: dict[str, Any]) -> Path:
    payload = dict(data)
    payload["saved_at"] = _now_iso()
    path = _connectors_path()
    _write_json(path, payload)
    return path


def list_connectors() -> list[ConnectorRow]:
    data = load_connectors_registry()
    return [ConnectorRow.from_dict(entry) for entry in data["connectors"] if isinstance(entry, dict)]


def find_connector(ref: str) -> ConnectorRow:
    ref_lower = ref.lower()
    matches: list[ConnectorRow] = []
    for row in list_connectors():
        if row.id == ref:
            return row
        if row.name.lower() == ref_lower:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        raise ConnectorError(
            f"Ambiguous connector name {ref!r} matches {len(matches)} entries ({ids}). Use the connector ID instead."
        )
    raise ConnectorNotFoundError(ref)


def add_connector(row: ConnectorRow) -> None:
    data = load_connectors_registry()
    data["connectors"].append(row.to_dict())
    save_connectors_registry(data)


def remove_connector(ref: str) -> ConnectorRow:
    data = load_connectors_registry()
    ref_lower = ref.lower()
    remaining: list[dict[str, Any]] = []
    removed: ConnectorRow | None = None
    for entry in data["connectors"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").lower()
        row_id = str(entry.get("id") or "")
        if name == ref_lower or row_id == ref:
            removed = ConnectorRow.from_dict(entry)
        else:
            remaining.append(entry)
    if removed is None:
        raise ConnectorNotFoundError(ref)
    data["connectors"] = remaining
    save_connectors_registry(data)
    return removed


def update_connector(ref: str, updates: dict[str, Any]) -> ConnectorRow:
    data = load_connectors_registry()
    ref_lower = ref.lower()
    found = False
    updated_row: ConnectorRow | None = None
    for entry in data["connectors"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").lower()
        row_id = str(entry.get("id") or "")
        if name == ref_lower or row_id == ref:
            entry.update(updates)
            entry.setdefault("metadata", {})["updated_at"] = _now_iso()
            updated_row = ConnectorRow.from_dict(entry)
            found = True
            break
    if not found:
        raise ConnectorNotFoundError(ref)
    save_connectors_registry(data)
    return updated_row  # type: ignore[return-value]
