"""Gateway liveness, work-state, confidence, reachability, and mode derivation.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

from typing import Any

from .gateway_constants import (
    _BLOCKED_STATUSES,
    _CONTROLLED_APPROVAL_STATES,
    _CONTROLLED_ATTESTATION_STATES,
    _CONTROLLED_CONFIDENCE_REASONS,
    _CONTROLLED_ENVIRONMENT_STATUSES,
    _CONTROLLED_IDENTITY_STATUSES,
    _CONTROLLED_SPACE_STATUSES,
    _WORKING_STATUSES,
    RUNTIME_OFFLINE_AFTER_SECONDS,
    RUNTIME_STALE_AFTER_SECONDS,
    _normalized_optional_controlled,
)


def _looks_like_setup_error(snapshot: dict[str, Any], raw_state: str) -> bool:
    if raw_state == "error":
        return True
    last_error = str(snapshot.get("last_error") or "").lower()
    preview = str(snapshot.get("last_reply_preview") or "").lower()
    if "repo not found" in last_error or "repo not found" in preview:
        return True
    if preview.startswith("(stderr:") or last_error.startswith("stderr:"):
        return True
    return False


def _derive_liveness(snapshot: dict[str, Any], *, raw_state: str, last_seen_age: int | None) -> tuple[str, bool]:
    if _looks_like_setup_error(snapshot, raw_state):
        return "setup_error", False
    if raw_state == "running":
        # Two-threshold staleness escalation: brief gap (>75s) → stale (yellow,
        # may self-heal); persistent gap (>5min) → offline (red, needs operator
        # attention). Applies to entries whose raw state stays "running" while
        # registry signals age — e.g. a supervised runtime with a wedged loop.
        # Attached sessions and external plugins are resolved by the sweep into
        # raw "stale" and reach red via reachability instead (ADR-008).
        if last_seen_age is None or last_seen_age > RUNTIME_OFFLINE_AFTER_SECONDS:
            return "offline", False
        if last_seen_age > RUNTIME_STALE_AFTER_SECONDS:
            return "stale", False
        # Channel agents report SSE subscription health separately from process
        # liveness. A running process with a dead SSE stream can't receive messages.
        sse_connected = snapshot.get("sse_connected")
        if sse_connected is False:
            return "stale", False
        return "connected", True
    if raw_state in {"starting", "reconnecting", "stale"}:
        return "stale", False
    return "offline", False


def _external_runtime_connected(snapshot: dict[str, Any], *, last_seen_age: int | None) -> bool:
    state = str(snapshot.get("external_runtime_state") or "").strip().lower()
    if state not in {"connected", "running", "active", "heartbeat"}:
        return False
    return last_seen_age is not None and last_seen_age <= RUNTIME_STALE_AFTER_SECONDS


def _external_runtime_expected(snapshot: dict[str, Any]) -> bool:
    """Whether this runtime is owned by an external process/plugin.

    External Hermes platform adapters should stay externally managed across
    Gateway restarts. A missing fresh heartbeat means "plugin not attached",
    not permission to fall back to the legacy managed sentinel.
    """
    if bool(snapshot.get("external_runtime_managed")):
        return True
    if str(snapshot.get("external_runtime_kind") or "").strip():
        return True
    if str(snapshot.get("external_runtime_instance_id") or "").strip():
        return True
    return False


def _derive_work_state(snapshot: dict[str, Any], *, liveness: str, profile: dict[str, str] | None = None) -> str:
    attestation_state = _normalized_optional_controlled(
        snapshot.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    approval_state = _normalized_optional_controlled(snapshot.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
    identity_status = _normalized_optional_controlled(snapshot.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
    environment_status = _normalized_optional_controlled(
        snapshot.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    space_status = _normalized_optional_controlled(snapshot.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    if liveness == "setup_error":
        return "blocked"
    if attestation_state in {"drifted", "unknown", "blocked"} or approval_state in {"pending", "rejected"}:
        return "blocked"
    if identity_status in {"unknown_identity", "credential_mismatch", "fallback_blocked", "bootstrap_only", "blocked"}:
        return "blocked"
    if environment_status in {"environment_mismatch", "environment_blocked"}:
        return "blocked"
    if space_status in {"active_not_allowed", "no_active_space"}:
        return "blocked"
    status = str(snapshot.get("current_status") or "").strip().lower()
    backlog_depth = int(snapshot.get("backlog_depth") or 0)
    profile = profile or {}
    queue_state_applies = profile.get("placement") == "mailbox" or profile.get("activation") == "queue_worker"
    if status in _WORKING_STATUSES:
        return "working"
    if queue_state_applies and (status == "queued" or backlog_depth > 0):
        return "queued"
    if status in _BLOCKED_STATUSES:
        return "blocked"
    return "idle"


def _doctor_has_failed(snapshot: dict[str, Any]) -> bool:
    result = snapshot.get("last_doctor_result")
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in {"failed", "error"}:
        return True
    checks = result.get("checks")
    if isinstance(checks, list):
        return any(
            isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "failed" for item in checks
        )
    return False


def _derive_mode(profile: dict[str, str]) -> str:
    if profile["placement"] == "mailbox":
        return "INBOX"
    if profile["activation"] in {"persistent", "attach_only"}:
        return "LIVE"
    return "ON-DEMAND"


def _derive_presence(*, mode: str, liveness: str, work_state: str) -> str:
    if liveness == "setup_error":
        return "ERROR"
    if work_state == "blocked":
        return "BLOCKED"
    if liveness == "stale":
        return "STALE"
    # OFFLINE presence is meaningful only for LIVE agents — it signals that an
    # always-on listener has lost its connection. For INBOX/ON-DEMAND agents,
    # availability is defined by queue access or launch capability, not by an
    # active connection, so offline liveness falls through to IDLE below.
    if liveness == "offline" and mode == "LIVE":
        return "OFFLINE"
    if work_state == "working":
        return "WORKING"
    if work_state == "queued":
        return "QUEUED"
    return "IDLE"


def _derive_reply(reply_mode: str) -> str:
    if reply_mode == "interactive":
        return "REPLY"
    if reply_mode == "silent":
        return "SILENT"
    return "SUMMARY"


def _derive_reachability(
    *,
    snapshot: dict[str, Any],
    mode: str,
    liveness: str,
    activation: str,
    last_seen_age: int | None = None,
) -> str:
    attestation_state = _normalized_optional_controlled(
        snapshot.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    approval_state = _normalized_optional_controlled(snapshot.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
    identity_status = _normalized_optional_controlled(snapshot.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
    environment_status = _normalized_optional_controlled(
        snapshot.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    space_status = _normalized_optional_controlled(snapshot.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    if liveness == "setup_error":
        return "unavailable"
    if attestation_state in {"drifted", "unknown", "blocked"} or approval_state in {"pending", "rejected"}:
        return "unavailable"
    if identity_status in {"unknown_identity", "credential_mismatch", "fallback_blocked", "bootstrap_only", "blocked"}:
        return "unavailable"
    if environment_status in {"environment_mismatch", "environment_blocked"}:
        return "unavailable"
    if space_status in {"active_not_allowed", "no_active_space"}:
        return "unavailable"
    if mode == "INBOX":
        return "queue_available"
    if activation == "attach_only" and liveness in {"stale", "offline"}:
        # A frozen sse_connected=False from a session that died during an SSE
        # outage must not mask "process gone". The channel bridge heartbeat
        # loop writes every 30s while alive, so sse_disconnected is only
        # trustworthy while the registry signal is fresh.
        signal_fresh = last_seen_age is not None and last_seen_age <= RUNTIME_STALE_AFTER_SECONDS
        if snapshot.get("sse_connected") is False and signal_fresh:
            return "sse_disconnected"
        return "attach_required"
    if mode == "LIVE" and liveness == "connected":
        return "live_now"
    if mode == "ON-DEMAND" and liveness != "setup_error":
        return "launch_available"
    return "unavailable"


def _setup_error_detail(snapshot: dict[str, Any]) -> str:
    if _doctor_has_failed(snapshot):
        summary = _doctor_summary(snapshot)
        if summary:
            return summary
    return str(
        snapshot.get("last_error")
        or snapshot.get("last_reply_preview")
        or "Setup must be fixed before Gateway can send work."
    )


def _doctor_summary(snapshot: dict[str, Any]) -> str:
    result = snapshot.get("last_doctor_result")
    if not isinstance(result, dict):
        return ""
    summary = str(result.get("summary") or "").strip()
    if summary:
        return summary
    checks = result.get("checks")
    if isinstance(checks, list):
        failed = [
            str(item.get("name") or "").strip()
            for item in checks
            if isinstance(item, dict) and str(item.get("status") or "").strip().lower() == "failed"
        ]
        if failed:
            return f"Doctor failed: {', '.join(filter(None, failed))}."
    return ""


def _derive_confidence(
    snapshot: dict[str, Any],
    *,
    mode: str,
    liveness: str,
    reachability: str,
) -> tuple[str, str, str]:
    attestation_state = _normalized_optional_controlled(
        snapshot.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
    )
    approval_state = _normalized_optional_controlled(snapshot.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
    governance_reason = _normalized_optional_controlled(
        snapshot.get("confidence_reason"), _CONTROLLED_CONFIDENCE_REASONS
    )
    governance_detail = (
        str(snapshot.get("confidence_detail") or "").strip()
        or "Gateway blocked this runtime until its binding is approved."
    )
    identity_status = _normalized_optional_controlled(snapshot.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
    environment_status = _normalized_optional_controlled(
        snapshot.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
    )
    space_status = _normalized_optional_controlled(snapshot.get("space_status"), _CONTROLLED_SPACE_STATUSES)
    if liveness == "setup_error":
        return ("BLOCKED", "setup_blocked", _setup_error_detail(snapshot))
    if identity_status == "unknown_identity":
        return (
            "BLOCKED",
            "identity_unbound",
            "Gateway does not have a bound acting identity for this asset in the requested environment.",
        )
    if identity_status in {"credential_mismatch", "fallback_blocked"}:
        return (
            "BLOCKED",
            "identity_mismatch",
            "Gateway blocked a mismatched acting identity instead of borrowing another identity.",
        )
    if identity_status == "bootstrap_only":
        return (
            "BLOCKED",
            "bootstrap_only",
            "Gateway bootstrap credentials can only be used for setup, verification, or repair flows.",
        )
    if environment_status == "environment_mismatch":
        return (
            "BLOCKED",
            "environment_mismatch",
            "Requested environment does not match the bound Gateway environment for this asset.",
        )
    if environment_status == "environment_blocked":
        return ("BLOCKED", "environment_mismatch", "Gateway blocked this asset in the requested environment.")
    if space_status == "active_not_allowed":
        return (
            "BLOCKED",
            "active_space_not_allowed",
            "The resolved target space is not allowed for this acting identity.",
        )
    if space_status == "no_active_space":
        return ("BLOCKED", "no_active_space", "Gateway does not have an active space selected for this asset.")
    if space_status == "unknown":
        return ("LOW", "space_unknown", "Gateway could not verify the allowed-space list for this acting identity.")
    if approval_state == "rejected":
        return ("BLOCKED", governance_reason or "approval_denied", governance_detail)
    if attestation_state in {"blocked", "unknown", "drifted"} or approval_state == "pending":
        return ("BLOCKED", governance_reason or "approval_required", governance_detail)
    if _doctor_has_failed(snapshot):
        detail = _doctor_summary(snapshot) or "Gateway Doctor reported a failed send path."
        return ("LOW", "recent_test_failed", detail)
    completion_rate = snapshot.get("completion_rate")
    try:
        if completion_rate is not None and float(completion_rate) < 0.5:
            return ("LOW", "completion_degraded", "Recent completion rate is below the healthy threshold.")
    except (TypeError, ValueError):
        pass
    if mode == "INBOX":
        return ("HIGH", "queue_available", "Gateway can safely accept and queue work now.")
    if mode == "ON-DEMAND" and reachability == "launch_available":
        return ("MEDIUM", "launch_available", "Gateway can launch this runtime on send. Cold start possible.")
    if liveness in {"offline", "stale"}:
        if reachability == "sse_disconnected":
            return (
                "LOW",
                "sse_disconnected",
                "The attached session's platform SSE subscription is down — "
                "messages will not be delivered until it reconnects.",
            )
        if reachability == "attach_required":
            return ("LOW", "attach_required", "Start the attached session before sending.")
        return ("LOW", "unavailable", "Gateway does not currently have a healthy live path.")
    if liveness == "connected":
        return ("HIGH", "live_now", "A live runtime is ready to claim work.")
    return ("MEDIUM", "unknown", "Gateway has partial health signals but no stronger confidence signal yet.")
