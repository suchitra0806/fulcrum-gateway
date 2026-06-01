"""Load + filter activity.jsonl entries for audit export.

Reads ``~/.ax/gateway/activity.jsonl`` line by line, applies ``--since``
/ ``--until`` / ``--event`` / ``--agent`` filters, optionally redacts
secret / PII fields, and renders the chosen output format.

Kept as a separate module from ``ax_cli.gateway`` so the export path
can be unit-tested without spinning up the full Gateway daemon, and so
it can later be reused by an SIEM-forwarding daemon or scheduled job
without dragging in the CLI module.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path
from typing import Any, Iterable

from .formats import format_cef, format_jsonl, format_splunk
from .redact import redact_record


def _parse_iso_filter(value: str | None) -> _dt.datetime | None:
    """Parse a user-supplied ISO-8601 timestamp into a UTC-aware datetime.

    Accepts ``Z``-suffixed UTC and bare ``+00:00`` variants the way
    activity.jsonl writes them. Naive datetimes are assumed UTC so an
    operator running ``--since 2026-05-01`` doesn't need to think about
    their local timezone.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _record_ts(record: dict[str, Any]) -> _dt.datetime | None:
    ts_raw = record.get("ts")
    if not ts_raw:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def load_activity_events(
    log_path: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    events: Iterable[str] | None = None,
    agents: Iterable[str] | None = None,
    stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Read and filter ``activity.jsonl``. Returns records in oldest-to-newest order.

    Malformed lines are skipped rather than aborting the export — an audit run
    must succeed even if one log line was truncated by a crash. When ``stats``
    is provided, it gets populated in-place with ``{"loaded": int, "skipped":
    int}`` so the caller can surface the skipped count (a silent line drop in
    an *audit* export can mask gaps or tampering, so the CLI emits the count
    to stderr).

    ``since`` and ``until`` are both **inclusive** boundaries — events whose
    ``ts`` equals either endpoint are included.

    Returns an empty list if ``log_path`` does not exist.
    """
    if not log_path.exists():
        if stats is not None:
            stats["loaded"] = 0
            stats["skipped"] = 0
        return []
    since_dt = _parse_iso_filter(since)
    until_dt = _parse_iso_filter(until)
    event_set = {e.strip() for e in events if e and e.strip()} if events else None
    agent_set = {a.strip().lower() for a in agents if a and a.strip()} if agents else None

    items: list[dict[str, Any]] = []
    skipped = 0
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = _json.loads(line)
                except _json.JSONDecodeError:
                    # Malformed line — don't abort the export, but track it so
                    # the operator can spot gaps in the audit trail.
                    skipped += 1
                    continue
                if not isinstance(record, dict):
                    skipped += 1
                    continue
                if event_set is not None and str(record.get("event") or "") not in event_set:
                    continue
                if agent_set is not None and str(record.get("agent_name") or "").lower() not in agent_set:
                    continue
                if since_dt is not None or until_dt is not None:
                    record_ts = _record_ts(record)
                    if record_ts is None:
                        # No usable ts on a time-bounded export — can't place
                        # the event on the timeline; count as skipped.
                        skipped += 1
                        continue
                    if since_dt is not None and record_ts < since_dt:
                        continue
                    if until_dt is not None and record_ts > until_dt:
                        continue
                items.append(record)
    except OSError as exc:
        raise OSError(f"Cannot read audit log at {log_path}: {exc}") from exc
    if stats is not None:
        stats["loaded"] = len(items)
        stats["skipped"] = skipped
    return items


def export_events(
    records: list[dict[str, Any]],
    *,
    output_format: str = "jsonl",
    redact: bool = False,
    redact_message_content: bool = False,
) -> str:
    """Format an already-filtered list of records into the chosen output shape.

    Set ``redact=True`` to mask credential-shaped keys (``token``, ``*_secret``,
    ``Authorization``, etc.). Set ``redact_message_content=True`` to additionally
    mask user-authored message body fields (``content``, ``reply_preview``).
    """
    if redact or redact_message_content:
        records = [redact_record(r, redact_message_content=redact_message_content) for r in records]

    fmt = output_format.lower()
    if fmt == "jsonl":
        return format_jsonl(records)
    if fmt == "cef":
        return format_cef(records)
    if fmt == "splunk":
        return format_splunk(records)
    raise ValueError(f"Unknown audit export format: {output_format!r}. Expected jsonl, cef, or splunk.")
