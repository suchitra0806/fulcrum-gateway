# ADR-008: Agent Status Model — Operator Intent, Liveness Escalation, and UI Tone

**Status:** Accepted — implemented and verified against this codebase 2026-06-10

**Depends on:** [ADR-007: Agent Classes and Gateway Signaling Contract](ADR-007-agent-classes-and-signals.md)

![Gateway status architecture](../images/gateway-status-architecture.svg)

[ADR-007](ADR-007-agent-classes-and-signals.md) defines the left boundary:
what each agent class is responsible for reporting to the Gateway registry.
This ADR (ADR-008) defines the right boundary: how the daemon translates those
signals into operator-visible status.

## Context

The Gateway UI and CLI need to communicate agent health to operators clearly
and consistently across all agent classes defined in ADR-007. Early
implementations derived display state from observed runtime signals alone —
presence, heartbeat age, connection flags — without a consistent semantic
model. This produced several problems:

- A `claude_code_channel` agent with a dead SSE subscription showed GREEN
  because MCP pings kept the heartbeat fresh and the process PID was alive.
- An agent the operator had stopped showed the same gray as an agent that had
  crashed — "intentionally off" and "broken" were visually indistinguishable.
- An agent that had been unreachable for five minutes looked the same as one
  that had missed a single heartbeat thirty seconds ago.
- Agent-type-specific branches in the UI accumulated over time, making it
  harder to reason about how new agent types would render.

The canonical current status contract — operator intent priority, UI tones,
liveness thresholds, and field mapping — is defined in
[GATEWAY-CONNECTIVITY-001](../../specs/GATEWAY-CONNECTIVITY-001/spec.md)
(Daemon-to-UI Status Contract section). This ADR records the decisions that
shaped that contract.

## Decision

### 1. Operator intent is evaluated before any health signal

Three registry fields express operator intent and are checked in priority order
before any runtime health signal:

1. `lifecycle_phase == "hidden"` or `lifecycle_phase == "archived"` → gray
   immediately, regardless of observed runtime state. Operators hide or archive
   agents to explicitly remove them from active operation — a common reason is
   that the agent did not record its shutdown correctly and still appears yellow
   or red. The operator's decision overrides the signal.
2. `desired_state == "stopped"` AND `connected == false` → gray ("Stopped")
3. `desired_state == "stopped"` AND `connected == true` → yellow ("Stopping",
   transition in progress)
4. `desired_state == "running"` → all subsequent health checks apply

All agent classes defined in ADR-007 pass through these checks before any
class-specific logic.

### 2. Four tones with explicit semantic boundaries

| Tone | Color | Meaning |
| --- | --- | --- |
| `muted` | gray | Operator has intentionally taken this agent out of active operation: stopped, hidden, or archived. |
| `warning` | yellow | Agent needs attention: transitioning, pending approval, or degraded. |
| `error` | red | Agent is desired=running but cannot function. |
| `ok` | green | Agent is healthy and ready. |

Gray is reserved exclusively for intentional-off states. Any agent that is
desired=running but not working correctly renders red, not gray.

### 3. Two-threshold liveness escalation in the daemon sweep

The Gateway daemon sweep (`_derive_liveness` in `ax_cli/gateway_state.py`)
applies two thresholds to the age of the freshest registry signal
(`last_seen_at`):

- **75 seconds** without a registry signal → `liveness = "stale"` → yellow
- **300 seconds** without a registry signal → `liveness = "offline"` → red
  (for LIVE mode agents)

The thresholds derive from the 30-second registry signal cadence
(`RUNTIME_HEARTBEAT_INTERVAL_SECONDS`) that every first-party writer shares:
75s = 2.5 beats (absorbs one missed beat plus an SSE reconnect window —
the 45s idle timeout plus backoff — without flapping yellow), 300s = 10 beats.
They are deliberately **not** derived from the platform heartbeat channel
(ADR-009): the gateway never sees platform heartbeats — it observes the
registry signals agents write locally.

This escalation runs in the daemon and writes to the registry. The UI reads
the pre-computed `liveness` and `presence` fields — no time-based logic in
JavaScript.

