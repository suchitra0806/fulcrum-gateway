"""Chain-of-custody verification for ``activity.jsonl`` (#171).

Covers ``ax_cli.audit.verify.verify_chain`` and ``record_gateway_activity``'s
new ``seq`` / ``prev_hash`` fields end-to-end.

Module layout note: after the #28 gateway split, the write path
(``record_gateway_activity`` + ``activity_log_path`` + ``load_gateway_registry``)
lives in ``ax_cli.gateway_storage`` and the CLI command resolves
``activity_log_path`` from ``ax_cli.commands.gateway_audit`` — so the isolation
fixture patches both call sites (matching ``tests/test_connectors_activity.py``
and ``tests/test_audit_export.py``).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from ax_cli import gateway_storage as gws
from ax_cli.audit import verify_chain
from ax_cli.main import app

runner_app = app


def _hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    log = tmp_path / "activity.jsonl"
    # Write path resolves these from gateway_storage's module globals.
    monkeypatch.setattr(gws, "activity_log_path", lambda: log)
    monkeypatch.setattr(gws, "load_gateway_registry", lambda: {"gateway": {"gateway_id": "gw-test"}})
    # CLI read path resolves activity_log_path from the audit command module.
    monkeypatch.setattr("ax_cli.commands.gateway_audit.activity_log_path", lambda: log)
    return log


# ── write path ──────────────────────────────────────────────────────────────


def test_first_record_has_seq_1_and_null_prev_hash(isolated):
    rec = gws.record_gateway_activity("gateway_login", username="alice")
    assert rec["seq"] == 1
    assert rec["prev_hash"] is None


def test_subsequent_record_increments_seq_and_hashes_prior_line(isolated):
    gws.record_gateway_activity("gateway_login", username="alice")
    first_line = isolated.read_text().splitlines()[0]
    rec = gws.record_gateway_activity("managed_agent_added", agent_name="echo-demo")
    assert rec["seq"] == 2
    assert rec["prev_hash"] == _hash(first_line)


def test_chain_written_to_disk_matches_in_memory_record(isolated):
    gws.record_gateway_activity("gateway_login")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    lines = isolated.read_text().splitlines()
    assert json.loads(lines[0])["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2
    assert json.loads(lines[1])["prev_hash"] == _hash(lines[0])


# ── verify_chain happy paths ────────────────────────────────────────────────


def test_verify_passes_on_clean_chain(isolated):
    gws.record_gateway_activity("gateway_login")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    gws.record_gateway_activity("asset_bound", agent_name="x")
    report = verify_chain(isolated)
    assert report.ok is True
    assert report.chained_records == 3
    assert report.breaks == ()


def test_verify_passes_on_empty_log(tmp_path):
    report = verify_chain(tmp_path / "missing.jsonl")
    assert report.ok is True
    assert report.chained_records == 0


# ── tamper detection ───────────────────────────────────────────────────────


def test_verify_detects_record_modification(isolated):
    gws.record_gateway_activity("gateway_login")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    lines = isolated.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["username"] = "mallory"
    lines[0] = json.dumps(tampered, sort_keys=True)
    isolated.write_text("\n".join(lines) + "\n")
    report = verify_chain(isolated)
    assert report.ok is False
    assert report.breaks[0].kind == "prev_hash_mismatch"
    assert report.breaks[0].seq == 2


def test_verify_detects_deleted_record(isolated):
    gws.record_gateway_activity("gateway_login")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    gws.record_gateway_activity("asset_bound", agent_name="x")
    lines = isolated.read_text().splitlines()
    isolated.write_text(lines[0] + "\n" + lines[2] + "\n")
    report = verify_chain(isolated)
    assert report.ok is False
    assert report.breaks[0].kind == "seq_gap"
    assert report.breaks[0].seq == 3


# ── ADR-005: failure messages must not leak record content ─────────────────


def test_failure_messages_carry_no_raw_record_fields(isolated):
    gws.record_gateway_activity("gateway_login", token="axp_u_SECRET", username="alice")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    lines = isolated.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["event"] = "tampered"
    lines[0] = json.dumps(tampered, sort_keys=True)
    isolated.write_text("\n".join(lines) + "\n")
    report = verify_chain(isolated)
    assert report.ok is False
    for b in report.breaks:
        assert "axp_u_SECRET" not in b.detail
        assert "alice" not in b.detail


# ── legacy / pre-feature records ───────────────────────────────────────────


def test_legacy_records_skipped_in_default_mode(isolated):
    isolated.write_text(json.dumps({"event": "old_no_seq", "ts": "x"}) + "\n")
    gws.record_gateway_activity("gateway_login")  # seq=1, fresh chain
    report = verify_chain(isolated)
    assert report.ok is True
    assert report.legacy_records == 1
    assert report.chained_records == 1


def test_strict_mode_rejects_legacy_records(isolated):
    isolated.write_text(json.dumps({"event": "old_no_seq", "ts": "x"}) + "\n")
    gws.record_gateway_activity("gateway_login")
    report = verify_chain(isolated, strict=True)
    assert report.ok is False
    assert report.breaks[0].kind == "missing_seq"


# ── --from-seq window ──────────────────────────────────────────────────────


def test_from_seq_skips_earlier_records(isolated):
    gws.record_gateway_activity("a")
    gws.record_gateway_activity("b")
    gws.record_gateway_activity("c")
    lines = isolated.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["event"] = "tampered"
    lines[0] = json.dumps(tampered, sort_keys=True)
    isolated.write_text("\n".join(lines) + "\n")
    # Without --from-seq the break surfaces at seq=2.
    assert verify_chain(isolated).ok is False
    # With --from-seq=2 we start fresh from seq 2; nothing to anchor against.
    assert verify_chain(isolated, from_seq=2).ok is True


# ── malformed lines ────────────────────────────────────────────────────────


def test_malformed_line_reported_not_crashed(isolated):
    isolated.write_text("not json\n")
    report = verify_chain(isolated)
    assert report.ok is False
    assert report.breaks[0].kind == "malformed"


# ── CLI integration ────────────────────────────────────────────────────────


def test_cli_audit_verify_clean_log_exits_zero(isolated):
    from typer.testing import CliRunner

    gws.record_gateway_activity("gateway_login")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    result = CliRunner().invoke(runner_app, ["gateway", "audit", "verify"])
    assert result.exit_code == 0
    assert "Chain intact" in result.output


def test_cli_audit_verify_tampered_exits_one(isolated):
    from typer.testing import CliRunner

    gws.record_gateway_activity("gateway_login")
    gws.record_gateway_activity("managed_agent_added", agent_name="x")
    lines = isolated.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["event"] = "tampered"
    lines[0] = json.dumps(tampered, sort_keys=True)
    isolated.write_text("\n".join(lines) + "\n")
    result = CliRunner().invoke(runner_app, ["gateway", "audit", "verify"])
    assert result.exit_code == 1
    assert "Chain break" in result.output
