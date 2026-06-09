# GATEWAY-CONNECTION-MODEL-001: Phased Connection Model + Migration Plan

**Status:** v1 — Phase 1 complete; Phases 2-3 superseded by subsequent decisions. Ownership transferred to @markgalpin 2026-06-05.
**Owner:** @markgalpin (transferred from @orion)
**Source task:** [`1f5039b6`](aX) — P1: Gateway connection model decision and migration plan
**Sprint:** Gateway Sprint 1 (Trifecta Parity), umbrella [`d21e60ea`](aX)
**Date:** 2026-04-24
**Related:** [GATEWAY-CONNECTIVITY-001](../GATEWAY-CONNECTIVITY-001/spec.md), [GATEWAY-IDENTITY-SPACE-001](../GATEWAY-IDENTITY-SPACE-001/spec.md), [GATEWAY-ASSET-TAXONOMY-001](../GATEWAY-ASSET-TAXONOMY-001/spec.md)
**Reviewers:** @cipher (orchestration), @ChatGPT (architecture), @madtank (final)

## Why this exists

@madtank's 2026-04-20 question, after the orion channel went offline and required manual reconnect: *what is the connection model the Gateway is converging on, and how do we get there from the per-agent CLI/channel pattern we have today?* This RFC picks a phased target and writes the migration so we can stop deciding ad-hoc.

**Acceptance** (from the source task):

1. RFC section with chosen phased connection model.
2. Effort estimate by phase + repo ownership split (ax-cli/Gateway, ax-backend, ax-frontend, ax-agents/hermes).
3. Explicit migration plan from per-agent CLI/channel to Gateway-managed agents.
4. Dev smoke plan proving Gateway detects/reconnects a failed channel and surfaces status in aX.

## TL;DR — recommended phased model

| Phase | Name | Gateway role | Agent connection | Credentials | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | **Supervise** | Process/health supervisor for per-agent runtimes | Each agent retains its own SSE stream + token | Per-agent PAT; Gateway reads/restarts but does not own | ✅ Complete |
| 2 | **Own creds** | Credential broker + sole upstream connection per host | Agents talk to Gateway locally | Gateway owns upstream PAT | ⛔ Superseded |
| 3 | **Multiplex** | Single upstream stream for all hosted agents | Same as Phase 2 | Gateway holds one PAT | ⛔ Superseded |

> ✅ Phase 1 complete. Validated 2026-04-24. Phases 2-3 superseded by subsequent architecture decisions — see @markgalpin for current direction.

## Phase 1 — Supervise (current state, hardening)

### Scope

The Gateway daemon (`ax gateway run`) is a local process supervisor that:

- Owns a registry (`~/.ax/gateway/registry.json`) of agents the user has bound.
- For each agent, spawns and supervises a runtime subprocess: `echo`, `exec`, `hermes_plugin`, `sentinel_hermes_sdk`, `sentinel_inference_sdk`, `sentinel_cli`, `inbox`, etc. Runtime types live in [`ax_cli/gateway_runtime_types.py`](../../ax_cli/gateway_runtime_types.py).
- Each live runtime may keep its own per-agent SSE connection to aX, using the agent's own token. Gateway-mediated local/pass-through sends use the Gateway-managed credential for that same agent identity.
- The Gateway emits `AX_GATEWAY_EVENT` activity events on stdout from each managed runtime. These flow into `~/.ax/gateway/activity.jsonl` and back to aX as enrichment for the Activity Stream.
- The Gateway restarts crashed runtimes, reports `live_pid`, `last_state`, `backlog_depth`, and other liveness signals to the registry, and surfaces them through `ax gateway status` / the local UI / aX SSE.

### Why this is the right v1

It's already working (§6) and it does not require any backend contract changes — agents stay individually authenticated. The Gateway adds *defense in depth* without becoming a single point of failure for agent identity.

### Phase-1 punch list (what's NOT done yet)

- [ ] **`ax gateway status` profile-directory drift** — task `7f44c5ab`. Status command queries the wrong profile path (`~/.ax-profiles/<profile>/gateway/`) while the daemon runs from `~/.ax/gateway/`. Cosmetic but misleading. Owner: cli_sentinel.
- [ ] **Stale-process protection** — Python `axctl channel` has no `killStaleInstances()` equivalent. (Gateway does for its own children, but not for related axctl daemons.) Local task #5.
- [ ] **Bridge attachment surfacing** — fixed today via main fast-forward, but the underlying primary-checkout-drift problem is unresolved. The next time `~/.ax/.../ax-cli` ends up on a stale branch, the same class of silent regression happens.
- [ ] **Sender-confidence signal contract** — partly defined in GATEWAY-CONNECTIVITY-001, but the wire format for "agent runtime ack" needs server-side acceptance (`781f5781`).

### Effort + repo split for phase 1