**Escalation scope — which classes actually escalate.** The yellow-then-red
ladder semantically means "may self-heal, then needs the operator." It applies
to entries whose raw state remains `running` while signals age — i.e.
daemon-managed runtimes whose supervisor believes them alive (a wedged
listener loop). The other classes resolve differently, by design:

- **Attached sessions** cannot self-heal (the daemon cannot restart them), so
  they skip the ladder: the sweep resolves signal absence at the 75s threshold
  and the UI renders red immediately via `reachability` —
  `sse_disconnected` (process alive, SSE dead) or `attach_required` (process
  gone). Waiting 300s for red here would delay the operator for no benefit.
- **External plugins** similarly pin at raw `stale` and render via the
  `externalManaged` check (gap documented in ADR-007).
- **Polling mailbox and on-demand agents** are exempt entirely via the
  INBOX/ON-DEMAND mode bypass below.

### 4. OFFLINE presence is meaningful only for LIVE mode agents

`_derive_presence()` maps `liveness = "offline"` to presence `OFFLINE` only
when `mode == "LIVE"`. For INBOX and ON-DEMAND agents, availability is defined
by queue access or launch capability — not an active connection — so offline
liveness falls through to `IDLE`. This preserves the semantic accuracy of the
presence field: OFFLINE means "was supposed to be connected and isn't", which
only applies to always-on listeners.

### 5. Gateway computes health; UI reads it

Health state (liveness, presence, confidence, reachability) is computed by the
Gateway daemon sweep and stored in the registry. The UI renders whatever the
daemon computed. This keeps class-specific logic in one place and makes new
agent classes automatically compatible with the status model as long as they
follow the signaling contract in ADR-007.

## Known Gaps

