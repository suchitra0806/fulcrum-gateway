"""Chain-of-custody verification for ``~/.ax/gateway/activity.jsonl``.

``record_gateway_activity`` writes ``seq`` (monotonic per ``gateway_dir``) and
``prev_hash`` (sha256 of the prior record's serialized form, or ``null`` for
the first chained record). This module walks the log and validates that the
chain is intact.

Per ADR-005, failure messages never quote raw record content — activity
records can carry ``credential_source`` / ``token_file`` / other sensitive
fields. Reports reference ``seq``, file line, and a short hash diff only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def _hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _short(value: str | None) -> str:
    if value is None:
        return "<none>"
    return value[:12] + "…" if len(value) > 12 else value


@dataclass(frozen=True)
class VerifyBreak:
    """A single chain integrity violation."""

    kind: str  # "prev_hash_mismatch" | "seq_gap" | "missing_seq" | "malformed"
    line_no: int
    seq: int | None
    expected_prev_hash: str | None
    actual_prev_hash: str | None
    detail: str


@dataclass(frozen=True)
class VerifyReport:
    """Result of walking the activity log."""

    file_path: str
    total_lines: int
    chained_records: int
    legacy_records: int  # pre-feature records (no seq) skipped in default mode
    breaks: tuple[VerifyBreak, ...]
    ok: bool

    @property
    def summary(self) -> str:
        if self.ok:
            return (
                f"verified {self.chained_records} chained record(s) ({self.legacy_records} pre-chain record(s) skipped)"
            )
        first = self.breaks[0]
        return f"chain break at seq {first.seq} (line {first.line_no}): {first.kind}"


def _iter_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield ``(line_no, stripped_line)`` for non-empty lines (1-indexed)."""
    with path.open("r", encoding="utf-8") as handle:
        for idx, raw in enumerate(handle, 1):
            stripped = raw.rstrip("\n")
            if stripped:
                yield idx, stripped


def verify_chain(
    path: Path,
    *,
    from_seq: int | None = None,
    strict: bool = False,
) -> VerifyReport:
    """Walk ``path`` and validate the chain link by link.

    ``from_seq`` skips records with seq < from_seq (lets operators verify
    only post-rotation windows). ``strict`` rejects any pre-feature record
    (missing ``seq``) instead of skipping it; useful for compliance audits
    that demand a fully chained log.
    """
    if not path.exists():
        return VerifyReport(str(path), 0, 0, 0, (), True)

    breaks: list[VerifyBreak] = []
    chained = 0
    legacy = 0
    total = 0
    prev_seq = 0
    prev_hash: str | None = None
    chain_started = False

    for line_no, line in _iter_lines(path):
        total += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            breaks.append(
                VerifyBreak(
                    kind="malformed",
                    line_no=line_no,
                    seq=None,
                    expected_prev_hash=None,
                    actual_prev_hash=None,
                    detail=f"json decode failed: {exc.msg}",
                )
            )
            continue
        if not isinstance(record, dict):
            breaks.append(
                VerifyBreak(
                    kind="malformed",
                    line_no=line_no,
                    seq=None,
                    expected_prev_hash=None,
                    actual_prev_hash=None,
                    detail="record is not a JSON object",
                )
            )
            continue

        seq = record.get("seq")
        record_prev_hash = record.get("prev_hash")

        if not isinstance(seq, int):
            if strict:
                breaks.append(
                    VerifyBreak(
                        kind="missing_seq",
                        line_no=line_no,
                        seq=None,
                        expected_prev_hash=None,
                        actual_prev_hash=None,
                        detail="pre-feature record rejected under --strict",
                    )
                )
            else:
                legacy += 1
            continue

        if from_seq is not None and seq < from_seq:
            continue

        if not chain_started:
            # First chained record this walk sees.
            chain_started = True
            prev_seq = seq
            prev_hash = _hash_line(line)
            chained += 1
            continue

        expected_seq = prev_seq + 1
        if seq != expected_seq:
            breaks.append(
                VerifyBreak(
                    kind="seq_gap",
                    line_no=line_no,
                    seq=seq,
                    expected_prev_hash=None,
                    actual_prev_hash=None,
                    detail=f"expected seq {expected_seq}, got {seq}",
                )
            )
            # Re-anchor so the rest of the file can still verify against
            # whatever sequence is now in front of us.
            prev_seq = seq
            prev_hash = _hash_line(line)
            chained += 1
            continue

        actual_prev = record_prev_hash if isinstance(record_prev_hash, str) else None
        if actual_prev != prev_hash:
            breaks.append(
                VerifyBreak(
                    kind="prev_hash_mismatch",
                    line_no=line_no,
                    seq=seq,
                    expected_prev_hash=prev_hash,
                    actual_prev_hash=actual_prev,
                    detail=(f"expected prev_hash={_short(prev_hash)} got={_short(actual_prev)}"),
                )
            )

        prev_seq = seq
        prev_hash = _hash_line(line)
        chained += 1

    return VerifyReport(
        file_path=str(path),
        total_lines=total,
        chained_records=chained,
        legacy_records=legacy,
        breaks=tuple(breaks),
        ok=not breaks,
    )
