"""ax gateway — local Gateway control plane (thin orchestrator).

The implementation lives in focused ``gateway_*`` modules (issue #28 Phase 1).
This module wires them together: it imports :data:`app` (with every sub-app
already nested) from :mod:`gateway_app`, then imports each concern module for
its Typer-command registration side effects so ``ax gateway ...`` exposes the
full command surface. ``main.py`` consumes ``gateway.app`` and
``gateway.GatewaySessionRejectedError`` from here.
"""

from __future__ import annotations

# Import concern modules so their @app/@<subapp>.command decorators register on
# the shared Typer apps. Order is irrelevant (commands attach on import).
from . import (  # noqa: F401  (imported for command-registration side effects)
    gateway_agents,
    gateway_audit,
    gateway_auth,
    gateway_connectors,
    gateway_daemon_cmd,
    gateway_diagnostics,
    gateway_lifecycle,
    gateway_local,
    gateway_messaging,
    gateway_runtime_cmd,
    gateway_session,
    gateway_spaces,
    gateway_ui,
)
from .gateway_app import app
from .gateway_auth import GatewaySessionRejectedError

__all__ = ["GatewaySessionRejectedError", "app"]