- One case in the status table requires a class-specific check in the UI —
  marked *(gap)* in the table below. This is a consequence of the Gateway not
  yet computing a fully generic semantic field for that case. The root cause
  and discussion of a possible future improvement is documented in
  [ADR-007 § Known Gaps](ADR-007-agent-classes-and-signals.md#known-gaps).
- The escalation ladder is defeated for daemon-supervised sentinel
  subprocesses: the monitor thread stamps `last_seen_at` from PID existence,
  so an alive-but-wedged sentinel never ages into stale/offline. Tracked in
  [#295](https://github.com/FulcrumDefense/fulcrum-gateway/issues/295); the
  fix direction is to let the sentinel's adapter heartbeats carry freshness
  and keep the PID poll for exit detection only, which would make the ladder
  apply uniformly to all raw-`running` entries.

## Consequences

- **Positive:** Operators get consistent, predictable status signals across all
  agent classes. Gray means "I stopped this." Red means "this is broken."
- **Positive:** New agent classes require no UI changes to render correctly,
  provided they follow the signaling contract in ADR-007.
- **Positive:** Transient heartbeat gaps show yellow before escalating to red,
  reducing false alarms.
- **Positive:** The `desired_state` / `lifecycle_phase` checks eliminate
  separate "stopped" and "hidden" branches in class-specific code paths.
- **Breaking:** the sweep's auto-hide path (hide stale agents after a
  threshold, auto-restore on reconnect) is removed by this decision — a
  system that sets `lifecycle_phase=hidden` on its own would make gray mean
  "broken for a while" instead of operator intent. Hide and unhide are
  operator-only operations.
- **Negative:** The 300-second escalation threshold is fixed. Agents with
  legitimately long quiet periods between heartbeats may incorrectly escalate
  — though INBOX and ON-DEMAND agents are protected by the LIVE-mode-only
  OFFLINE rule.
- **Negative:** The two thresholds (75s, 300s) are not yet configurable per
  agent class. They are conservative defaults intended to work generically.

## Notes

### "Attached session" in UI messages

UI labels and detail strings use the term "attached session" rather than naming
a specific runtime (e.g. "Claude Code"). This refers to the **Attached Session**
agent class defined in ADR-007 — currently `claude_code_channel`, but
intentionally generic to accommodate future agent types in that class. Operators
who see "The attached session is not running" should consult the Gateway UI or
`ax gateway agents doctor` to identify which specific runtime is affected.

### `sse_connected` field

The `sse_connected` field and its role in health derivation are defined in
[GATEWAY-AGENT-REGISTRY-001](../../specs/GATEWAY-AGENT-REGISTRY-001/spec.md).
The decision to introduce it as a separate field (rather than using
`effective_state`) is documented in ADR-007.

### Status mapping before and after this ADR

| Condition | Gateway signal | UI class check | Label (before) | Tone (before) | Label (after) | Tone (after) | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `lifecycle_phase == "hidden"` | `lifecycle_phase` | generic | *(not handled)* | *(live dot shown)* | "Hidden" | gray | New — operator intent override |
| `lifecycle_phase == "archived"` | `lifecycle_phase` | generic | *(not handled)* | *(live dot shown)* | "Archived" | gray | New — operator intent override |
| External plugin, `desired=stopped`, actually stopped | `desired_state`, `connected` | generic | "Plugin stopped" | yellow | "Stopped" | gray | Stopped = gray |
| External plugin, `desired=stopped`, still running | `desired_state`, `connected` | generic | "Plugin stopping" | yellow | "Stopping" | yellow | Unchanged |
| External plugin, `desired=running`, not connected | `external_runtime_managed`, `connected` | `externalManaged` *(gap)* | "Plugin not attached" | yellow | "Plugin not attached" | red | Red — desired=running but broken |
| External plugin, `desired=running`, connected | *(falls through)* | generic | *(falls through)* | green | *(falls through)* | green | Unchanged — treated as active |
| `desired=stopped`, `connected=false` | `desired_state`, `connected` | generic | "Stopped" | gray | "Stopped" | gray | Unchanged |
| `desired=stopped`, `connected=true` | `desired_state`, `connected` | generic | "Stopped" | gray | "Stopping" | yellow | New — transition shown as yellow |
| Approval pending | `approval_state` | generic | "Needs approval" | yellow | "Needs approval" | yellow | Unchanged |
| Approval rejected | `approval_state` | generic | "Rejected" | red | "Rejected" | red | Unchanged |
| Setup error | `presence`, `confidence_reason` | generic | "Setup error" | red | "Setup error" | red | Unchanged |
| `BLOCKED` + binding drift | `confidence`, `confidence_reason` | generic | "Needs approval" | yellow | "Needs approval" | yellow | Unchanged |
| `BLOCKED` (other) | `confidence` | generic | "Blocked" | yellow | "Blocked" | red | Red — gateway blocking = broken |
| Attach in progress | `current_status`, `connected` | generic | "Starting" | yellow | "Starting" | yellow | Unchanged |
| Mailbox with pending work | `backlog_depth` / `queue_depth` | `isMailboxRuntime` (uses `mode=INBOX`) | "N messages" | yellow | "N messages" | yellow | Unchanged |
| Mailbox idle | *(no specific signal)* | `isMailboxRuntime` (uses `mode=INBOX`) | "Inbox" | gray | "Inbox" | green | Green — healthy passive state |
| Attached + SSE disconnected | `reachability=sse_disconnected` | generic | "SSE down" | red | "SSE down" | red | Unchanged |
| Attached runtime + `presence=STALE` | `reachability=attach_required` | generic | "Stopped" | gray | "Not running" | red | Red — process gone, new label |
| `presence=STALE` (other runtimes) | `presence` (from `liveness`) | generic | "Stale" | yellow | "Stale" | yellow | Unchanged |
| `presence=OFFLINE` | `presence` (from `liveness`) | generic | "Offline" | gray | "Offline" | red | Red — desired=running but unreachable |
| `connected=true` | `connected` | generic | "Active" | green | "Active" | green | Unchanged |
| `confidence=MEDIUM`, `launch_available` | `confidence`, `confidence_reason` | generic | "Ready" | green | "Ready" | green | Unchanged |
| `confidence=HIGH` | `confidence` | generic | "Ready" | green | "Ready" | green | Unchanged |
| `presence=IDLE` | `presence` | generic | "Idle" | gray | "Idle" | green | Green — connected, healthy, quiet |
| Fallback (unrecognised state) | *(none)* | generic | "Idle" | gray | "Unknown" | yellow | Yellow — needs attention |
