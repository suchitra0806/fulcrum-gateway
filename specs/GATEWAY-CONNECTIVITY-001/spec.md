# GATEWAY-CONNECTIVITY-001: Gateway Connectivity, Signal Model, and Sender Confidence

**Status:** Draft — sections added 2026-06-10: *Daemon-to-UI Status Contract* (operator intent priority, UI tone semantics, reachability values, liveness escalation thresholds). Derivation pseudocode and confidence reason codes reconciled against the implementation (`ax_cli/gateway_state.py`) the same day.  
**Owner:** @markgalpin (transferred from @madtank, 2026-06-03)  
**Date:** 2026-04-22  
**Related:** [LISTENER-001](../LISTENER-001/spec.md), [AGENT-CONTACT-001](../AGENT-CONTACT-001/spec.md), [MESH-SPAWN-001](../MESH-SPAWN-001/spec.md), [HEARTBEAT-001](../HEARTBEAT-001/spec.md) (source of the 30s registry signal cadence this spec's thresholds derive from), [GATEWAY-AGENT-REGISTRY-001](../GATEWAY-AGENT-REGISTRY-001/spec.md), [ADR-008](../../docs/adr/ADR-008-agent-status-model.md) (daemon-to-UI status contract decision), [docs/mcp-remote-oauth.md](../../docs/mcp-remote-oauth.md)  
**Companion Mockups:** [mockups.md](mockups.md)

## Purpose

Define the v1 contract for the local Gateway as the execution and control plane
between aX and managed runtimes. The goal is to make agent connectivity,
pickup, progress, and reply expectations explicit enough that:

1. senders trust that a message really reached the Gateway,
2. operators can tell whether an agent is live, on-demand, inbox-backed, or in
   error,
3. aX can render durable sender confidence without depending on transient SSE
   alone, and
4. later OAuth, MCP Gateway mode, and single-pipe Gateway ownership can replace
   transport details without renaming the core model.

The product must never use `running` as the primary status. User-facing state
is always expressed as **Mode + Presence + Reply behavior + Confidence**,
derived from a more precise internal model.

## Goals

- Make the Gateway the local source of truth for delivery, queueing, claim,
  progress, and completion semantics.
- Distinguish persistent live agents from on-demand, inbox, and attached
  agents.
- Preserve rich telemetry where adapters can provide it, while still making
  sparse runtimes trustworthy.
- Persist both **latest status snapshots** and **recent event timelines** in aX
  for fleet views, drill-ins, and sender activity bubbles.
- Keep user PAT bootstrap acceptable for v1 while making OAuth a login-provider
  swap rather than a runtime-contract rewrite.
- Make it almost impossible for a user to misunderstand whether an agent is
  listening live, queue-backed, on-demand, stale, or blocked.

## Non-goals

- Exact-once delivery.
- Direct child-runtime authentication to aX.
- Skill Gateway in v1.
- Cross-machine HA Gateway.
- Arbitrary third-party template marketplace.
- Hard service-level guarantees.
- Production OAuth requirement in v1.
- Direct shipping from `main` as an implementation constraint.

## Product Questions Answered

### 1. How does a user connect agents to the platform?

Users connect agents through Gateway-owned templates, not raw runtime backends.
The first visible starter kit is:

- `Echo (Test)`
- `Hermes`
- `Pass-through`
- `Claude Code Channel`
- `Ollama`

`Inbox / Background Worker` is fully specified but advanced in v1. The product
should also name this clearly as an **inbox-backed agent** pattern so it is not
mistaken for a broken or disconnected live agent.

`Pass-through` is the user-facing mailbox identity for agents that check
Gateway when available instead of listening live. It is distinct from both live
listeners and background workers; see **[GATEWAY-PASS-THROUGH-MAILBOX-001](../GATEWAY-PASS-THROUGH-MAILBOX-001/spec.md)** for
the approval, fingerprint, mailbox count, and last-activity contract.

The user picks a template, sees its reply behavior and expected signals, runs a
Gateway-authored smoke test, and then sees the resulting mode, presence,
typical timing, and last outcome in the fleet view.

### 2. How does Gateway know whether an agent is really reachable?

Gateway tracks six immutable internal dimensions:

- `placement`
- `activation`
- `liveness`
- `work_state`
- `reply_mode`
- `telemetry_level`

The operator never sees `running` directly. The operator sees:

- `mode`
- `presence`
- `reply`
- `confidence`

And the UI may also surface:

- `reachability`

Gateway is an **agent-operable control plane**. The UI is a human-readable
surface over that control plane, but lifecycle, doctor, send, and approval
actions must also be available through stable CLI and local API paths so
agents can operate Gateway without UI-only dependencies.

### 3. How does aX learn what is happening?

Gateway emits append-only lifecycle events to aX and also updates two derived
snapshots:

- `AgentStatusSnapshot` keyed by `agent_id` for fleet and drill-in views
- `InvocationStatusSnapshot` keyed by `invocation_id` and indexed by
  `message_id + agent_id` for message bubbles and recent activity

The timeline is the history. The snapshot is the latest truth.

### 4. How does the sender know what to expect?

Expectation is shown both **before** sending and **after** sending:

- Pre-send: Mode, Presence, Reply behavior, telemetry richness, and current
  confidence.
- Post-send: compact activity bubble phases such as `Received by Gateway`,
  `Claimed by runtime`, `Working`, `Summary pending`, or `No reply expected`.

## Topology and Upstream Model

### v1 Hybrid Upstream Model

v1 keeps the current hybrid pattern:

- Gateway stores one bootstrap user credential for management/login.
- Gateway mints and stores per-agent Gateway-managed credentials.
- Each managed agent may still hold its own upstream listen/send relationship to
  aX through Gateway's supervision.
- Child runtimes do not receive user PATs or raw platform JWTs.

This is acceptable for v1 because the product semantics are normalized through
Gateway even if the upstream transport is still per-agent.

### Later Single-Pipe Direction

The protocol defined here must also support a later model where:

- aX sees the Gateway as the authoritative connected object,
- Gateway owns the single upstream control relationship,
- all managed agents exist behind Gateway, and
- child runtimes never authenticate directly to aX.

The lifecycle event names and status model in this spec must remain valid under
both topologies.

## Internal Model

### Internal Fields

| Field | Values | Meaning |
| --- | --- | --- |
| `placement` | `hosted`, `attached`, `brokered`, `mailbox` | Where the runtime actually lives relative to Gateway |
| `activation` | `persistent`, `on_demand`, `attach_only`, `queue_worker` | How the runtime becomes active |
| `liveness` | `connected`, `stale`, `offline`, `setup_error` | Health of the active execution path |
| `work_state` | `idle`, `queued`, `working`, `blocked` | Current work ownership |
| `reply_mode` | `interactive`, `background`, `summary_only`, `silent` | Expected outcome behavior |
| `telemetry_level` | `rich`, `basic`, `silent` | Signal richness the adapter can provide |

### Internal Semantics

- `placement=hosted` means Gateway owns the runtime process or launch path.
- `placement=attached` means the runtime lives elsewhere, but Gateway has a
  live session/claim path to it.
- `placement=brokered` means Gateway invokes an external client or service on
  demand and no persistent listener is expected.
- `placement=mailbox` means Gateway accepts durable work but there may be no
  live runtime attached.

- `activation=persistent` means the runtime is expected to stay live.
- `activation=on_demand` means Gateway launches or invokes it per task.
- `activation=attach_only` means Gateway supervises an already-existing session
  but does not own the lifecycle.
- `activation=queue_worker` means work is first queued durably and later
  claimed by a worker.

## Derived Operator Model

### Operator Fields

| Field | Values | Meaning |
| --- | --- | --- |
| `mode` | `LIVE`, `ON-DEMAND`, `INBOX` | What kind of connectivity the user should assume |
| `presence` | `IDLE`, `QUEUED`, `WORKING`, `BLOCKED`, `STALE`, `OFFLINE`, `ERROR` | Current operational truth |
| `reply` | `REPLY`, `SUMMARY`, `SILENT` | What sort of result the sender should expect |
| `confidence` | `HIGH`, `MEDIUM`, `LOW`, `BLOCKED` | How safe it is to send work through this path right now |

`BROKERED` remains an internal placement detail in v1 and maps to
`mode=ON-DEMAND` for user-facing UX.

### Reachability Helper

In addition to the primary operator fields, the product should derive a
human-readable `reachability` helper for wizard copy, composer expectations,
and drill-ins:

- `live_now`
- `queue_available`
- `launch_available`
- `attach_required`
- `sse_disconnected`
- `unavailable`

This is explanatory text, not a primary fleet chip.

### Deterministic Derivation Rules

Implementations must use the same precedence rules everywhere.

#### Mode derivation

```text
if placement == mailbox:
  mode = INBOX
else if activation in {persistent, attach_only}:
  mode = LIVE
else if activation == on_demand:
  mode = ON-DEMAND
else if placement == brokered:
  mode = ON-DEMAND
else:
  mode = ON-DEMAND
```

#### Presence derivation

```text
if liveness == setup_error:
  presence = ERROR
else if work_state == blocked:
  presence = BLOCKED
else if liveness == stale:
  presence = STALE
else if liveness == offline and mode == LIVE:
  presence = OFFLINE
else if work_state == working:
  presence = WORKING
else if work_state == queued:
  presence = QUEUED
else:
  presence = IDLE
```

Signal freshness outranks work claims: a runtime whose registry signals have
gone stale renders `STALE` even if its last write claimed `working` — the
work claim is exactly as old as the stale signal, so it cannot be trusted
over it. `OFFLINE` applies only to LIVE mode agents; INBOX and ON-DEMAND
agents fall through to work-state evaluation and ultimately `IDLE`, because
their availability is defined by queue access or launch capability, not a
persistent connection (see Daemon-to-UI Status Contract below).

#### Reply derivation

```text
interactive -> REPLY
background or summary_only -> SUMMARY
silent -> SILENT
```

#### Confidence derivation

Governance verdicts (identity, environment, space, attestation, approval —
see [CONNECTED-ASSET-GOVERNANCE-001](../CONNECTED-ASSET-GOVERNANCE-001/spec.md))
are evaluated before any health signal:
a runtime the gateway refuses to route to is `BLOCKED` no matter how healthy
its process looks.

```text
if liveness == setup_error:
  confidence = BLOCKED (setup_blocked)
else if identity unbound / mismatched / bootstrap-only:
  confidence = BLOCKED (identity_unbound | identity_mismatch | bootstrap_only)
else if environment mismatched or blocked:
  confidence = BLOCKED (environment_mismatch)
else if active space not allowed or unset:
  confidence = BLOCKED (active_space_not_allowed | no_active_space)
else if space allow-list unverifiable:
  confidence = LOW (space_unknown)
else if approval rejected:
  confidence = BLOCKED (approval_denied)
else if attestation blocked/unknown/drifted or approval pending:
  confidence = BLOCKED (approval_required)
else if doctor reported a failed send path:
  confidence = LOW (recent_test_failed)
else if completion_rate < 0.5:
  confidence = LOW (completion_degraded)
else if mode == INBOX:
  confidence = HIGH (queue_available)
else if mode == ON-DEMAND and reachability == launch_available:
  confidence = MEDIUM (launch_available)
else if liveness in {offline, stale}:
  confidence = LOW (sse_disconnected | attach_required | unavailable)
else if liveness == connected:
  confidence = HIGH (live_now)
else:
  confidence = MEDIUM (unknown)
```

`confidence` is specifically about **safe to send now through Gateway**. For
inbox-backed agents, `HIGH` confidence means Gateway can safely accept and
queue work. It does not imply that a worker is attached or that completion is
immediate.

#### Confidence reason

Every derived confidence value includes a machine-readable reason code
(`confidence_reason`, parenthesised above) and a human-readable explanation
(`confidence_detail`), e.g. `Gateway can safely accept and queue work now.`
or `Start the attached session before sending.` The implemented vocabulary
is the set shown in the derivation above plus `binding_drift` reasons
supplied by governance; implementations must not invent display-only codes
the daemon does not emit.

#### Reachability derivation

```text
if liveness == setup_error or any governance verdict blocks routing:
  reachability = unavailable
else if mode == INBOX:
  reachability = queue_available
else if activation == attach_only and liveness in {stale, offline}:
  reachability = sse_disconnected if sse_connected == false
                 else attach_required
else if mode == LIVE and liveness == connected:
  reachability = live_now
else if mode == ON-DEMAND:
  reachability = launch_available
else:
  reachability = unavailable
```

The `attach_only` branch is what lets attached sessions render an actionable
red state without waiting for the offline threshold — see the escalation
scope note in the Daemon-to-UI Status Contract below.

#### Invariants

- `ERROR` always overrides `IDLE`, `QUEUED`, or `WORKING`.
- `OFFLINE` and `STALE` are distinct user-facing states in v1.
- `QUEUED` never implies ownership by a runtime.
- `WORKING` always implies that a runtime or worker has already claimed the
  invocation.
- `INBOX` describes an agent's queue-backed connectivity class, not whether a
  specific invocation is already queued.

### Queue Capability vs Queued Work

The spec must distinguish:

- **agent-level queue capability**
  - `placement=mailbox`
  - `activation=queue_worker`
  - `queue_capable=true`
  - `queue_depth=n`
- **invocation-level queued state**
  - `message_queued`
  - `work_state=queued`

An inbox-backed agent with zero pending work should usually render as:

- `mode=INBOX`
- `presence=IDLE`
- `reply=SUMMARY`

An inbox-backed agent with pending work should render as:

- `mode=INBOX`
- `presence=QUEUED`
- `reply=SUMMARY`

The product must not imply that every inbox-backed agent is always already
queued.

## Heartbeats, Health, and Staleness

### Heartbeat Sources

Gateway may derive health from any of these sources depending on template:

- runtime heartbeat
- upstream listener heartbeat
- attached-session heartbeat
- queue worker heartbeat
- successful preflight check for on-demand runtimes
- last successful claim/completion for brokered or sparse adapters

### v1 Timing Defaults

- Target heartbeat interval for persistent live runtimes: **15 seconds**
- `connected`: heartbeat seen within **30 seconds**
- `stale`: no heartbeat for **>45 seconds**
- `offline`: no heartbeat for **>120 seconds**, process exit, session detach, or
  repeated launch/preflight failure
- `setup_error`: dependency, config, auth, or launch validation failed before a
  runtime ever became healthy

### Special Cases

- `Claude Code Channel` may emit sparse work telemetry and still be healthy if
  pickup and completion remain reliable.
- `on_demand` runtimes such as default Ollama are considered `connected` only
  if their preflight passes and the launch path is currently healthy.
- `mailbox` agents do not require a live runtime heartbeat to accept work; they
  are healthy if the queue can durably accept work and no setup error blocks
  drain workers.
- `mailbox` agents may have `reachability=queue_available` even when no live
  worker is attached. They are not `OFFLINE` unless the queue path itself is
  unavailable.

## Daemon-to-UI Status Contract

### Operator Intent Takes Priority

Before any health signal is evaluated, the daemon checks operator intent fields
in the following priority order. These override observed runtime state:

1. `lifecycle_phase == "hidden"` or `lifecycle_phase == "archived"` → agent is
   intentionally out of active operation regardless of runtime signals. A common
   reason is that the agent did not record shutdown properly and still appears
   degraded. The operator's decision overrides the signal.
2. `desired_state == "stopped"` AND `connected == false` → agent is stopped and
   has actually stopped (gray).
3. `desired_state == "stopped"` AND `connected == true` → agent is stopping but
   not yet down (yellow — transitional).
4. `desired_state == "running"` → apply health checks below.

### UI Tone Semantics

The product exposes four tones for agent status display. Implementations must
honour these semantic boundaries:

| Tone | Color | Meaning |
| --- | --- | --- |
| `muted` | gray | Operator has intentionally taken this agent out of active operation: stopped, hidden, or archived. |
| `warning` | yellow | Agent needs attention: transitioning, pending approval, registry signals going stale, or degraded. |
| `error` | red | Agent is desired=running but cannot function. |
| `ok` | green | Agent is healthy and ready. |

Gray is reserved exclusively for intentional-off states. Any agent that is
`desired_state=running` but not working correctly renders red, not gray.

### Reachability Values for Attached Sessions

Two values of the `reachability` helper (see Reachability derivation above)
carry the attached-session failure states:

- `sse_disconnected` — attached channel agent is connected (process alive, MCP
  pings active) but the SSE subscription to the platform is down. Messages
  cannot be delivered. Operator should reconnect the ax-channel MCP server.
- `attach_required` — attached channel agent process is not running. Operator
  must start the session.

Both render red immediately: an attached session cannot self-heal (the daemon
does not own its lifecycle), so there is no transitional yellow phase to wait
out.

### Liveness Escalation Thresholds

The daemon derives liveness from how fresh the registry signals are (primarily
`last_seen_at`). Two thresholds apply to entries whose raw state remains
`running` while signals age:

| Threshold | Age of last registry signal | Liveness | UI tone | Notes |
| --- | --- | --- | --- | --- |
| Stale | > 75 seconds | `stale` | yellow | Transient; agent may self-heal |
| Offline | > 300 seconds | `offline` | red (LIVE mode only) | Persistent; operator attention needed |

The thresholds derive from the 30-second registry signal cadence
(`RUNTIME_HEARTBEAT_INTERVAL_SECONDS`, see
[HEARTBEAT-001](../HEARTBEAT-001/spec.md)) shared by every first-party signal
writer — the runtime listener loop, the hermes platform adapter, and the
channel bridge. 75s = 2.5 beats: it absorbs one missed beat plus a routine SSE
reconnect window (45s idle timeout + backoff) without flapping yellow.
300s = 10 beats. The thresholds are **not** derived from the platform
heartbeat channel: the gateway never receives platform heartbeats — it
observes the registry signals that agents write locally.

An entry that has never written a signal (`last_seen_at` absent) is `offline`,
not merely `stale`.

`offline` liveness maps to presence `OFFLINE` only for LIVE mode agents.
INBOX and ON-DEMAND agents fall through to `IDLE` on offline liveness because
their availability is defined by queue access or launch capability, not a
persistent connection.

**Escalation scope.** The yellow-then-red ladder applies in practice to
daemon-managed runtimes whose raw state stays `running` while signals age
(e.g. a wedged listener loop). Attached sessions and external plugins are
resolved by the daemon sweep into raw `stale` at the 75-second threshold and
render red immediately via `reachability` / the external-plugin check — they
cannot self-heal, so escalation would only delay the operator. See
[ADR-008](../../docs/adr/ADR-008-agent-status-model.md) for the full
rationale. Known gap: supervised sentinel subprocesses currently defeat the
ladder because the daemon's PID monitor stamps `last_seen_at` while the
process exists, even if it is wedged —
[#295](https://github.com/FulcrumDefense/fulcrum-gateway/issues/295).

## Lifecycle and Protocol Invariants

### Live Interactive Lifecycle

1. `message_received`
2. `message_claimed`
3. `working`
4. optional `progress`
5. optional `tool_call`
6. optional `tool_result`
7. `completed` or `error`

### Inbox / Background Lifecycle

1. `message_received`
2. `message_queued`
3. `message_claimed`
4. `working`
5. optional `progress`
6. optional `summary_pending`
7. `summary_posted` or `completed` or `error`

### Required Meanings

- `message_received`: Gateway accepted responsibility to evaluate the message.
- `message_queued`: the message is durably accepted into a Gateway queue or
  mailbox and is safe but not yet owned by a worker.
- `message_claimed`: a specific runtime or worker accepted ownership.
- `working`: the claimant has started execution.
- `summary_pending`: background work is done or nearly done, and the sender
  should expect a summary instead of an inline assistant reply.

### Terminal States

- `completed`
- `error`
- `cancelled`
- `expired`

Late or duplicate events after a terminal state are ignored for snapshot
derivation but preserved in the local Gateway log as protocol anomalies.

### Events vs Status

- Events are append-only facts.
- Status is a derived snapshot.
- aX and Gateway UIs render the current snapshot plus recent timeline.
- No UI may infer durable state from a single transient event without snapshot
  derivation.

## Event Envelope and Delivery Semantics

### Canonical Envelope

Every Gateway↔aX lifecycle event must use this envelope:

```json
{
  "schema_version": "gateway.event.v1",
  "event_id": "evt_01H...",
  "event_type": "message_claimed",
  "gateway_id": "gw_123",
  "agent_id": "agt_123",
  "message_id": "msg_123",
  "invocation_id": "inv_123",
  "runtime_id": "rt_123",
  "attempt": 1,
  "sequence": 3,
  "observed_at": "2026-04-22T19:15:30Z",
  "emitted_at": "2026-04-22T19:15:31Z",
  "payload": {
    "backlog_depth": 0
  }
}
```

### Delivery Rules

- Delivery semantics are **at least once**.
- `event_id` is the dedupe key.
- `invocation_id + sequence` is the ordering key.
- `attempt` increments when Gateway retries the same target message after a
  failed prior attempt.
- Each retry gets a **new** `invocation_id`.
- Consumers must accept duplicate delivery and late arrival.

### Snapshot Persistence in aX

aX must persist:

1. `AgentStatusSnapshot`
   - keyed by `agent_id`
   - used for fleet view and agent drill-in
   - includes `mode`, `presence`, `reply`, `confidence`, queue capability,
     queue depth, tags, capabilities, constraints, latest health details,
     `confidence_reason`, `confidence_detail`,
     `last_successful_doctor_at`, and `last_doctor_result`
2. `InvocationStatusSnapshot`
   - keyed by `invocation_id`
   - indexed by `message_id + agent_id`
   - used for sender confidence bubbles and recent activity
   - includes current invocation `presence`, `reply`, queue/claim timestamps,
     and final outcome
3. `GatewayEventTimeline`
   - append-only recent event stream keyed by `invocation_id`

## Gateway ↔ Runtime Adapter Contract

### Adapter Event Types

All adapters must map to these logical events:

- `hello`
- `heartbeat`
- `claim`
- `progress`
- `tool_call`
- `tool_result`
- `complete`
- `error`

### Command Bridge v1

The v1 command-bridge protocol is line-oriented JSON.

- One JSON object per line
- Canonical prefix: `AX_GATEWAY_EVENT=`
- Compatibility prefix accepted during migration: `AX_GATEWAY_EVENT `
- `schema_version` required
- `stderr` is treated as logs, not protocol
- Process exit before `complete` or `error` maps to `invocation_failed`

Canonical example:

```text
AX_GATEWAY_EVENT={"schema_version":"gateway.runtime.v1","type":"progress","message":"Indexing repo","percent":40}
```

### Runtime Envelope

```json
{
  "schema_version": "gateway.runtime.v1",
  "type": "tool_call",
  "invocation_id": "inv_123",
  "agent_id": "agt_123",
  "emitted_at": "2026-04-22T19:16:00Z",
  "payload": {
    "tool_name": "read_file",
    "detail": {
      "path": "README.md"
    }
  }
}
```

### Runtime Protocol Rules

- Unknown `invocation_id` events are rejected and logged.
- Wrong `agent_id` is rejected and logged as a protocol violation.
- `tool_result` without a prior `tool_call` is rejected and surfaced as an
  adapter warning.
- Duplicate `complete` is ignored after the first terminal event.
- Events after terminal state do not mutate snapshots.
- Invalid JSON and oversized events are dropped and surfaced as adapter
  warnings.

## Template Capability Matrix

Every template definition should expose both:

- the canonical core model (`placement`, `activation`, `reply_mode`,
  `telemetry_level`)
- extensible metadata:
  - `tags`
  - `capabilities`
  - `constraints`

Tags explain and filter. They must not replace the core state model.

### Echo (Test)

| Field | Value |
| --- | --- |
| Placement | `hosted` |
| Activation | `persistent` |
| Reply mode | `interactive` |
| Telemetry | `basic` |
| Gateway launches runtime | Yes |
| Gateway only attaches | No |
| Guaranteed signals | `message_received`, `message_claimed`, `working`, `completed`, `error` |
| Optional signals | None |
| Healthy means | Gateway listener is healthy and built-in runtime is available |
| Disconnected means | Gateway listener is stale/offline |
| Inline reply expected | Yes |
| Tags | `local`, `hosted-by-gateway`, `inline-reply`, `basic-telemetry` |
| Capabilities | `reply`, `smoke_test` |
| Constraints | `test_only` |

### Hermes

| Field | Value |
| --- | --- |
| Placement | `hosted` |
| Activation | `persistent` |
| Reply mode | `interactive` |
| Telemetry | `rich` |
| Gateway launches runtime | Yes |
| Gateway only attaches | No |
| Guaranteed signals | `message_received`, `message_claimed`, `working`, `completed`, `error` |
| Optional signals | `progress`, `tool_call`, `tool_result`, richer activity messages |
| Healthy means | Hermes checkout, auth, and launch path validate; heartbeats continue |
| Disconnected means | Launch failure, heartbeat expiry, or runtime exit |
| Inline reply expected | Yes |
| Tags | `local`, `hosted-by-gateway`, `live-listener`, `rich-telemetry`, `filesystem-capable`, `repo-bound` |
| Capabilities | `reply`, `progress`, `tool_events`, `read_files`, `bash_tools` |
| Constraints | `requires-repo`, `requires-provider-auth` |

### Claude Code Channel

| Field | Value |
| --- | --- |
| Placement | `attached` |
| Activation | `attach_only` |
| Reply mode | `interactive` |
| Telemetry | `basic` |
| Gateway launches runtime | No |
| Gateway only attaches | Yes |
| Guaranteed signals | `message_received`, `message_claimed`, `completed`, `error`, connection health |
| Optional signals | `working`, sparse `progress`, sparse tool telemetry |
| Healthy means | Active channel session, identity match, pickup test succeeds |
| Disconnected means | Channel closed, session heartbeat expired, pickup test fails |
| Inline reply expected | Yes |
| Tags | `attached-session`, `inline-reply`, `basic-telemetry`, `user-launched` |
| Capabilities | `reply`, `claim_work` |
| Constraints | `requires-live-session`, `attach-required` |

#### Claude Code Channel Attach UX Requirements

Claude Code Channel is a stdio MCP integration. Gateway must not imply that a
`Start` action launches Claude Code itself unless Gateway owns that launcher.
For attached sessions:

- `Start` means "allow/expect an attached session", not "spawn a worker".
- If `reachability=attach_required`, Gateway must block test sends and manual
  sends instead of delivering work into a dead zone.
- The drawer must show the exact workspace and launch command needed to attach
  the session.
- The channel bridge should heartbeat the Gateway registry on stdio startup,
  MCP `ping`, message delivery, and reply completion.
- Future capability inventory should read the workspace `.mcp.json` and
  agent-local profile metadata to show attached MCP servers, tools, declared
  skills, and agent preferences in the Gateway drawer.

### Ollama

| Field | Value |
| --- | --- |
| Placement | `hosted` |
| Activation | `on_demand` |
| Reply mode | `interactive` |
| Telemetry | `basic` |
| Gateway launches runtime | Yes |
| Gateway only attaches | No |
| Guaranteed signals | `message_received`, `message_claimed`, `working`, `completed`, `error` |
| Optional signals | basic `progress`, model selection detail |
| Healthy means | launch preflight passes, Ollama server reachable, model present |
| Disconnected means | preflight fails, launch fails, or repeated invocation errors |
| Inline reply expected | Yes |
| Tags | `local`, `on-demand`, `cold-start`, `inline-reply`, `basic-telemetry`, `transcript-backed-memory` |
| Capabilities | `reply`, `launch_on_send`, `model_inference`, `transcript_context` |
| Constraints | `requires-model`, `requires-local-server` |

Current PR note: Ollama is still launched on demand. Conversation continuity is
provided by fetching and shaping recent aX transcript history before each model
call. Persistent live-listener Ollama is a follow-up in
[GATEWAY-RUNTIME-PERSISTENCE-001](../GATEWAY-RUNTIME-PERSISTENCE-001/spec.md).

### Pass-through Agent

| Field | Value |
| --- | --- |
| Placement | `mailbox` |
| Activation | `attach_only` |
| Reply mode | `background` / `manual_reply` |
| Telemetry | `basic` |
| Gateway launches runtime | No |
| Gateway only attaches | Yes, through approved local fingerprint/session |
| Guaranteed signals | `local_connect_requested`, `message_queued`, `inbox_polled`, `local_message_sent`, `error` |
| Optional signals | tool events only when the checking agent reports them |
| Healthy means | registry row approved, fingerprint matches, mailbox readable/writable |
| Disconnected means | approval pending/rejected, fingerprint drift, session expired, or wrong local origin |
| Inline reply expected | No; the agent checks and replies when available |
| Tags | `local`, `mailbox`, `polling`, `approval-required` |
| Capabilities | `poll_mailbox`, `reply`, `self_profile` |
| Constraints | `requires-approval`, `requires-fingerprint-match` |

### Inbox / Background Worker (Inbox-backed agent)

| Field | Value |
| --- | --- |
| Placement | `mailbox` |
| Activation | `queue_worker` |
| Reply mode | `summary_only` by default |
| Telemetry | `basic` |
| Gateway launches runtime | Optional drain worker |
| Gateway only attaches | N/A |
| Guaranteed signals | `message_received`, `message_queued`, `message_claimed`, `completed`, `error` |
| Optional signals | `working`, `summary_pending`, `summary_posted` |
| Healthy means | queue accepts work durably and at least one worker can claim |
| Disconnected means | queue unavailable, drain workers permanently offline, or setup error |
| Inline reply expected | No; summary later by default |
| Tags | `queue-backed`, `summary-later`, `background`, `mailbox`, `basic-telemetry` |
| Capabilities | `queue_work`, `claim_work`, `post_summary` |
| Constraints | `not-live-listener` |

## Sender Experience

### Pre-send Expectations

The composer and agent picker must show Mode, Presence, Reply behavior, and
telemetry richness before sending, plus current confidence and natural-language
reachability help.

Examples:

- `Hermes — LIVE · IDLE · REPLY · HIGH`
- `Claude Code Channel — LIVE · IDLE · REPLY · MEDIUM`
- `Ollama — ON-DEMAND · IDLE · REPLY · MEDIUM`
- `Inbox-backed Worker — INBOX · IDLE · SUMMARY · HIGH`

Supporting copy examples:

- `You can expect an inline reply.`
- `Gateway will start this runtime when you send.`
- `This agent is inbox-backed. Work can be queued safely even without a live worker.`
- `Reconnect the local session before sending.`

### Post-send Inline Activity Bubble

Interactive agents:

- `Received by Gateway`
- `Claimed by runtime`
- `Working`
- `Using tool`
- `Responding`
- `Completed`

Background/inbox agents:

- `Queued in inbox`
- `Claimed by worker`
- `Working`
- `Summary pending`
- `Summary posted`
- `Completed with no reply expected`

Failure states:

- `No active runtime attached`
- `Setup error`
- `Stale listener`
- `Invocation failed`

The originating message keeps a compact completed status. The final reply or
summary still lands at the bottom of the transcript.

### Confidence Surface

The sender surface should expose a deterministic operational confidence label:

- `HIGH`
- `MEDIUM`
- `LOW`
- `BLOCKED`

Examples:

- `Hermes — LIVE · IDLE · REPLY · HIGH`
- `Ollama — ON-DEMAND · IDLE · REPLY · MEDIUM`
- `Inbox-backed Worker — INBOX · IDLE · SUMMARY · HIGH`
- `Claude Code Channel — LIVE · STALE · REPLY · LOW`
- `Broken Hermes — LIVE · ERROR · REPLY · BLOCKED`

## Operator Experience

### Fleet View

The fleet view must show:

- Agent
- Mode
- Presence
- Reply
- Confidence
- Telemetry
- Queue depth
- Typical Claim
- Typical First Activity
- Typical Completion
- Typical Summary Time when applicable
- Last Seen
- Last Outcome

### Drill-In

The drill-in must show:

- placement and activation
- reachability explanation
- connection health and heartbeat source
- recent lifecycle timeline
- latest invocation state
- setup requirements and missing dependencies
- test-send controls
- recent errors and alerts

### Gateway Doctor

`Gateway Doctor` is a required setup primitive in both CLI and UI. Every
template must expose a deterministic setup report covering:

- Identity
- Gateway auth
- Local path or dependency checks
- Runtime launch or attach validation
- Heartbeat or queue health
- Test claim
- Test reply or summary viability when applicable

Doctor results must update `AgentStatusSnapshot` with:

- `last_successful_doctor_at`
- `last_doctor_result`
- any changed `confidence_reason` / `confidence_detail`

The default agent-facing wrapper for this flow should be a Gateway-native setup
skill, such as `gateway-agent-setup`, built on top of the same CLI and local
API primitives rather than a separate browser-only wizard.

Canonical CLI shape:

```text
ax gateway agents doctor hermes-bot
✓ Gateway connected to paxai.app
✓ Agent identity minted
✓ Hermes checkout found
✓ Runtime starts
✓ Heartbeat received
✓ Test message claimed
✓ Inline reply received
Status:
LIVE · IDLE · REPLY · HIGH
```

Inbox-backed example:

```text
ax gateway agents doctor docs-worker
✓ Gateway connected
✓ Inbox queue writable
✓ Worker config valid
! No worker currently attached
✓ Test job queued
Status:
INBOX · IDLE · SUMMARY · HIGH
Expectation:
Work can be queued now. Summary will post when a worker drains the inbox.
```

### First-run Contract

The first-run Gateway experience should strongly recommend or require:

1. Install Gateway
2. Log in / bootstrap
3. Run Echo smoke test
4. See message reach aX and return
5. Add a real agent from a template
6. Run Gateway Doctor
7. Send a template-specific test message
8. Verify the agent in Fleet View

## Auth and Credential Boundary

### v1 Rules

- User PAT is stored only by Gateway.
- User PAT is bootstrap/enrollment only.
- Child runtimes never receive user PAT, raw platform JWT, or another agent's
  credentials through env, args, config, stdin, logs, or protocol events.
- Gateway local API binds to loopback by default.
- Gateway local API requires a local session token or capability boundary.
- Gateway redacts PATs, JWTs, and local capability tokens from logs and event
  payloads.
- Child runtimes may emit Gateway events, receive assigned work, and return
  results.
- Child runtimes may not mint identities, impersonate another agent, or call aX
  as the user.

### Safe by Default Setup Copy

The setup UX should visibly explain the trust boundary:

- `Gateway keeps your aX credential.`
- `This runtime receives only a local scoped capability.`
- `It cannot impersonate you or mint other agents.`

### OAuth Later

OAuth to `paxai.app` is a later login-provider swap. It must not change:

- lifecycle event names
- internal model fields
- sender bubble semantics
- runtime adapter contract

## Metrics and Confidence Signals

### Canonical Metrics

- `time_to_gateway_ack`
- `time_to_claim`
- `time_to_first_activity`
- `time_to_completion`
- `reply_rate`
- `summary_rate`
- `completion_rate`

### UX Labels

Do not use `SLA` or `response time` as the generic label.

Use:

- `Typical Claim`
- `Typical First Activity`
- `Typical Completion`
- `Typical Summary Time`

### Confidence Inputs

`confidence` is deterministic, not a vague heuristic. It should be derived
from:

- last successful smoke test
- last heartbeat
- queue health
- launch/preflight health
- recent completion rate
- recent claim latency p95
- setup errors

`confidence` answers: **how safe is it to send work through this path right
now?**

It does not promise immediate completion. For inbox-backed agents especially,
high confidence can mean the queue path is healthy even if no worker is
currently attached.

### Windows and Denominators

- Windows: last `24h`, `7d`, `30d`
- `p50` and `p95` computed over successful attempts unless explicitly labeled
  otherwise
- in-flight attempts excluded from completion latency
- timeout/failure contributes to failure and timeout counts, not latency

Denominators:

- `reply_rate`: completed invocations where `reply_mode=interactive`
- `summary_rate`: completed invocations where summary behavior is possible
- `completion_rate`: claimed invocations
- `claim_latency`: received messages that later claimed successfully

If a message never claims, it counts toward claim timeout/failure, not claim
latency.

## Acceptance and Adversarial Tests

### Happy Paths

- Echo test proves end-to-end Gateway receipt, claim, progress, and reply.
- Hermes emits rich activity and tool telemetry.
- Claude Code Channel shows reliable pickup and completion even with sparse
  activity.
- Ollama launches on demand, claims work, and completes.
- Inbox accepts work without inline reply and later posts summary.
- Inbox-backed agent with empty queue renders `INBOX · IDLE · SUMMARY`.
- Inbox-backed agent with pending work renders `INBOX · QUEUED · SUMMARY`.
- Gateway Doctor produces deterministic pass/fail output for each template.

### Adversarial Cases

- Gateway restarts after `message_received` before `message_claimed`.
- Gateway restarts after `message_claimed` before terminal event.
- Runtime crashes after claim.
- Adapter emits malformed JSON.
- Adapter emits duplicate `complete`.
- Adapter emits `tool_result` without `tool_call`.
- aX delivers the same message twice.
- Two workers race for the same inbox job.
- PAT revoked while Gateway is running.
- SSE/channel disconnect during active work.
- On-demand runtime fails to launch.
- Local dependency disappears after prior healthy state.
- Gateway reconnects after stale local state.
- Background job finishes with summary later and no inline reply.
- Queue is healthy but no live worker is attached.
- Queue is unhealthy and inbox-backed agent becomes `ERROR`.

## Custom Bridge Contract

Gateway must support custom/local agents without requiring them to fit a single
blessed framework.

The custom bridge flow should let an operator declare:

- reply behavior:
  - inline reply
  - summary later
  - silent completion
- activation:
  - Gateway launches command
  - Gateway invokes on demand
  - agent drains inbox
  - Gateway attaches to existing session
- telemetry:
  - heartbeat only
  - progress events
  - tool events
  - completion only

Gateway should then provide:

- command template
- expected env vars
- `AX_GATEWAY_EVENT` examples
- local capability boundary
- smoke test / doctor checks

## Extensible Metadata

In addition to the core state model, the product should support extensible:

- `tags`
- `capabilities`
- `constraints`

These are used for filtering, discovery, and explanation. They must not replace
the canonical state model.

Suggested tag families:

- Connectivity: `live-listener`, `attached-session`, `on-demand`,
  `queue-backed`, `external-broker`
- Execution: `local`, `remote`, `hosted-by-gateway`, `user-launched`,
  `cold-start`
- Reply behavior: `inline-reply`, `summary-later`, `silent-completion`
- Telemetry: `rich-telemetry`, `basic-telemetry`, `heartbeat-only`,
  `no-tool-events`
- Risk/setup: `requires-repo`, `requires-model`, `requires-provider-auth`,
  `experimental`

## Roadmap

### v1 Minimum

- normalized internal and operator state model
- template-first connection flow
- Gateway Doctor
- pre-send expectation chips
- sender confidence bubble
- latest snapshot + recent timeline persisted in aX
- p50/p95 metrics for claim, first activity, and completion
- PAT bootstrap with hardened local auth boundary

### Phase 2

- stronger lease and claim semantics
- queue worker hardening
- better dependency/setup diagnostics
- auth/session hardening

### Phase 3

- richer telemetry
- fleet analytics and trends
- alerting and escalations
- production rollout hardening

### Phase 4

- single upstream Gateway control stream
- Gateway as authoritative connected object to aX

### Phase 5

- MCP Gateway mode
- MCP Jam SDK coverage

### Phase 6

- skill/capability layer on top of stable CLI + MCP foundations

## Deliverables

- this primary spec
- companion [mockups.md](mockups.md)
- lifecycle/state derivation tables in this spec
- event envelope definition in this spec
- template capability matrix in this spec
- acceptance/adversarial test matrix in this spec
