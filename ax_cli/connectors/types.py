"""Connector row dataclass — registry entry for an outbound tool provider."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import Any


@dataclasses.dataclass
class ConnectorRow:
    """A single connector registry entry.

    ``name`` is the operator-facing lookup key (case-insensitive).
    ``id`` is the stable UUID used for auth file paths and cross-references.
    """

    id: str
    name: str
    provider: str
    enabled: bool = True
    auth_ref: str | None = None
    config: dict[str, Any] = dataclasses.field(default_factory=dict)
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectorRow:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            provider=str(data.get("provider") or ""),
            enabled=bool(data.get("enabled", True)),
            auth_ref=data.get("auth_ref"),
            config=dict(data.get("config") or {}),
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def create(
        cls,
        name: str,
        provider: str,
        *,
        managed_auth: bool = False,
        config: dict[str, Any] | None = None,
    ) -> ConnectorRow:
        row_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        auth_ref = f"connectors/auth/{row_id}.env" if managed_auth else None
        return cls(
            id=row_id,
            name=name,
            provider=provider,
            enabled=True,
            auth_ref=auth_ref,
            config=dict(config or {}),
            metadata={"created_at": now, "updated_at": now},
        )
