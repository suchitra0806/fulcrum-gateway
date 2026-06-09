"""Gateway registry entry CRUD, placement events, and env sanitization.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .gateway_constants import (
    DEFAULT_HANDLER_TIMEOUT_SECONDS,
    ENV_DENYLIST,
    GATEWAY_EVENT_PREFIX,
    MIN_HANDLER_TIMEOUT_SECONDS,
)


class GatewayRuntimeTimeoutError(TimeoutError):
    """Raised when a managed runtime exceeds its per-message timeout."""

    def __init__(self, timeout_seconds: int, *, runtime_type: str | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.runtime_type = runtime_type
        label = f" {runtime_type}" if runtime_type else ""
        super().__init__(f"Gateway{label} runtime timed out after {timeout_seconds}s.")


def _apply_placement_event(
    entry: dict[str, Any],
    event_data: dict[str, Any],
    *,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Apply an ``agent.placement.changed`` event to the local Gateway registry.

    Returns a result dict describing what happened:

        {
          "applied": bool,
          "reason": str | None,           # if not applied
          "previous_space": str | None,
          "new_space": str | None,
          "placement_state": str | None,
          "policy_revision": int | None,
        }

    Spec: ``specs/GATEWAY-PLACEMENT-POLICY-001/spec.md``. The event payload
    follows the placement record fields (lines 32-46): ``agent_id``,
    ``current_space``, ``placement_state``, ``policy_revision``, etc.

    Idempotent: events for an agent we don't manage, or where ``current_space``
    already matches, are no-ops (``applied: False``, ``reason`` set).
    """
    event_agent_id = str(event_data.get("agent_id") or "").strip()
    entry_agent_id = str(entry.get("agent_id") or "").strip()
    if event_agent_id and entry_agent_id and event_agent_id != entry_agent_id:
        return {"applied": False, "reason": "agent_id_mismatch"}

    raw_current_space = event_data.get("current_space")
    if isinstance(raw_current_space, dict):
        new_space = str(raw_current_space.get("space_id") or raw_current_space.get("id") or "").strip()
    else:
        new_space = str(raw_current_space or event_data.get("space_id") or "").strip()
    if not new_space:
        return {"applied": False, "reason": "missing_current_space"}

    previous_space = str(entry.get("space_id") or "").strip() or None
    placement_state = str(event_data.get("placement_state") or "applied").strip() or "applied"
    policy_revision = event_data.get("policy_revision")
    try:
        policy_revision_int = int(policy_revision) if policy_revision is not None else None
    except (TypeError, ValueError):
        policy_revision_int = None

    # Already in sync — ack-without-apply unless older placement metadata would
    # still route sends through a stale active/default space.
    placement_stale = any(
        str(entry.get(field) or "").strip() not in {"", new_space} for field in ("active_space_id", "default_space_id")
    )
    if previous_space == new_space:
        existing_rev = entry.get("placement_revision")
        if not placement_stale and (
            policy_revision_int is None or (existing_rev is not None and int(existing_rev) >= policy_revision_int)
        ):
            return {
                "applied": False,
                "reason": "already_at_target",
                "previous_space": previous_space,
                "new_space": new_space,
                "placement_state": placement_state,
                "policy_revision": policy_revision_int,
            }

    # Persist to local registry
    registry = load_gateway_registry()
    name = agent_name or str(entry.get("name") or "")
    target_entry = find_agent_entry(registry, name)
    if target_entry is None:
        return {
            "applied": False,
            "reason": "agent_not_in_registry",
            "previous_space": previous_space,
            "new_space": new_space,
        }
    space_name = (
        str(
            event_data.get("current_space_name") or event_data.get("space_name") or event_data.get("name") or ""
        ).strip()
        or None
    )
    if isinstance(event_data.get("current_space"), dict):
        current_space = event_data["current_space"]
        space_name = (
            str(current_space.get("name") or current_space.get("space_name") or space_name or "").strip() or None
        )
    apply_entry_current_space(target_entry, new_space, space_name=space_name)
    target_entry["placement_state"] = placement_state
    if policy_revision_int is not None:
        target_entry["placement_revision"] = policy_revision_int
    if "current_space_set_by" in event_data:
        target_entry["placement_source"] = str(event_data["current_space_set_by"])
    ensure_gateway_identity_binding(registry, target_entry)
    save_gateway_registry(registry)

    # Mirror to caller's `entry` so subsequent calls in same loop see the new value
    apply_entry_current_space(entry, new_space, space_name=space_name)
    entry["placement_state"] = placement_state
    if policy_revision_int is not None:
        entry["placement_revision"] = policy_revision_int

    return {
        "applied": True,
        "previous_space": previous_space,
        "new_space": new_space,
        "placement_state": placement_state,
        "policy_revision": policy_revision_int,
    }


