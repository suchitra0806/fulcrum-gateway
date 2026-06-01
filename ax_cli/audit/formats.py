"""Output format writers for ``ax gateway audit export``.

Three formats:

  - jsonl: same shape as activity.jsonl (one JSON object per line). Default.
  - cef:   ArcSight Common Event Format — ``CEF:0|<vendor>|<product>|...``.
  - splunk: Splunk's preferred JSON shape with ``_time``, ``source``,
    ``sourcetype``, ``host``.

CEF spec reference: https://docs.centrify.com/.../CommonEventFormat.pdf
Splunk JSON event spec: https://docs.splunk.com/Documentation/Splunk/latest/Data/FormateventsforHTTPEventCollector
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from typing import Any

CEF_VENDOR = "FulcrumDefense"
CEF_PRODUCT = "ax-gateway"
CEF_VERSION_DEFAULT = "0.6.0"

# Map activity events to a numeric CEF severity (0-10). Conservative defaults:
# operator-driven writes are info (3), errors are high (8), security-relevant
# events are critical (10). Unmapped events fall back to ``info``.
#
# Every key here is asserted to be a real, emitted event by
# ``test_severity_map_uses_only_emitted_events`` — the previous version of this
# map included aspirational names that never fire (#62 review feedback). When
# adding a new severity entry, confirm the event name is actually passed to
# ``record_gateway_activity`` somewhere in ax_cli/.
_SEVERITY_MAP: dict[str, int] = {
    # Critical (10) — security boundary violations and hard stops.
    "local_connect_fingerprint_mismatch": 10,
    "attestation_drift_detected": 10,
    "gateway_start_blocked": 10,
    "invocation_blocked": 10,
    # High (8) — errors that broke something operator-visible.
    "runtime_error": 8,
    "runtime_timeout": 8,
    "runtime_auto_disabled": 8,
    "connector_tool_failed": 8,
    "placement_apply_failed": 8,
    "managed_agent_remove_upstream_failed": 8,
    "message_queue_error": 8,
    "message_dropped": 8,
    # Medium (5) — operator-driven state changes worth surfacing in SIEM.
    "connector_tool_denied": 5,
    "managed_agent_added": 5,
    "managed_agent_removed": 5,
    "managed_agent_archived": 5,
    "managed_agent_hidden": 5,
    "managed_agent_recovered": 5,
    "managed_agent_moved_space": 5,
    "manual_attach_confirmed": 5,
    "gateway_space_use": 5,
    # Info (3) — routine events that match the default severity. Listed here
    # so the drift-guard test exercises them rather than only the elevated set.
    "gateway_login": 3,
    "gateway_started": 3,
    "gateway_stopped": 3,
    "runtime_started": 3,
    "runtime_stopped": 3,
    "connector_tool_started": 3,
    "connector_tool_completed": 3,
    "reply_sent": 3,
    "message_received": 3,
}
_SEVERITY_INFO = 3


def format_jsonl(records: list[dict[str, Any]]) -> str:
    """Return JSONL output (one JSON object per line) sorted by ``ts``."""
    lines = [_json.dumps(record, sort_keys=True, default=str) for record in records]
    return "\n".join(lines) + ("\n" if lines else "")


def format_cef(records: list[dict[str, Any]], *, version: str = CEF_VERSION_DEFAULT) -> str:
    """Return CEF-formatted output (one event per line).

    CEF prefix: ``CEF:0|<vendor>|<product>|<version>|<event_id>|<name>|<severity>|<ext>``.
    Pipes, backslashes, and equals signs inside extension values are escaped.
    """
    lines = [_format_cef_record(record, version=version) for record in records]
    return "\n".join(lines) + ("\n" if lines else "")


def format_splunk(records: list[dict[str, Any]]) -> str:
    """Return Splunk-flavored JSON (one JSON object per line) with ``_time``,
    ``source``, ``sourcetype``, ``host`` envelope and the original event as
    the inner payload under ``event``."""
    lines: list[str] = []
    for record in records:
        envelope = {
            "_time": _to_epoch(record.get("ts")),
            "source": "ax-gateway:activity.jsonl",
            "sourcetype": "ax-gateway:audit",
            "host": record.get("gateway_id") or "ax-gateway",
            "event": record,
        }
        lines.append(_json.dumps(envelope, sort_keys=True, default=str))
    return "\n".join(lines) + ("\n" if lines else "")


def _format_cef_record(record: dict[str, Any], *, version: str) -> str:
    event_id = str(record.get("event") or "unknown")
    name = str(record.get("event") or "unknown")
    severity = _SEVERITY_MAP.get(event_id, _SEVERITY_INFO)
    prefix_fields = [
        "CEF:0",
        _escape_cef_prefix(CEF_VENDOR),
        _escape_cef_prefix(CEF_PRODUCT),
        _escape_cef_prefix(version),
        _escape_cef_prefix(event_id),
        _escape_cef_prefix(name),
        str(severity),
    ]
    extension = _cef_extension(record)
    return "|".join(prefix_fields) + ("|" + extension if extension else "|")


def _cef_extension(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(record.keys()):
        if key in {"event"}:
            continue  # already in the prefix as event_id / name
        value = record[key]
        if value is None:
            continue
        parts.append(f"{_escape_cef_key(key)}={_escape_cef_value(value)}")
    return " ".join(parts)


def _escape_cef_prefix(value: str) -> str:
    # In the CEF prefix, the pipe and backslash are the only required escapes.
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _escape_cef_key(key: str) -> str:
    # Replace anything that isn't an ASCII letter, digit, or underscore with
    # underscore so the key parses cleanly.
    return "".join(c if c.isalnum() or c == "_" else "_" for c in key)


def _escape_cef_value(value: Any) -> str:
    text = _json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
    return text.replace("\\", "\\\\").replace("=", "\\=").replace("\n", "\\n").replace("\r", "")


def _to_epoch(ts: Any) -> float:
    """Best-effort conversion of an ISO-8601 timestamp to epoch seconds.

    Splunk's preferred shape is ``_time`` as epoch seconds (float). Falls
    back to 0.0 if the timestamp is missing or unparseable so the record
    still ships with the rest of the export.
    """
    if not ts:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        parsed = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0
