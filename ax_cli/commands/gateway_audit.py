"""ax gateway — activity audit-log export command.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from ..gateway import (
    activity_log_path,
)
from ..output import JSON_OPTION, err_console, print_json
from .gateway_app import audit_app

# ---------------------------------------------------------------------------
# audit sub-app — SIEM-compatible export of the activity.jsonl log for
# ATO / STIG compliance review (issue #62). The actual format writers,
# redactor, and loader live in ax_cli.audit; this command is a thin CLI
# wrapper that pipes options through.
# ---------------------------------------------------------------------------


@audit_app.command("export")
def audit_export(
    output_format: str = typer.Option(
        "jsonl",
        "--format",
        "-f",
        help="Output format: jsonl (default, JSON Lines), cef (ArcSight Common Event Format), splunk (Splunk JSON).",
    ),
    since: str = typer.Option(
        None,
        "--since",
        help=(
            "ISO-8601 timestamp (e.g. 2026-05-01T00:00:00+00:00). "
            "Only export events at or after this time (inclusive boundary)."
        ),
    ),
    until: str = typer.Option(
        None,
        "--until",
        help=(
            "ISO-8601 timestamp. Only export events at or before this time "
            "(inclusive boundary — events whose `ts` equals --until are included)."
        ),
    ),
    event: list[str] = typer.Option(
        None,
        "--event",
        help="Filter to specific event type(s). Repeatable (e.g. --event runtime_started --event runtime_stopped).",
    ),
    agent: list[str] = typer.Option(
        None,
        "--agent",
        help="Filter to specific agent name(s). Repeatable.",
    ),
    redact: bool = typer.Option(
        True,
        "--redact/--no-redact",
        help=(
            "Mask credential-shaped fields (token, *_secret, *_key, Authorization). "
            "Default: enabled. Use --no-redact to export raw values — refused when "
            "writing to a file unless --i-understand-credentials-in-file is also set, "
            "since the source activity.jsonl is 0o600 but file outputs inherit umask."
        ),
    ),
    redact_message_content: bool = typer.Option(
        False,
        "--redact-content",
        help=(
            "Additionally mask user-authored message body fields (content, reply_preview). "
            "Some audits require this; others require the content intact for context."
        ),
    ),
    output: str = typer.Option(
        None,
        "--output",
        "-o",
        help="Write to file instead of stdout. Use '-' to force stdout. File is created with 0o600 perms.",
    ),
    allow_unredacted_file: bool = typer.Option(
        False,
        "--i-understand-credentials-in-file",
        help=(
            "Explicit acknowledgment required to combine --no-redact with -o/--output, since the "
            "exported file may contain raw bearer tokens from log payloads."
        ),
    ),
):
    """Export the activity audit log in a SIEM-compatible format.

    Reads ~/.ax/gateway/activity.jsonl, applies filters, redacts credential-
    shaped fields by default, and renders the result as JSONL, CEF, or Splunk
    JSON. File outputs are created with 0o600 perms to match the source log.

    Examples:

      ax gateway audit export --since 2026-05-01
      ax gateway audit export --format cef --event connector_tool_failed
      ax gateway audit export --format splunk --agent codex-bot -o /var/log/ax-audit.json
    """

    from ..audit import export_events, load_activity_events

    log_path = activity_log_path()

    # Refuse to write potentially-secret-bearing raw events to a file unless the
    # operator explicitly acknowledges (Andrew's #62 finding #1). stdout is fine
    # because it inherits whatever the operator's shell environment provides.
    is_file_output = bool(output) and output != "-"
    if is_file_output and not redact and not allow_unredacted_file:
        err_console.print(
            "[red]Refusing to write --no-redact output to a file.[/red] "
            "Add --i-understand-credentials-in-file to acknowledge that the file "
            "may contain raw bearer tokens, or drop --no-redact."
        )
        raise typer.Exit(1)

    stats: dict[str, int] = {}
    try:
        records = load_activity_events(
            log_path,
            since=since,
            until=until,
            events=event,
            agents=agent,
            stats=stats,
        )
    except (ValueError, OSError) as exc:
        err_console.print(f"[red]Audit export failed:[/red] {exc}")
        raise typer.Exit(1)

    try:
        rendered = export_events(
            records,
            output_format=output_format,
            redact=redact,
            redact_message_content=redact_message_content,
        )
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    if is_file_output:
        out_path = Path(output)
        out_path.write_text(rendered, encoding="utf-8")
        # Match the source activity.jsonl perms (0o600) so the export doesn't
        # silently widen the credential boundary the source already enforces.
        try:
            out_path.chmod(0o600)
        except OSError:
            # Best-effort on filesystems that don't honor unix perms (Windows
            # NTFS via WSL2, some network mounts). The operator will see the
            # file path and can secure it manually.
            err_console.print(
                f"[yellow]Warning:[/yellow] could not chmod {output} to 0o600 — "
                "secure the file manually if it may contain sensitive event payloads."
            )
        err_console.print(f"[green]Wrote {len(records)} record(s) to {output}[/green] (format={output_format.lower()})")
    else:
        # Write to stdout directly so pipes (`| splunk hec`, `| grep`) work
        # without Rich-formatting interference.
        sys.stdout.write(rendered)
        sys.stdout.flush()
        err_console.print(f"[dim]Exported {len(records)} record(s) (format={output_format.lower()})[/dim]")
    # Surface the skipped-line count so silent gaps in the audit trail are
    # visible to the operator. An audit export that quietly loses lines can
    # mask tampering or crashes mid-write.
    skipped = stats.get("skipped", 0)
    if skipped:
        err_console.print(
            f"[yellow]Note:[/yellow] {skipped} line(s) skipped (malformed JSON, "
            "non-dict, or missing/unparseable `ts` on time-bounded export)."
        )


@audit_app.command("verify")
def audit_verify(
    from_seq: int = typer.Option(
        None,
        "--from-seq",
        help="Skip records with seq < N. Useful when verifying only the active rotation window.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Reject pre-feature records (no `seq` field) instead of skipping them. "
            "Required for compliance audits that demand a fully chained log."
        ),
    ),
    as_json: bool = JSON_OPTION,
):
    """Verify the activity log's chain-of-custody (#171).

    Walks ``~/.ax/gateway/activity.jsonl`` and confirms each record's
    ``prev_hash`` matches the sha256 of the prior record's serialized form
    and that ``seq`` increments monotonically. Reports the first break with
    the line number, seq, and a short hash diff. Failure messages reference
    seq + file line only — never raw record content (ADR-005).
    """
    from ..audit import verify_chain

    log_path = activity_log_path()
    report = verify_chain(log_path, from_seq=from_seq, strict=strict)

    if as_json:
        print_json(
            {
                "file_path": report.file_path,
                "ok": report.ok,
                "total_lines": report.total_lines,
                "chained_records": report.chained_records,
                "legacy_records": report.legacy_records,
                "breaks": [
                    {
                        "kind": b.kind,
                        "line_no": b.line_no,
                        "seq": b.seq,
                        "expected_prev_hash": b.expected_prev_hash,
                        "actual_prev_hash": b.actual_prev_hash,
                        "detail": b.detail,
                    }
                    for b in report.breaks
                ],
            }
        )
        if not report.ok:
            raise typer.Exit(1)
        return

    if report.ok:
        err_console.print(f"[green]Chain intact:[/green] {report.summary}")
        return

    err_console.print(f"[red]Chain break(s) detected in {report.file_path}[/red]")
    for b in report.breaks:
        err_console.print(f"  line {b.line_no} seq={b.seq} kind={b.kind} — {b.detail}")
    raise typer.Exit(1)