def _post_placement_ack(
    client: Any,
    entry: dict[str, Any],
    *,
    placement_state: str,
    policy_revision: int | None = None,
    runtime_pid: int | None = None,
) -> bool:
    """Best-effort PATCH /api/v1/agents/{id}/placement/ack — backend task 31adc3a4.

    Returns True on success, False otherwise. 404 is the expected failure mode
    until backend ships the endpoint; logged but not fatal.
    """
    agent_id = str(entry.get("agent_id") or "").strip()
    if not agent_id:
        return False
    body: dict[str, Any] = {"placement_state": placement_state}
    if policy_revision is not None:
        body["policy_revision"] = int(policy_revision)
    if runtime_pid is not None:
        body["runtime_pid"] = int(runtime_pid)
    body["ack_at"] = _now_iso()
    try:
        response = client._http.patch(f"/api/v1/agents/{agent_id}/placement/ack", json=body)
    except Exception:  # noqa: BLE001
        return False
    if response.status_code == 404:
        # Endpoint not yet shipped (31adc3a4 pending) — silent no-op
        return False
    return 200 <= response.status_code < 300


def sanitize_exec_env(prompt: str, entry: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_DENYLIST}
    agent_id = str(entry.get("agent_id") or "")
    agent_name = str(entry.get("name") or "")
    env["AX_GATEWAY_AGENT_ID"] = agent_id
    env["AX_GATEWAY_AGENT_NAME"] = agent_name
    env["AX_AGENT_ID"] = agent_id
    env["AX_AGENT_NAME"] = agent_name
    env["AX_GATEWAY_RUNTIME_TYPE"] = str(entry.get("runtime_type") or "")
    env["AX_MENTION_CONTENT"] = prompt
    if str(entry.get("token_file") or "").strip():
        # Validate the bound credential without placing the secret in the child
        # process environment. Bridges read AX_TOKEN_FILE when they need to call aX.
        load_gateway_managed_agent_token(entry)
        # Inject the resolved absolute path — the child resolves AX_TOKEN_FILE
        # against its own cwd, so the stored relative form would break (#89).
        env["AX_TOKEN_FILE"] = str(resolve_agent_token_file(entry))
    base_url = str(entry.get("base_url") or "").strip()
    if base_url:
        env["AX_BASE_URL"] = base_url
    space_id = str(entry.get("space_id") or "").strip()
    if space_id:
        env["AX_SPACE_ID"] = space_id
    ollama_model = str(entry.get("model") or "").strip()
    if ollama_model:
        env["OLLAMA_MODEL"] = ollama_model
    hermes_repo_path = str(entry.get("hermes_repo_path") or "").strip()
    if hermes_repo_path:
        env["HERMES_REPO_PATH"] = hermes_repo_path
    connector_ref = str(entry.get("connector_ref") or "").strip()
    if connector_ref:
        env["AX_GATEWAY_CONNECTOR_REF"] = connector_ref
    return env


def _parse_gateway_exec_event(raw_line: str) -> dict[str, Any] | None:
    line = raw_line.strip()
    if not line.startswith(GATEWAY_EVENT_PREFIX):
        return None
    payload = line[len(GATEWAY_EVENT_PREFIX) :].strip()
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _hash_tool_arguments(arguments: dict[str, Any] | None) -> str | None:
    if not arguments:
        return None
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_timeout_seconds(entry: dict[str, Any]) -> int:
    """Resolve a safe per-message runtime timeout for Gateway-managed agents."""
    raw_value = entry.get("timeout_seconds")
    if raw_value is None:
        raw_value = entry.get("timeout")
    try:
        timeout = int(raw_value) if raw_value is not None else DEFAULT_HANDLER_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = DEFAULT_HANDLER_TIMEOUT_SECONDS
    return max(MIN_HANDLER_TIMEOUT_SECONDS, timeout)


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_health import _now_iso  # noqa: E402
from .gateway_identity import apply_entry_current_space, ensure_gateway_identity_binding  # noqa: E402
from .gateway_storage import (  # noqa: E402
    find_agent_entry,
    load_gateway_managed_agent_token,
    load_gateway_registry,
    resolve_agent_token_file,
    save_gateway_registry,
)
