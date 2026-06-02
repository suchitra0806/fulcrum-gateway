"""Gateway directory resolution for connector files."""

from __future__ import annotations

from pathlib import Path


def connectors_dir() -> Path:
    from ax_cli.gateway import gateway_dir

    return gateway_dir() / "connectors"


def connectors_registry_path() -> Path:
    from ax_cli.gateway import gateway_dir

    return gateway_dir() / "connectors.json"


def auth_dir() -> Path:
    d = connectors_dir() / "auth"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def auth_path(connector_id: str) -> Path:
    return auth_dir() / f"{connector_id}.env"
