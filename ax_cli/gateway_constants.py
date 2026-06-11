"""Gateway controlled vocabularies, normalization helpers, and defaults.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

REPLY_ANCHOR_MAX = 500
SEEN_IDS_MAX = 500
DEFAULT_QUEUE_SIZE = 50
DEFAULT_ACTIVITY_LIMIT = 10
DEFAULT_HANDLER_TIMEOUT_SECONDS = 900
MIN_HANDLER_TIMEOUT_SECONDS = 1
SSE_IDLE_TIMEOUT_SECONDS = 45.0
RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 30.0
RUNTIME_STALE_AFTER_SECONDS = 75.0
RUNTIME_HIDDEN_AFTER_SECONDS = 15 * 60.0  # default: hide stale agents after 15 min
SETUP_ERROR_BACKOFF_SCHEDULE = (30.0, 60.0, 120.0, 300.0, 600.0)
SETUP_ERROR_MAX_CONSECUTIVE = 10

HERMES_KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-20250514",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4",
    },
    "bedrock": {
        "base_url": "",
        "default_model": "claude-sonnet-4-20250514",
    },
}


# active = visible, normal operation
# hidden = system auto-hid because of staleness; auto-restores on reconnect
# archived = user explicitly disabled; sticky (no auto-restore); requires explicit `agents restore`
_LIFECYCLE_PHASES = {"active", "hidden", "archived"}
LOCAL_SESSION_TTL_SECONDS = 24 * 60 * 60
GATEWAY_EVENT_PREFIX = "AX_GATEWAY_EVENT "
DEFAULT_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
ENV_DENYLIST = {
    "AX_AGENT_ID",
    "AX_AGENT_NAME",
    "AX_BASE_URL",
    "AX_CONFIG_FILE",
    "AX_ENV",
    "AX_SPACE_ID",
    "AX_TOKEN",
    "AX_TOKEN_FILE",
    "AX_GATEWAY_CONNECTOR_REF",
    "AX_USER_BASE_URL",
    "AX_USER_ENV",
    "AX_USER_TOKEN",
}
_GATEWAY_PROCESS_RE = re.compile(
    r"(?:uv\s+run\s+ax\s+gateway\s+run|(?:^|\s).+?/ax(?:ctl)?\s+gateway\s+run(?:\s|$)|-m\s+ax_cli\.main\s+gateway\s+run(?:\s|$))"
)
_GATEWAY_UI_PROCESS_RE = re.compile(
    r"(?:uv\s+run\s+ax\s+gateway\s+ui|(?:^|\s).+?/ax(?:ctl)?\s+gateway\s+ui(?:\s|$)|-m\s+ax_cli\.main\s+gateway\s+ui(?:\s|$))"
)

_CONTROLLED_PLACEMENTS = {"hosted", "attached", "brokered", "mailbox"}
_CONTROLLED_ACTIVATIONS = {"persistent", "on_demand", "attach_only", "queue_worker"}
_CONTROLLED_LIVENESS = {"connected", "stale", "offline", "setup_error"}
_CONTROLLED_WORK_STATES = {"idle", "queued", "working", "blocked"}
_CONTROLLED_REPLY_MODES = {"interactive", "background", "summary_only", "silent"}
_CONTROLLED_TELEMETRY_LEVELS = {"rich", "basic", "silent"}
_CONTROLLED_ASSET_CLASSES = {
    "interactive_agent",
    "background_worker",
    "scheduled_job",
    "alert_listener",
    "service_proxy",
    "service_account",
}
_CONTROLLED_INTAKE_MODELS = {
    "live_listener",
    "launch_on_send",
    "polling_mailbox",
    "queue_accept",
    "queue_drain",
    "scheduled_run",
    "event_triggered",
    "manual_only",
    "notification_source",
}
_CONTROLLED_TRIGGER_SOURCES = {
    "direct_message",
    "mailbox_poll",
    "manual_check",
    "queued_job",
    "scheduled_invocation",
    "external_alert",
    "manual_trigger",
    "manual_message",
    "automation",
    "scheduled_job",
    "tool_call",
}
_CONTROLLED_RETURN_PATHS = {
    "inline_reply",
    "manual_reply",
    "sender_inbox",
    "summary_post",
    "task_update",
    "event_log",
    "outbound_message",
    "silent",
}
_CONTROLLED_TELEMETRY_SHAPES = {"rich", "basic", "heartbeat_only", "opaque"}
_CONTROLLED_WORKER_MODELS = {"agent_check_in", "queue_drain", "no_runtime"}
_CONTROLLED_ATTESTATION_STATES = {"verified", "drifted", "unknown", "blocked"}
_CONTROLLED_APPROVAL_STATES = {"not_required", "pending", "approved", "rejected"}
_CONTROLLED_IDENTITY_STATUSES = {
    "verified",
    "unknown_identity",
    "credential_mismatch",
    "fallback_blocked",
    "bootstrap_only",
    "blocked",
}
_CONTROLLED_SPACE_STATUSES = {"active_allowed", "active_not_allowed", "no_active_space", "unknown"}
_CONTROLLED_ENVIRONMENT_STATUSES = {
    "environment_allowed",
    "environment_mismatch",
    "environment_unknown",
    "environment_blocked",
}
_CONTROLLED_ACTIVE_SPACE_SOURCES = {"explicit_request", "gateway_binding", "visible_default", "none"}
_CONTROLLED_MODES = {"LIVE", "ON-DEMAND", "INBOX"}
_CONTROLLED_PRESENCE = {"IDLE", "QUEUED", "WORKING", "BLOCKED", "STALE", "OFFLINE", "ERROR"}
_CONTROLLED_REPLY = {"REPLY", "SUMMARY", "SILENT"}
_CONTROLLED_CONFIDENCE = {"HIGH", "MEDIUM", "LOW", "BLOCKED"}
_CONTROLLED_REACHABILITY = {
    "live_now",
    "queue_available",
    "launch_available",
    "attach_required",
    "sse_disconnected",
    "unavailable",
}
_CONTROLLED_CONFIDENCE_REASONS = {
    "live_now",
    "queue_available",
    "launch_available",
    "attach_required",
    "sse_disconnected",
    "unavailable",
    "setup_blocked",
    "recent_test_failed",
    "completion_degraded",
    "approval_required",
    "binding_drift",
    "new_gateway",
    "unknown_asset",
    "asset_mismatch",
    "approval_denied",
    "identity_unbound",
    "identity_mismatch",
    "fallback_blocked",
    "bootstrap_only",
    "active_space_not_allowed",
    "no_active_space",
    "space_unknown",
    "environment_mismatch",
    "unknown",
    "other",
}
_WORKING_STATUSES = {
    "accepted",
    "started",
    "processing",
    "thinking",
    "tool_call",
    "tool_started",
    "streaming",
    "working",
}
_BLOCKED_STATUSES = {"rate_limited"}
_NO_REPLY_STATUSES = {"no_reply", "declined", "skipped", "not_responding"}


# --- Canonical Gateway activity vocabulary ----------------------------------
#
# Spec: GATEWAY-ACTIVITY-VISIBILITY-001 (Phase 1 of the agent-feedback contract).
# The phase set is the supervisor-loop and aX message-bubble contract; the
# event-name → phase map lets runtimes evolve their event vocabulary without
# changing what consumers depend on. Unknown event names still record (legacy
# callers, future events) but receive no phase, so drift is visible instead of
# silently mis-classified.

GATEWAY_ACTIVITY_PHASES: frozenset[str] = frozenset(
    {
        "received",
        "routed",
        "delivered",
        "claimed",
        "working",
        "tool",
        "reply",
        "result",
        "blocked",
        "stale",
        "reminder",
    }
)

GATEWAY_ACTIVITY_EVENTS: dict[str, str] = {
    # received: Gateway has the message in hand
    "message_received": "received",
    "message_queued": "received",
    # delivered: Gateway placed the message into the agent's surface
    "delivered_to_inbox": "delivered",
    "local_message_sent": "delivered",
    "gateway_test_sent": "delivered",
    # claimed: agent has picked up the work
    "message_claimed": "claimed",
    # working: agent is doing model/runtime work for the message
    "runtime_activity": "working",
    # tool: agent is invoking a tool as part of the work
    "tool_started": "tool",
    "tool_call_recorded": "tool",
    "tool_call_record_failed": "tool",
    # reply: agent posted a reply
    "reply_sent": "reply",
    # channel bridge lifecycle
    "channel_message_received": "received",
    "channel_message_delivered": "delivered",
    "channel_reply_sent": "reply",
    # connector: outbound tool invocations via connector providers
    "connector_tool_started": "tool",
    "connector_tool_completed": "tool",
    "connector_tool_failed": "result",
    "connector_tool_denied": "result",
    # result: terminal outcome that is not a reply
    "runtime_error": "result",
    "agent_skipped": "result",
}


def phase_for_event(event_name: str | None) -> str | None:
    """Return the supervisor-facing phase for a Gateway activity event name.

    Returns ``None`` for unknown or empty event names so callers can spot
    drift instead of papering over it.
    """
    if not event_name:
        return None
    return GATEWAY_ACTIVITY_EVENTS.get(str(event_name))


def _normalized_controlled(value: object, allowed: set[str], *, fallback: str) -> str:
    normalized = str(value or "").strip()
    if normalized in allowed:
        return normalized
    lowered_map = {item.lower(): item for item in allowed}
    lowered = normalized.lower()
    if lowered in lowered_map:
        return lowered_map[lowered]
    return fallback


def _normalized_controlled_list(value: object, allowed: set[str], *, fallback: list[str]) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, str):
        parts = value.split(",") if "," in value else [value]
        raw_items = [part.strip() for part in parts if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value if str(item).strip()]

    lowered_map = {item.lower(): item for item in allowed}
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        candidate = item if item in allowed else lowered_map.get(item.lower())
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized or list(fallback)


def _normalized_optional_controlled(value: object, allowed: set[str]) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized in allowed:
        return normalized
    lowered_map = {item.lower(): item for item in allowed}
    return lowered_map.get(normalized.lower())


def _normalized_string_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or list(fallback)
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(fallback)
    return list(fallback)


def _bool_with_fallback(value: object, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return fallback


def _override_fields(snapshot: dict[str, Any], *, domain: str) -> set[str]:
    names: set[str] = set()
    nested = snapshot.get("user_overrides")
    if isinstance(nested, dict):
        scoped = nested.get(domain)
        if isinstance(scoped, dict):
            names.update(str(key).strip() for key in scoped.keys() if str(key).strip())
        elif isinstance(scoped, (list, tuple, set)):
            names.update(str(item).strip() for item in scoped if str(item).strip())

    direct_key = f"{domain}_overrides"
    direct = snapshot.get(direct_key)
    if isinstance(direct, dict):
        names.update(str(key).strip() for key in direct.keys() if str(key).strip())
    elif isinstance(direct, (list, tuple, set)):
        names.update(str(item).strip() for item in direct if str(item).strip())
    return names


def _template_operator_defaults(template_id: str | None, runtime_type: object) -> dict[str, str]:
    template_key = str(template_id or "").strip().lower()
    runtime_key = str(runtime_type or "").strip().lower()
    defaults_by_template = {
        "echo_test": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "ollama": {
            "placement": "hosted",
            "activation": "on_demand",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "hermes": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "sentinel_cli": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "claude_code_channel": {
            "placement": "attached",
            "activation": "attach_only",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "pass_through": {
            "placement": "mailbox",
            "activation": "attach_only",
            "reply_mode": "background",
            "telemetry_level": "basic",
        },
        "service_account": {
            "placement": "mailbox",
            "activation": "queue_worker",
            "reply_mode": "silent",
            "telemetry_level": "basic",
        },
        "inbox": {
            "placement": "mailbox",
            "activation": "queue_worker",
            "reply_mode": "summary_only",
            "telemetry_level": "basic",
        },
    }
    defaults_by_runtime = {
        "echo": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "exec": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "sentinel_inference_sdk": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "hermes_plugin": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "sentinel_cli": {
            "placement": "hosted",
            "activation": "persistent",
            "reply_mode": "interactive",
            "telemetry_level": "rich",
        },
        "claude_code_channel": {
            "placement": "attached",
            "activation": "attach_only",
            "reply_mode": "interactive",
            "telemetry_level": "basic",
        },
        "inbox": {
            "placement": "mailbox",
            "activation": "queue_worker",
            "reply_mode": "summary_only",
            "telemetry_level": "basic",
        },
    }
    return dict(
        defaults_by_template.get(template_key) or defaults_by_runtime.get(runtime_key) or defaults_by_runtime["exec"]
    )


def _is_system_agent(entry: dict[str, Any]) -> bool:
    """Identify infrastructure agents exempt from lifecycle cleanup.

    Per-space switchboards and explicit service accounts are gateway
    plumbing, not user-managed agents — they should be hidden from default
    listings and never auto-archived.
    """
    template_id = str(entry.get("template_id") or "").strip().lower()
    if template_id in {"service_account", "inbox"}:
        return True
    name = str(entry.get("name") or "")
    if name.startswith("switchboard-"):
        return True
    return False


def _hide_after_stale_seconds(registry: dict[str, Any] | None = None) -> float:
    """Resolve the stale-to-hidden threshold (env > registry > default)."""
    env_raw = os.environ.get("AX_GATEWAY_HIDE_AFTER_STALE_SECONDS", "").strip()
    if env_raw:
        try:
            return max(0.0, float(env_raw))
        except ValueError:
            pass
    if isinstance(registry, dict):
        gw = registry.get("gateway") or {}
        if isinstance(gw, dict):
            raw = gw.get("hide_after_stale_seconds")
            if raw is not None:
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    pass
    return RUNTIME_HIDDEN_AFTER_SECONDS


def _template_asset_defaults(template_id: str | None, runtime_type: object) -> dict[str, Any]:
    template_key = str(template_id or "").strip().lower()
    runtime_key = str(runtime_type or "").strip().lower()
    defaults_by_template: dict[str, dict[str, Any]] = {
        "echo_test": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "live-listener", "test-agent"],
            "capabilities": ["reply"],
            "constraints": [],
        },
        "ollama": {
            "asset_class": "interactive_agent",
            "intake_model": "launch_on_send",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "on-demand", "cold-start"],
            "capabilities": ["reply"],
            "constraints": ["requires-model"],
        },
        "hermes": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "rich",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "live-listener", "hosted-by-gateway", "rich-telemetry", "repo-bound"],
            "capabilities": ["reply", "progress", "tool_events"],
            "constraints": ["requires-repo", "requires-provider-auth"],
        },
        "sentinel_cli": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "rich",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "live-listener", "hosted-by-gateway", "sentinel-cli", "rich-telemetry"],
            "capabilities": ["reply", "progress", "tool_events", "session_resume"],
            "constraints": ["requires-cli-auth"],
        },
        "claude_code_channel": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["attached-session", "live-listener", "basic-telemetry"],
            "capabilities": ["reply"],
            "constraints": ["requires-attached-session"],
        },
        "pass_through": {
            "asset_class": "interactive_agent",
            "intake_model": "polling_mailbox",
            "trigger_sources": ["mailbox_poll", "manual_check"],
            "return_paths": ["manual_reply", "summary_post"],
            "telemetry_shape": "basic",
            "worker_model": "agent_check_in",
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "mailbox", "polling", "approval-required"],
            "capabilities": ["poll_mailbox", "reply"],
            "constraints": ["requires-approval"],
        },
        "service_account": {
            "asset_class": "service_account",
            "intake_model": "notification_source",
            "trigger_sources": ["manual_message", "automation", "scheduled_job"],
            "return_paths": ["outbound_message"],
            "telemetry_shape": "basic",
            "worker_model": "no_runtime",
            "addressable": True,
            "messageable": True,
            "schedulable": True,
            "externally_triggered": True,
            "tags": ["service-account", "notifications", "automation-source"],
            "capabilities": ["send_message", "label_source"],
            "constraints": ["no-runtime-reply"],
        },
        "inbox": {
            "asset_class": "background_worker",
            "intake_model": "queue_accept",
            "trigger_sources": ["queued_job", "manual_trigger"],
            "return_paths": ["summary_post"],
            "telemetry_shape": "basic",
            "worker_model": "queue_drain",
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["queue-backed", "summary-later"],
            "capabilities": ["queue_work", "post_summary"],
            "constraints": [],
        },
    }
    defaults_by_runtime: dict[str, dict[str, Any]] = {
        "echo": defaults_by_template["echo_test"],
        "exec": {
            "asset_class": "interactive_agent",
            "intake_model": "live_listener",
            "trigger_sources": ["direct_message"],
            "return_paths": ["inline_reply"],
            "telemetry_shape": "basic",
            "worker_model": None,
            "addressable": True,
            "messageable": True,
            "schedulable": False,
            "externally_triggered": False,
            "tags": ["local", "custom-bridge"],
            "capabilities": ["reply"],
            "constraints": [],
        },
        "sentinel_inference_sdk": defaults_by_template["hermes"],
        "hermes_plugin": defaults_by_template["hermes"],
        "sentinel_cli": defaults_by_template["sentinel_cli"],
        "inbox": defaults_by_template["inbox"],
    }
    resolved = (
        defaults_by_template.get(template_key) or defaults_by_runtime.get(runtime_key) or defaults_by_runtime["exec"]
    )
    return {
        "asset_class": resolved["asset_class"],
        "intake_model": resolved["intake_model"],
        "trigger_sources": list(resolved["trigger_sources"]),
        "return_paths": list(resolved["return_paths"]),
        "telemetry_shape": resolved["telemetry_shape"],
        "worker_model": resolved.get("worker_model"),
        "addressable": bool(resolved.get("addressable", True)),
        "messageable": bool(resolved.get("messageable", True)),
        "schedulable": bool(resolved.get("schedulable", False)),
        "externally_triggered": bool(resolved.get("externally_triggered", False)),
        "tags": list(resolved.get("tags", [])),
        "capabilities": list(resolved.get("capabilities", [])),
        "constraints": list(resolved.get("constraints", [])),
    }


def _asset_type_label(*, asset_class: str, intake_model: str, worker_model: str | None = None) -> str:
    if asset_class == "interactive_agent":
        if intake_model == "live_listener":
            return "Live Listener"
        if intake_model == "launch_on_send":
            return "On-Demand Agent"
        if intake_model == "polling_mailbox":
            return "Pass-through Agent"
    if asset_class == "background_worker":
        if intake_model == "queue_accept" or worker_model == "queue_drain":
            return "Inbox Worker"
        return "Background Worker"
    if asset_class == "scheduled_job":
        return "Scheduled Job"
    if asset_class == "alert_listener":
        return "Alert Listener"
    if asset_class == "service_account":
        return "Service Account"
    if asset_class == "service_proxy":
        return "Service / Tool Proxy"
    return "Connected Asset"


def _output_label(return_paths: list[str]) -> str:
    primary = return_paths[0] if return_paths else "inline_reply"
    return {
        "inline_reply": "Reply",
        "manual_reply": "Manual Reply",
        "sender_inbox": "Inbox",
        "summary_post": "Summary",
        "task_update": "Task",
        "event_log": "Event Log",
        "outbound_message": "Message",
        "silent": "Silent",
    }.get(primary, "Reply")


def infer_asset_descriptor(
    snapshot: dict[str, Any], *, operator_profile: dict[str, str] | None = None
) -> dict[str, Any]:
    defaults = _template_asset_defaults(
        str(snapshot.get("template_id") or "").strip() or None, snapshot.get("runtime_type")
    )
    overrides = _override_fields(snapshot, domain="asset")
    telemetry_fallback = defaults["telemetry_shape"]
    if operator_profile:
        telemetry_fallback = {
            "rich": "rich",
            "basic": "basic",
            "silent": "opaque",
        }.get(operator_profile.get("telemetry_level", ""), telemetry_fallback)

    asset_class = defaults["asset_class"]
    if "asset_class" in overrides:
        asset_class = _normalized_controlled(
            snapshot.get("asset_class"), _CONTROLLED_ASSET_CLASSES, fallback=defaults["asset_class"]
        )

    intake_model = defaults["intake_model"]
    if "intake_model" in overrides:
        intake_model = _normalized_controlled(
            snapshot.get("intake_model"), _CONTROLLED_INTAKE_MODELS, fallback=defaults["intake_model"]
        )

    worker_model = defaults.get("worker_model")
    if "worker_model" in overrides:
        worker_model = _normalized_optional_controlled(
            snapshot.get("worker_model"), _CONTROLLED_WORKER_MODELS
        ) or defaults.get("worker_model")

    trigger_sources = list(defaults["trigger_sources"])
    if "trigger_sources" in overrides or "trigger_source" in overrides:
        trigger_sources = _normalized_controlled_list(
            snapshot.get("trigger_sources")
            if snapshot.get("trigger_sources") is not None
            else snapshot.get("trigger_source"),
            _CONTROLLED_TRIGGER_SOURCES,
            fallback=defaults["trigger_sources"],
        )

    return_paths = list(defaults["return_paths"])
    if "return_paths" in overrides or "return_path" in overrides:
        return_paths = _normalized_controlled_list(
            snapshot.get("return_paths") if snapshot.get("return_paths") is not None else snapshot.get("return_path"),
            _CONTROLLED_RETURN_PATHS,
            fallback=defaults["return_paths"],
        )

    telemetry_shape = telemetry_fallback
    if "telemetry_shape" in overrides:
        telemetry_shape = _normalized_controlled(
            snapshot.get("telemetry_shape"),
            _CONTROLLED_TELEMETRY_SHAPES,
            fallback=telemetry_fallback,
        )

    tags = list(defaults["tags"])
    if "tags" in overrides:
        tags = _normalized_string_list(snapshot.get("tags"), fallback=defaults["tags"])

    capabilities = list(defaults["capabilities"])
    if "capabilities" in overrides:
        capabilities = _normalized_string_list(snapshot.get("capabilities"), fallback=defaults["capabilities"])

    constraints = list(defaults["constraints"])
    if "constraints" in overrides:
        constraints = _normalized_string_list(snapshot.get("constraints"), fallback=defaults["constraints"])

    descriptor = {
        "asset_id": str(snapshot.get("asset_id") or snapshot.get("agent_id") or snapshot.get("name") or "").strip()
        or None,
        "gateway_id": str(snapshot.get("gateway_id") or "").strip() or None,
        "display_name": str(
            snapshot.get("display_name")
            or snapshot.get("name")
            or snapshot.get("template_label")
            or snapshot.get("runtime_type")
            or "Managed Asset"
        ),
        "asset_class": asset_class,
        "intake_model": intake_model,
        "worker_model": worker_model,
        "trigger_sources": trigger_sources,
        "return_paths": return_paths,
        "telemetry_shape": telemetry_shape,
        "addressable": _bool_with_fallback(snapshot.get("addressable"), fallback=defaults["addressable"])
        if "addressable" in overrides
        else defaults["addressable"],
        "messageable": _bool_with_fallback(snapshot.get("messageable"), fallback=defaults["messageable"])
        if "messageable" in overrides
        else defaults["messageable"],
        "schedulable": _bool_with_fallback(snapshot.get("schedulable"), fallback=defaults["schedulable"])
        if "schedulable" in overrides
        else defaults["schedulable"],
        "externally_triggered": _bool_with_fallback(
            snapshot.get("externally_triggered"), fallback=defaults["externally_triggered"]
        )
        if "externally_triggered" in overrides
        else defaults["externally_triggered"],
        "tags": tags,
        "capabilities": capabilities,
        "constraints": constraints,
    }
    descriptor["type_label"] = _asset_type_label(
        asset_class=descriptor["asset_class"],
        intake_model=descriptor["intake_model"],
        worker_model=descriptor.get("worker_model"),
    )
    descriptor["output_label"] = _output_label(descriptor["return_paths"])
    descriptor["primary_trigger_source"] = descriptor["trigger_sources"][0] if descriptor["trigger_sources"] else None
    descriptor["primary_return_path"] = descriptor["return_paths"][0] if descriptor["return_paths"] else None
    return descriptor


def _normalized_base_url(value: object) -> str:
    return str(value or "").strip().rstrip("/")


def _format_daemon_log_line(message: str) -> str:
    """Prepend an ISO-8601 UTC timestamp matching activity.jsonl's `ts` shape.

    activity.jsonl entries carry `ts` like `2026-05-02T01:12:57.246824+00:00`.
    Match that shape so `gateway.log` and `activity.jsonl` are eyeball-correlatable
    by their leading column.
    """

    return f"{datetime.now(timezone.utc).isoformat()} {message}"


def _is_passive_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").lower() in {"inbox", "passive", "monitor"}


def _is_sentinel_inference_sdk_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").strip().lower() == "sentinel_inference_sdk"


def _is_sentinel_hermes_sdk_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").strip().lower() == "sentinel_hermes_sdk"