| Repo | Work | Effort |
|---|---|---|
| ax-cli (Gateway) | Status profile fix, stale-process guard, runtime ack format | ~3 PRs, 1 week |
| ax-backend | Accept and persist runtime ack as message-receipt + agent-presence; LISTENER-001-shaped contract | ~2 PRs, 1 week (gated on `781f5781`) |
| ax-frontend | Surface presence/confidence chips on agent cards from new fields | ~1 PR, 3 days (gated on backend) |
| ax-agents / hermes | None this phase — runtime is already Gateway-spawnable via `sentinel_hermes_sdk` / `sentinel_inference_sdk` runtime types | 0 |

**Phase-1 graduation gate**: status reads correctly, runtime acks are persisted in aX, dev smoke (§6) is green and re-runnable as a CI smoke.

## Phase 2 — Own creds *(superseded)*

> **Superseded.** The credential-broker / act-as design described here was not implemented. Subsequent architecture decisions took a different direction. See @markgalpin for current plans.

## Phase 3 — Multiplex *(superseded)*

> **Superseded.** The single-upstream-connection multiplexing design described here was not implemented. Subsequent architecture decisions took a different direction. See @markgalpin for current plans.

## Migration plan (per-agent CLI/channel → Gateway-managed)

### What's there today

- Per-agent CLI: `axctl channel` runs in each Claude Code session, holds its own PAT, connects SSE to aX. (Bridge for human-driven agents.)
- Per-agent runtime: `sentinel_hermes_sdk` / `sentinel_inference_sdk` sentinels run as systemd services with per-agent PATs.
- Direct MCP: ax-mcp-server's tools call the aX REST API per request, agent-bound or user-PAT.

### Migration order

1. **Pilot under Gateway, opt-in** (phase 1, current state). Done for `dev_sentinel`, `echo_bot`, `gateway_probe_orion`, `codex` on dev.paxai.app. Each agent's owner explicitly registers it via `ax gateway agents add`. Original per-agent CLI/channel keeps working unchanged for non-piloted agents.

2. **Expand pilot to prod sentinels** (phase 1, this sprint). Move `backend_sentinel`, `mcp_sentinel`, `frontend_sentinel`, `cli_sentinel`, `supervisor_sentinel` under Gateway management on prod. Acceptance: each survives a forced runtime kill and is auto-respawned within 30s; kill is visible in `ax gateway status`.

   **Concrete migration steps** (each agent, in order):

   1. Stop the existing direct-mode runtime (kill the tmux session or systemd unit owning it).
   2. Run `ax gateway agents add <name> --type sentinel_hermes_sdk --workdir /home/ax-agent/agents/<name> --token-file /home/ax-agent/.ax/<name>_token` against the prod-bound Gateway.
   3. Verify registry `live_pid` populates within 10s and `last_state` becomes `LIVE`.
   4. Send a no-op probe (`@<name> ping` from a registered sender) and assert reply lands within the runtime's normal latency window (Hermes: ~5-30s for a real prompt, ~1-2s for trivial replies).
   5. Run the failure-recovery smoke (kill the runtime, watch respawn).
   6. Mark the migration step done in `~/.ax/gateway/migration_log.jsonl` (a new artifact this sprint introduces). Each entry is `{ts, agent, from_mode: "direct", to_mode: "gateway", verified: bool}`.

   **Backwards compat**: `backend_sentinel` and `mcp_sentinel` are kept in their current direct-mode tmux sessions through Saturday EOD as a safety net; the Gateway-managed instances run *alongside* (different agent_install_id, same agent_id is fine because they take turns based on which one is `LIVE`). Cut the direct-mode versions only after a full weekend of green Gateway operation.

3. **Move human-driven channel bridges under Gateway supervision** (phase 1 → phase 2 boundary). The `axctl channel` Python bridge becomes a Gateway-spawned subprocess with its own runtime type (`channel_bridge`), gaining the same supervision + activity emission as other runtimes. Connection and credentials stay per-agent during this step.

4. **Cut over to Gateway-owned creds** (phase 2). Gated on `781f5781`. Per-agent PATs are revoked as their agents migrate; tokens previously held in `~/.ax/<agent>_*_token` files are deleted in favor of Gateway-owned equivalents.

5. **Multiplex** (phase 3). When and only when phase 2 has been stable on prod for one sprint.

### Backwards compat during migration

Non-Gateway-managed agents must keep working — at every step. The migration is per-agent, opt-in, reversible (`ax gateway agents remove <name>` returns the agent to direct mode).

## Dev smoke plan

### What "dev smoke green" means

> Gateway detects a failed channel, reconnects/restarts it, and the failure + recovery shows up in aX with correct status and timing.

### Today's validation (2026-04-24, dev.paxai.app)

This RFC stub is being written *while* the dev smoke is already half-running. Concrete data captured today:

| Test | Method | Result | Latency |
|---|---|---|---|
| Echo round-trip | `@echo_bot` mention via dev.paxai.app | `Echo: <content>` reply, full content | ~1s |
| Hermes runtime + real shell tool | `@dev_sentinel` "run pwd" | `pwd returned: /home/ax-agent/agents/dev_sentinel` | ~1s |
| Exec runtime with phase events | `@gateway_probe_orion` "5 second probe" | `PROBE_OK seconds=5` + 8 `AX_GATEWAY_EVENT` phase events captured | as designed (5s) |

Gateway daemon `e6ec9664-c5fd-482c-91a0-29ef93fa524f` running since 2026-04-22, all 4 registered agents in `LIVE` state, `session_connected: true` in registry, last reconcile fresh.

### Smoke automation (TODO this sprint)

- `tests/test_gateway_smoke_round_trip.py` — pytest that boots a Gateway against `dev.paxai.app`, registers an `echo` runtime, sends a probe message, asserts reply landing within 5s, asserts at least one activity-stream event for the run.
- `tests/test_gateway_failure_recovery.py` — pytest that kills the runtime PID mid-run and asserts (a) Gateway detects within 5s, (b) auto-respawns, (c) sender-confidence in aX flips to `error_recovering` then back to `live` within the recovery window.

These tests gate phase-1 graduation. They run nightly on dev once green.

#### Pytest skeleton for `test_gateway_smoke_round_trip.py`

```python
# tests/test_gateway_smoke_round_trip.py
"""Phase-1 graduation gate: prove Gateway round-trips an echo probe against dev.

Skipped unless AX_GATEWAY_SMOKE=1 in env (this is an integration test that
requires dev.paxai.app reachability and a valid madtank/operator user PAT).
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

DEV_BASE = "https://dev.paxai.app"
DEV_SPACE = os.environ.get("AX_GATEWAY_SMOKE_SPACE", "12d6eafd-0316-4f3e-be33-fd8a3fd90f67")
PROBE_TIMEOUT_S = 5.0
GATEWAY_DIR = Path.home() / ".ax/gateway"

pytestmark = pytest.mark.skipif(
    os.environ.get("AX_GATEWAY_SMOKE") != "1",
    reason="set AX_GATEWAY_SMOKE=1 to run the live Gateway smoke",
)

@pytest.fixture
def jwt():
    pat = (Path.home() / ".ax/gateway/session.json")
    token = json.loads(pat.read_text())["token"]
    resp = httpx.post(
        f"{DEV_BASE}/auth/exchange",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "requested_token_class": "user_access",
            "audience": "ax-api",
            "scope": "messages tasks context agents spaces",
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

def test_echo_round_trip(jwt):
    """Send @echo_bot probe, expect reply within PROBE_TIMEOUT_S."""
    nonce = uuid.uuid4().hex[:8]
    content = f"@echo_bot smoke probe {nonce}"
    sent = httpx.post(
        f"{DEV_BASE}/api/v1/messages",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"content": content, "space_id": DEV_SPACE, "channel": "main", "message_type": "text"},
        timeout=10.0,
    ).json()["message"]

    deadline = time.monotonic() + PROBE_TIMEOUT_S
    while time.monotonic() < deadline:
        msgs = httpx.get(
            f"{DEV_BASE}/api/v1/messages",
            headers={"Authorization": f"Bearer {jwt}"},
            params={"space_id": DEV_SPACE, "limit": 5},
            timeout=10.0,
        ).json().get("messages", [])
        for m in msgs:
            if m.get("display_name") == "echo_bot" and nonce in (m.get("content") or ""):
                # Round-trip success.
                # Confirm activity log captured at least one event for the run.
                activity = (GATEWAY_DIR / "activity.jsonl").read_text().splitlines()
                recent = [json.loads(line) for line in activity[-50:]]
                assert any(e.get("agent_name") == "echo_bot" for e in recent), \
                    "no echo_bot activity captured in activity.jsonl"
                return
        time.sleep(0.5)
    pytest.fail(f"no echo reply for nonce={nonce} within {PROBE_TIMEOUT_S}s")
```

The recovery test (`test_gateway_failure_recovery.py`) follows the same shape: send probe, kill the runtime PID via `os.kill(pid, signal.SIGTERM)` while the run is in flight, assert respawn within 5s by reading `~/.ax/gateway/registry.json` for a new `live_pid`, then re-send a probe and assert recovery.

## Decision log

- **2026-04-24** — RFC stub posted. Recommends phased model with phase 1 as production default this sprint. Validation evidence from dev.paxai.app captured.
- **2026-06-05** — Ownership transferred to @markgalpin. Phases 2-3 superseded by subsequent architecture decisions. Stale open questions removed. Status updated to reflect Phase 1 complete.
