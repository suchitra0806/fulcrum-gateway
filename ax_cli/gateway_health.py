"""Gateway runtime-health annotation and scoring.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from .gateway_constants import (
    _CONTROLLED_ACTIVE_SPACE_SOURCES,
    _CONTROLLED_APPROVAL_STATES,
    _CONTROLLED_ASSET_CLASSES,
    _CONTROLLED_ATTESTATION_STATES,
    _CONTROLLED_CONFIDENCE,
    _CONTROLLED_CONFIDENCE_REASONS,
    _CONTROLLED_ENVIRONMENT_STATUSES,
    _CONTROLLED_IDENTITY_STATUSES,
    _CONTROLLED_INTAKE_MODELS,
    _CONTROLLED_LIVENESS,
    _CONTROLLED_MODES,
    _CONTROLLED_PRESENCE,
    _CONTROLLED_REACHABILITY,
    _CONTROLLED_REPLY,
    _CONTROLLED_SPACE_STATUSES,
    _CONTROLLED_TELEMETRY_SHAPES,
    _CONTROLLED_WORK_STATES,
    _normalized_controlled,
    _normalized_optional_controlled,
    infer_asset_descriptor,
)


def _pid_is_alive(pid: object) -> bool:
    try:
        pid_int = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except PermissionError:
        # The local OS can deny signal checks even when the child is still
        # visible to the user. Treat permission-denied as "alive enough" for
        # a UI-managed attached session.
        return True
    except OSError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(value: object, *, now: datetime | None = None) -> int | None:
    parsed = _parse_iso8601(value)
    if parsed is None:
        return None
    current = now or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = current - parsed.astimezone(timezone.utc)
    return max(0, int(delta.total_seconds()))


def annotate_runtime_health(
    snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    registry: dict[str, Any] | None = None,
    explicit_space_id: str | None = None,
) -> dict[str, Any]:
    enriched = dict(snapshot)
    resolved_registry = registry
    if resolved_registry is None:
        try:
            resolved_registry = load_gateway_registry()
        except Exception:
            resolved_registry = None
    if resolved_registry and (resolved_registry.get("identity_bindings") or enriched.get("identity_binding_id")):
        identity_space = evaluate_identity_space_binding(
            resolved_registry, enriched, explicit_space_id=explicit_space_id
        )
        enriched.update(identity_space)
    last_seen_age = _age_seconds(enriched.get("last_seen_at"), now=now)
    last_error_age = _age_seconds(enriched.get("last_listener_error_at"), now=now)
    if last_seen_age is not None:
        enriched["last_seen_age_seconds"] = last_seen_age
    if last_error_age is not None:
        enriched["last_listener_error_age_seconds"] = last_error_age

    profile = infer_operator_profile(enriched)
    asset_descriptor = infer_asset_descriptor(enriched, operator_profile=profile)
    state = str(enriched.get("effective_state") or "stopped").lower()
    raw_state = state
    attached_session_alive = False
    liveness, connected = _derive_liveness(enriched, raw_state=state, last_seen_age=last_seen_age)
    desired_stopped = str(enriched.get("desired_state") or "").lower() == "stopped"
    if not desired_stopped and _external_runtime_connected(enriched, last_seen_age=last_seen_age):
        liveness = "connected"
        connected = True
        state = "running"
        runtime_kind = str(enriched.get("external_runtime_kind") or "external runtime").strip()
        enriched["local_attach_state"] = "external_connected"
        enriched["local_attach_detail"] = f"{runtime_kind} announced a live local connection."
    if profile["activation"] == "attach_only":
        local_pid_alive = str(enriched.get("desired_state") or "").lower() == "running" and _pid_is_alive(
            enriched.get("attached_session_pid")
        )
        manual_attached = (
            str(enriched.get("desired_state") or "").lower() == "running"
            and str(enriched.get("manual_attach_state") or "").lower() == "attached"
        )
        if local_pid_alive or manual_attached:
            attached_session_alive = True
            if liveness in {"stale", "offline"}:
                # Don't restore to connected if the channel's SSE subscription is
                # explicitly broken — a live process with a dead SSE can't receive messages.
                if enriched.get("sse_connected") is not False:
                    liveness = "connected"
                    connected = True
                    state = "running"
            if manual_attached and not local_pid_alive:
                enriched["local_attach_state"] = "manual_attached"
                enriched["local_attach_detail"] = "Operator marked this Claude Code session as manually attached."
            else:
                enriched["local_attach_state"] = "connected"
                enriched["local_attach_detail"] = "Gateway-managed Claude Code session is running locally."
        elif str(enriched.get("local_attach_state") or "").lower() == "connected":
            enriched["local_attach_state"] = "stopped"
            enriched["local_attach_detail"] = "Claude Code is not running locally."
    if liveness == "stale" and raw_state == "running":
        state = "stale"
    elif liveness == "setup_error":
        state = "error"
    elif liveness == "offline" and state not in {"stopped", "error"}:
        state = "stopped"

    work_state = _derive_work_state(enriched, liveness=liveness, profile=profile)
    mode = _derive_mode(profile)
    presence = _derive_presence(mode=mode, liveness=liveness, work_state=work_state)
    reply = _derive_reply(profile["reply_mode"])
    reachability = _derive_reachability(
        snapshot=enriched, mode=mode, liveness=liveness, activation=profile["activation"]
    )
    confidence, confidence_reason, confidence_detail = _derive_confidence(
        enriched,
        mode=mode,
        liveness=liveness,
        reachability=reachability,
    )

    enriched.update(profile)
    enriched["asset_class"] = _normalized_controlled(
        asset_descriptor["asset_class"], _CONTROLLED_ASSET_CLASSES, fallback="interactive_agent"
    )
    enriched["intake_model"] = _normalized_controlled(
        asset_descriptor["intake_model"], _CONTROLLED_INTAKE_MODELS, fallback="launch_on_send"
    )
    if asset_descriptor.get("worker_model"):
        enriched["worker_model"] = asset_descriptor["worker_model"]
    enriched["trigger_sources"] = list(asset_descriptor.get("trigger_sources") or [])
    enriched["return_paths"] = list(asset_descriptor.get("return_paths") or [])
    enriched["telemetry_shape"] = _normalized_controlled(
        asset_descriptor.get("telemetry_shape"),
        _CONTROLLED_TELEMETRY_SHAPES,
        fallback="basic",
    )
    enriched["asset_type_label"] = str(asset_descriptor.get("type_label") or "Connected Asset")
    enriched["output_label"] = str(asset_descriptor.get("output_label") or "Reply")
    enriched["tags"] = list(asset_descriptor.get("tags") or [])
    enriched["capabilities"] = list(asset_descriptor.get("capabilities") or [])
    enriched["constraints"] = list(asset_descriptor.get("constraints") or [])
    enriched["asset_descriptor"] = asset_descriptor
    enriched["effective_state"] = state
    enriched["connected"] = connected
    if attached_session_alive:
        enriched["last_seen_age_seconds"] = 0
    enriched["liveness"] = _normalized_controlled(liveness, _CONTROLLED_LIVENESS, fallback="offline")
    enriched["work_state"] = _normalized_controlled(work_state, _CONTROLLED_WORK_STATES, fallback="idle")
    enriched["mode"] = _normalized_controlled(mode, _CONTROLLED_MODES, fallback="ON-DEMAND")
    enriched["presence"] = _normalized_controlled(presence, _CONTROLLED_PRESENCE, fallback="OFFLINE")
    enriched["reply"] = _normalized_controlled(reply, _CONTROLLED_REPLY, fallback="REPLY")
    enriched["reachability"] = _normalized_controlled(reachability, _CONTROLLED_REACHABILITY, fallback="unavailable")
    enriched["confidence"] = _normalized_controlled(confidence, _CONTROLLED_CONFIDENCE, fallback="MEDIUM")
    enriched["confidence_reason"] = _normalized_controlled(
        confidence_reason,
        _CONTROLLED_CONFIDENCE_REASONS,
        fallback="unknown",
    )
    enriched["confidence_detail"] = str(confidence_detail or "").strip() or None
    enriched["attestation_state"] = _normalized_optional_controlled(
        enriched.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    enriched["approval_state"] = _normalized_optional_controlled(
        enriched.get("approval_state"), _CONTROLLED_APPROVAL_STATES
    )
    enriched["identity_status"] = _normalized_optional_controlled(
        enriched.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES
    )
    enriched["space_status"] = _normalized_optional_controlled(enriched.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    enriched["environment_status"] = _normalized_optional_controlled(
        enriched.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    enriched["active_space_source"] = _normalized_optional_controlled(
        enriched.get("active_space_source"), _CONTROLLED_ACTIVE_SPACE_SOURCES
    )
    queue_capable = profile["placement"] == "mailbox"
    enriched["queue_capable"] = queue_capable
    enriched["queue_depth"] = int(enriched.get("backlog_depth") or 0) if queue_capable else 0
    if not queue_capable and str(enriched.get("current_status") or "").strip().lower() == "queued":
        enriched["current_status"] = "idle"
        if str(enriched.get("current_activity") or "").strip().lower().startswith("queued in gateway"):
            enriched["current_activity"] = None
    if str(enriched.get("current_status") or "").strip().lower() == "attaching" and (
        connected or (_age_seconds(enriched.get("last_started_at"), now=now) or 0) > 30
    ):
        enriched["current_status"] = None
        if str(enriched.get("current_activity") or "").strip().lower().startswith("starting attached"):
            enriched["current_activity"] = None
    enriched.setdefault("last_successful_doctor_at", None)
    enriched.setdefault("last_doctor_result", None)
    return enriched


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_assets import infer_operator_profile  # noqa: E402
from .gateway_identity import _parse_iso8601, evaluate_identity_space_binding  # noqa: E402
from .gateway_state import (  # noqa: E402
    _derive_confidence,
    _derive_liveness,
    _derive_mode,
    _derive_presence,
    _derive_reachability,
    _derive_reply,
    _derive_work_state,
    _external_runtime_connected,
)
from .gateway_storage import load_gateway_registry  # noqa: E402
