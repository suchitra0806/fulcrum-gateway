"""Tests for ``ax gateway audit export`` (issue #62).

Covers the three layers separately:

  - redact: secret-key matching, message-content opt-in, deep nesting
  - formats: jsonl shape, CEF prefix + extension escaping, Splunk envelope
  - export pipeline: load + filter + format + write

The CLI command itself is exercised through a Typer runner, mostly to
confirm the wiring (sub-app registration, flag parsing, file output)
rather than to re-test the underlying helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ax_cli.audit import (
    export_events,
    format_cef,
    format_jsonl,
    format_splunk,
    load_activity_events,
    redact_record,
)
from ax_cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# redact.py
# ---------------------------------------------------------------------------


def test_redact_masks_exact_secret_keys():
    record = {
        "ts": "2026-05-25T00:00:00+00:00",
        "event": "gateway_login",
        "token": "axp_u_real_secret",
        "api_key": "sk-live-xxx",
        "authorization": "Bearer xyz",
        "agent_name": "demo-bot",
    }
    out = redact_record(record)
    assert out["token"] == "<redacted>"
    assert out["api_key"] == "<redacted>"
    assert out["authorization"] == "<redacted>"
    # Non-secret fields are passed through.
    assert out["event"] == "gateway_login"
    assert out["agent_name"] == "demo-bot"


def test_redact_masks_token_file_locator():
    # token_file is a credential-locator path emitted by managed_agent_added /
    # asset_bound; leaking it forwards the on-disk bearer-token location.
    record = {
        "event": "managed_agent_added",
        "agent_name": "echo-demo",
        "token_file": "/home/dev/.ax/gateway/agents/echo-demo/token",
    }
    out = redact_record(record)
    assert out["token_file"] == "<redacted>"
    assert out["agent_name"] == "echo-demo"


def test_redact_matches_secret_key_suffixes():
    record = {
        "managed_agent_token": "axp_a_secret",
        "client_secret": "shhh",
        "session_proof": "abc",
        "encryption_key": "raw-key-bytes",
        "user_password": "pw",
    }
    out = redact_record(record)
    assert all(value == "<redacted>" for value in out.values())


def test_redact_passes_through_nested_non_secrets():
    record = {
        "event": "managed_agent_added",
        "entry": {
            "name": "demo",
            "agent_id": "uuid-1",
            "metadata": {
                "tags": ["staging", "qa"],
                "agent_token": "secret",  # nested secret should still be masked
            },
        },
    }
    out = redact_record(record)
    assert out["entry"]["name"] == "demo"
    assert out["entry"]["metadata"]["tags"] == ["staging", "qa"]
    assert out["entry"]["metadata"]["agent_token"] == "<redacted>"


def test_redact_message_content_only_when_opted_in():
    record = {
        "event": "message_sent",
        "content": "Hello world",
        "reply_preview": "Reply text",
        "body": "Body text",
    }
    default = redact_record(record)
    # Default: message content passes through; only credential-shaped keys mask.
    assert default["content"] == "Hello world"
    assert default["reply_preview"] == "Reply text"
    assert default["body"] == "Body text"

    masked = redact_record(record, redact_message_content=True)
    assert masked["content"] == "<redacted>"
    assert masked["reply_preview"] == "<redacted>"
    assert masked["body"] == "<redacted>"


def test_redact_returns_copy_does_not_mutate_input():
    record = {"token": "secret", "event": "x"}
    redact_record(record)
    assert record["token"] == "secret"  # original untouched


# ---------------------------------------------------------------------------
# formats.py — jsonl
# ---------------------------------------------------------------------------


def test_format_jsonl_emits_one_object_per_line():
    records = [
        {"ts": "2026-05-25T00:00:00+00:00", "event": "a"},
        {"ts": "2026-05-25T00:00:01+00:00", "event": "b"},
    ]
    out = format_jsonl(records)
    lines = out.strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"ts": "2026-05-25T00:00:00+00:00", "event": "a"}
    assert json.loads(lines[1]) == {"ts": "2026-05-25T00:00:01+00:00", "event": "b"}


def test_format_jsonl_empty_list_returns_empty_string():
    assert format_jsonl([]) == ""


# ---------------------------------------------------------------------------
# formats.py — CEF
# ---------------------------------------------------------------------------


def test_format_cef_prefix_shape():
    records = [{"ts": "2026-05-25T00:00:00+00:00", "event": "gateway_login", "agent_name": "demo"}]
    out = format_cef(records).strip()
    # CEF prefix: CEF:0|Vendor|Product|Version|EventId|Name|Severity|extension
    assert out.startswith("CEF:0|FulcrumDefense|ax-gateway|")
    parts = out.split("|", 7)
    assert len(parts) == 8
    assert parts[1] == "FulcrumDefense"
    assert parts[2] == "ax-gateway"
    assert parts[4] == "gateway_login"  # event_id
    assert parts[5] == "gateway_login"  # name
    assert parts[6] == "3"  # info severity
    assert "agent_name=demo" in parts[7]
    assert "ts=2026-05-25T00:00:00+00:00" in parts[7]


def test_format_cef_severity_map():
    """High-severity events should render with a higher CEF severity."""
    error = format_cef([{"event": "runtime_error"}]).strip()
    assert "|8|" in error  # error severity

    info = format_cef([{"event": "runtime_started"}]).strip()
    assert "|3|" in info  # info severity

    critical = format_cef([{"event": "attestation_drift_detected"}]).strip()
    assert "|10|" in critical  # critical severity

    medium = format_cef([{"event": "connector_tool_denied"}]).strip()
    assert "|5|" in medium  # medium severity

    # Unmapped events fall back to info (3) so a SIEM rule on severity won't
    # alert on every new event added to the activity log.
    unmapped = format_cef([{"event": "some_brand_new_event"}]).strip()
    assert "|3|" in unmapped


def test_severity_map_uses_only_emitted_events(tmp_path: Path):
    """Drift guard: every key in _SEVERITY_MAP must be a real event name that
    is actually passed to ``record_gateway_activity`` somewhere in ax_cli/.

    Without this guard, the severity map can drift from the canonical event
    vocabulary — a SIEM rule alerting on severity 10 then never fires because
    the high-severity event names never get emitted (#62 review finding #2).
    """
    import re
    from pathlib import Path as _Path

    from ax_cli.audit.formats import _SEVERITY_MAP

    repo_root = _Path(__file__).resolve().parent.parent
    ax_cli_dir = repo_root / "ax_cli"
    emitted: set[str] = set()
    pattern = re.compile(r"record_gateway_activity\(\s*[\"']([a-z_]+)[\"']")
    for py_file in ax_cli_dir.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in pattern.finditer(text):
            emitted.add(match.group(1))

    # Sanity check that the scan found *something* — otherwise the test is
    # silently passing without exercising the assertion.
    assert emitted, "scan turned up zero emitted events — pattern or path is wrong"

    missing = sorted(name for name in _SEVERITY_MAP if name not in emitted)
    assert not missing, (
        f"_SEVERITY_MAP references events that are never emitted: {missing}. "
        "A SIEM rule alerting on these severities would never fire. "
        "Remove the entry or wire the event in record_gateway_activity callers."
    )


def test_format_cef_escapes_pipes_and_equals_in_values():
    records = [{"event": "test", "weird": "value=with|pipes"}]
    out = format_cef(records).strip()
    # The `=` inside the value must be escaped so the SIEM parser doesn't
    # split on it. Pipes inside values are allowed by CEF but we escape them
    # defensively because some SIEM parsers don't handle them.
    assert "weird=value\\=with|pipes" in out or "weird=value\\=with\\|pipes" in out


def test_format_cef_complex_value_json_encoded():
    """Lists and dicts in extension values get JSON-encoded for SIEM ingestion."""
    records = [{"event": "test", "tags": ["a", "b"]}]
    out = format_cef(records).strip()
    assert "tags=" in out
    # The list should be serialized as JSON so SIEM parsers can re-parse it.
    assert '["a", "b"]' in out or '[\\"a\\", \\"b\\"]' in out


# ---------------------------------------------------------------------------
# formats.py — Splunk
# ---------------------------------------------------------------------------


def test_format_splunk_envelope_shape():
    records = [
        {
            "ts": "2026-05-25T00:00:00+00:00",
            "event": "gateway_login",
            "agent_name": "demo",
            "gateway_id": "gw-uuid",
        }
    ]
    out = format_splunk(records).strip()
    envelope = json.loads(out)
    # Splunk envelope: _time, source, sourcetype, host, event
    assert "_time" in envelope
    assert envelope["source"] == "ax-gateway:activity.jsonl"
    assert envelope["sourcetype"] == "ax-gateway:audit"
    assert envelope["host"] == "gw-uuid"
    assert envelope["event"] == records[0]
    # _time should be epoch seconds (float), not ISO.
    assert isinstance(envelope["_time"], (int, float))
    assert envelope["_time"] > 0


def test_format_splunk_falls_back_to_default_host_without_gateway_id():
    records = [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}]
    out = format_splunk(records).strip()
    envelope = json.loads(out)
    assert envelope["host"] == "ax-gateway"


def test_format_splunk_handles_missing_timestamp_gracefully():
    records = [{"event": "x"}]
    out = format_splunk(records).strip()
    envelope = json.loads(out)
    assert envelope["_time"] == 0.0  # missing ts -> epoch 0


# ---------------------------------------------------------------------------
# export.py — load + filter
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, records: list[dict]) -> Path:
    """Write a fake activity.jsonl from the given records and return the path."""
    log = tmp_path / "activity.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return log


def test_load_returns_empty_when_log_missing(tmp_path: Path):
    assert load_activity_events(tmp_path / "missing.jsonl") == []


def test_load_filters_by_event_type(tmp_path: Path):
    log = _write_log(
        tmp_path,
        [
            {"ts": "2026-05-25T00:00:00+00:00", "event": "runtime_started"},
            {"ts": "2026-05-25T00:00:01+00:00", "event": "runtime_stopped"},
            {"ts": "2026-05-25T00:00:02+00:00", "event": "gateway_login"},
        ],
    )
    results = load_activity_events(log, events=["runtime_started", "runtime_stopped"])
    assert [r["event"] for r in results] == ["runtime_started", "runtime_stopped"]


def test_load_filters_by_agent_case_insensitive(tmp_path: Path):
    log = _write_log(
        tmp_path,
        [
            {"ts": "2026-05-25T00:00:00+00:00", "event": "x", "agent_name": "Demo-Bot"},
            {"ts": "2026-05-25T00:00:01+00:00", "event": "x", "agent_name": "other"},
        ],
    )
    results = load_activity_events(log, agents=["demo-bot"])
    assert len(results) == 1
    assert results[0]["agent_name"] == "Demo-Bot"


def test_load_filters_by_since_until(tmp_path: Path):
    log = _write_log(
        tmp_path,
        [
            {"ts": "2026-05-25T00:00:00+00:00", "event": "a"},
            {"ts": "2026-05-25T01:00:00+00:00", "event": "b"},
            {"ts": "2026-05-25T02:00:00+00:00", "event": "c"},
        ],
    )
    results = load_activity_events(log, since="2026-05-25T01:00:00+00:00", until="2026-05-25T01:30:00+00:00")
    assert [r["event"] for r in results] == ["b"]


def test_load_accepts_naive_iso_treats_as_utc(tmp_path: Path):
    """Operators passing `--since 2026-05-25` should not need to think about timezones."""
    log = _write_log(
        tmp_path,
        [
            {"ts": "2026-05-24T23:59:00+00:00", "event": "before"},
            {"ts": "2026-05-25T00:00:01+00:00", "event": "after"},
        ],
    )
    results = load_activity_events(log, since="2026-05-25")
    assert [r["event"] for r in results] == ["after"]


def test_load_rejects_invalid_iso_timestamp(tmp_path: Path):
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}])
    with pytest.raises(ValueError, match="Invalid ISO-8601 timestamp"):
        load_activity_events(log, since="not-a-timestamp")


def test_load_skips_malformed_lines(tmp_path: Path):
    """A truncated or corrupt line must not abort the export."""
    log = tmp_path / "activity.jsonl"
    log.write_text(
        '{"ts": "2026-05-25T00:00:00+00:00", "event": "a"}\n'
        "not json at all\n"
        '{"ts": "2026-05-25T00:00:01+00:00", "event": "b"}\n'
    )
    results = load_activity_events(log)
    assert [r["event"] for r in results] == ["a", "b"]


def test_load_drops_event_without_timestamp_when_filtering_by_time(tmp_path: Path):
    """Records with no parseable ts can't be placed on a timeline — exclude when
    the user has asked for a time-bounded export."""
    log = _write_log(
        tmp_path,
        [
            {"event": "no-ts"},
            {"ts": "2026-05-25T00:00:00+00:00", "event": "with-ts"},
        ],
    )
    results = load_activity_events(log, since="2026-05-24T00:00:00+00:00")
    assert [r["event"] for r in results] == ["with-ts"]


# ---------------------------------------------------------------------------
# export.py — orchestration
# ---------------------------------------------------------------------------


def test_export_events_jsonl_passes_through_records():
    records = [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}]
    out = export_events(records, output_format="jsonl")
    assert json.loads(out.strip()) == records[0]


def test_export_events_with_redact_masks_tokens():
    records = [{"ts": "2026-05-25T00:00:00+00:00", "event": "gateway_login", "token": "axp_u_secret"}]
    out = export_events(records, output_format="jsonl", redact=True)
    payload = json.loads(out.strip())
    assert payload["token"] == "<redacted>"


def test_export_events_unknown_format_raises():
    with pytest.raises(ValueError, match="Unknown audit export format"):
        export_events([], output_format="protobuf")


def test_export_events_format_case_insensitive():
    records = [{"event": "x"}]
    assert export_events(records, output_format="JSONL") == export_events(records, output_format="jsonl")


# ---------------------------------------------------------------------------
# CLI wiring — confirm the sub-app is registered + flags parse + file output
# ---------------------------------------------------------------------------


def test_cli_audit_export_help_is_registered():
    """``ax gateway audit`` must be discoverable as a sub-app."""
    result = runner.invoke(app, ["gateway", "audit", "--help"])
    assert result.exit_code == 0
    assert "export" in result.output


def test_cli_audit_export_to_stdout(tmp_path: Path, monkeypatch):
    log = _write_log(
        tmp_path,
        [
            {"ts": "2026-05-25T00:00:00+00:00", "event": "gateway_login", "agent_name": "demo"},
            {"ts": "2026-05-25T00:00:01+00:00", "event": "runtime_started", "agent_name": "demo"},
        ],
    )
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)

    result = runner.invoke(app, ["gateway", "audit", "export", "--format", "jsonl"])
    assert result.exit_code == 0, result.output
    # Two JSON lines on stdout.
    lines = [ln for ln in result.stdout.strip().split("\n") if ln.strip().startswith("{")]
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "gateway_login"
    assert json.loads(lines[1])["event"] == "runtime_started"


def test_cli_audit_export_to_file(tmp_path: Path, monkeypatch):
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x", "token": "secret"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)
    out_path = tmp_path / "audit.out"

    result = runner.invoke(
        app,
        ["gateway", "audit", "export", "--format", "jsonl", "--redact", "-o", str(out_path)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out_path.read_text().strip())
    assert payload["event"] == "x"
    assert payload["token"] == "<redacted>"


def test_cli_audit_export_filter_flags_parse(tmp_path: Path, monkeypatch):
    log = _write_log(
        tmp_path,
        [
            {"ts": "2026-05-25T00:00:00+00:00", "event": "gateway_login", "agent_name": "alice"},
            {"ts": "2026-05-25T00:00:01+00:00", "event": "gateway_login", "agent_name": "bob"},
            {"ts": "2026-05-25T00:00:02+00:00", "event": "runtime_started", "agent_name": "alice"},
        ],
    )
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)

    result = runner.invoke(
        app,
        ["gateway", "audit", "export", "--event", "gateway_login", "--agent", "alice"],
    )
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stdout.strip().split("\n") if ln.strip().startswith("{")]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "gateway_login"
    assert payload["agent_name"] == "alice"


def test_cli_audit_export_invalid_since_exits_cleanly(tmp_path: Path, monkeypatch):
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)

    result = runner.invoke(app, ["gateway", "audit", "export", "--since", "not-a-date"])
    assert result.exit_code == 1
    assert "Invalid ISO-8601" in result.output or "Invalid ISO-8601" in result.stderr


def test_cli_audit_export_unknown_format_exits_with_code_2(tmp_path: Path, monkeypatch):
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)

    result = runner.invoke(app, ["gateway", "audit", "export", "--format", "protobuf"])
    assert result.exit_code == 2
    assert "Unknown audit export format" in result.output or "Unknown audit export format" in result.stderr


# ---------------------------------------------------------------------------
# #62 review findings — output perms, redact default, skipped-line visibility
# ---------------------------------------------------------------------------


def test_cli_audit_export_redact_is_default_on(tmp_path: Path, monkeypatch):
    """Default behaviour (no --redact / --no-redact flag) must mask credentials —
    silent unredacted export is the bug Andrew flagged (#62 finding #1)."""
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x", "token": "axp_u_secret"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)

    result = runner.invoke(app, ["gateway", "audit", "export"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.stdout.strip().split("\n") if ln.strip().startswith("{")]
    payload = json.loads(lines[0])
    assert payload["token"] == "<redacted>"


def test_cli_audit_export_file_output_chmods_to_0o600(tmp_path: Path, monkeypatch):
    """Exports written to a file must match the source 0o600 perms so the
    export doesn't widen the credential boundary (#62 finding #1)."""
    import os
    import stat

    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)
    out_path = tmp_path / "audit.out"

    result = runner.invoke(app, ["gateway", "audit", "export", "-o", str(out_path)])
    assert result.exit_code == 0, result.output
    mode = stat.S_IMODE(os.stat(out_path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_cli_audit_export_refuses_no_redact_file_without_ack(tmp_path: Path, monkeypatch):
    """--no-redact + --output without --i-understand-credentials-in-file is
    refused — the file would silently widen the credential boundary."""
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)
    out_path = tmp_path / "audit.out"

    result = runner.invoke(app, ["gateway", "audit", "export", "--no-redact", "-o", str(out_path)])
    assert result.exit_code == 1
    # The output file must NOT have been created.
    assert not out_path.exists()
    assert "Refusing to write" in result.output or "Refusing to write" in result.stderr


def test_cli_audit_export_allows_no_redact_file_with_ack(tmp_path: Path, monkeypatch):
    """The explicit --i-understand-credentials-in-file ack lets the operator
    bypass the refusal when they really need raw values (e.g. legal hold)."""
    log = _write_log(tmp_path, [{"ts": "2026-05-25T00:00:00+00:00", "event": "x", "token": "axp_u_raw"}])
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)
    out_path = tmp_path / "audit.out"

    result = runner.invoke(
        app,
        [
            "gateway",
            "audit",
            "export",
            "--no-redact",
            "-o",
            str(out_path),
            "--i-understand-credentials-in-file",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out_path.read_text().strip())
    assert payload["token"] == "axp_u_raw"  # raw — the ack flag bypasses redaction


def test_cli_audit_export_emits_skipped_count(tmp_path: Path, monkeypatch):
    """Malformed lines must surface to stderr — silent line drops in an audit
    export can mask gaps or tampering (#62 polish finding)."""
    log = tmp_path / "activity.jsonl"
    log.write_text(
        '{"ts": "2026-05-25T00:00:00+00:00", "event": "good"}\n'
        "not json at all\n"
        '{"ts": "2026-05-25T00:00:01+00:00", "event": "good"}\n'
        "another bad line\n"
    )
    monkeypatch.setattr("ax_cli.commands.gateway.activity_log_path", lambda: log)

    result = runner.invoke(app, ["gateway", "audit", "export"])
    assert result.exit_code == 0, result.output
    combined = result.output + (result.stderr or "")
    assert "2 line(s) skipped" in combined


def test_load_populates_stats_dict(tmp_path: Path):
    """The optional ``stats`` parameter lets callers surface skipped-line counts."""
    log = tmp_path / "activity.jsonl"
    log.write_text(
        '{"ts": "2026-05-25T00:00:00+00:00", "event": "a"}\n'
        "garbage\n"
        '{"ts": "2026-05-25T00:00:01+00:00", "event": "b"}\n'
    )
    stats: dict[str, int] = {}
    results = load_activity_events(log, stats=stats)
    assert len(results) == 2
    assert stats == {"loaded": 2, "skipped": 1}
