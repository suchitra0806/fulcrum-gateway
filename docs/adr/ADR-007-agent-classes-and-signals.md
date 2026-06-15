# ADR-007: Agent Classes and Gateway Signaling Contract

**Status:** Accepted — core agent classes reflect the implementation as of 2026-06-10; boundary completion ongoing (see Known Gaps below)

## Context

The Gateway manages a growing set of agent types with fundamentally different
runtime models: some are processes the daemon starts and supervises directly,
others are external processes that report their own state, others are passive
mailboxes, and others are attached sessions the daemon cannot control. Without
a defined classification and signaling contract, each new agent type required
bespoke handling in the daemon sweep, the health derivation logic, and the UI.

This ADR defines the canonical agent classes and specifies what each class is
responsible for reporting to the Gateway registry.

![Gateway status architecture](../images/gateway-status-architecture.svg)

This ADR (ADR-007) defines the left boundary: what each agent class is
responsible for reporting to the Gateway registry.
[ADR-008](ADR-008-agent-status-model.md) defines the right boundary: how the
daemon translates those signals into operator-visible status.

### Relationship to other specs

The five classes in this ADR are **signaling contract categories** — they
describe who owns the process lifecycle and how the agent reports health to the
local Gateway registry. They are distinct from the **asset taxonomy** defined in
[GATEWAY-ASSET-TAXONOMY-001](../../specs/GATEWAY-ASSET-TAXONOMY-001/spec.md),
which describes what kind of thing an asset is (`asset_class`) and how work
enters it (`intake_model`). Multiple signaling classes can share the same asset
class, and the same template can serve different asset classes depending on
deployment.

The registry entry model (including `template_id`, `runtime_type`, and
`capabilities`) is defined in
[GATEWAY-AGENT-REGISTRY-001](../../specs/GATEWAY-AGENT-REGISTRY-001/spec.md).
The polling mailbox pattern is specified in detail in
[GATEWAY-PASS-THROUGH-MAILBOX-001](../../specs/GATEWAY-PASS-THROUGH-MAILBOX-001/spec.md).

## Decision

### Agent Classes

Five classes cover all current and anticipated agent models:

| Class | Lifecycle ownership | Signaling model | Gateway mode |
| --- | --- | --- | --- |
| **Daemon-managed** | Daemon starts, supervises, and stops the process or in-daemon listener thread | Daemon sets registry state directly; runtime sends heartbeats from its own listener loop | LIVE |
| **Attached session** | External process started independently; daemon observes but does not own | Channel bridge writes `last_seen_at` and `sse_connected` to the registry on a 30s cadence, plus edge-triggered writes on SSE connect/disconnect | LIVE |
| **Polling mailbox** | No continuous runtime; agent polls on its own schedule | Check-in on each poll; no continuous heartbeat expected between polls | INBOX |
| **External plugin** | Plugin process managed externally; daemon tracks via periodic heartbeats | Plugin sends heartbeats to `/local/heartbeat`; daemon observes arrival and age | LIVE |
| **On-demand** | Daemon launches on message arrival; process exits when done | Daemon sets state at launch and exit; no heartbeat between launches | ON-DEMAND |

The polling mailbox and attached session classes are the most commonly confused.
The key distinction is delivery model, not runtime sophistication:

![Inbox vs channel delivery](../images/inbox-vs-channel.svg)

The `mode` field that the UI reads to determine delivery model is computed by
the Gateway from two template registration fields:

| `placement` | `activation` | `mode` |
| --- | --- | --- |
| `mailbox` | any | `INBOX` |
| `attached` or `hosted` | `persistent` or `attach_only` | `LIVE` |
| `hosted` | `on_demand` | `ON-DEMAND` |

**For new agent classes:** register with `placement=mailbox` to enter the
polling class; register with `placement=attached` and `activation=attach_only`
for the attached session class; `placement=hosted` with `activation=persistent`
for daemon-managed. The Gateway derives `mode` automatically — do not attempt
to set it directly.

### Lifecycle ownership, not connector ownership

The class boundary is who owns the **process lifecycle**, not who wrote the
code. For the attached session class, this repo ships both connector
implementations — the ax-channel MCP server (`channel/server.ts`) and the
Python channel bridge (`ax channel`, which feeds the registry) — but the
operator decides whether the hosting session runs at all, and the daemon
cannot restart it. That irrecoverability, not code provenance, is what makes
the class distinct (and is why its failure states render red immediately
rather than escalating; see ADR-008).

### Current Templates and Runtime Types by Class

`asset_class` and `intake_model` values follow
[GATEWAY-ASSET-TAXONOMY-001](../../specs/GATEWAY-ASSET-TAXONOMY-001/spec.md).
The authoritative list is discovered from ``ax_cli/manifest_templates/`` and
``~/.ax/templates/`` (see ``manifest_template_library.py``; closes #259).
Snapshot as of 2026-06-15:

| Signaling class | Template ID | Runtime type(s) | `asset_class` | `intake_model` | Notes |
| --- | --- | --- | --- | --- | --- |
| **Daemon-managed** | `echo_test` | `echo` | `interactive_agent` | `live_listener` | Built-in test runtime; in-daemon listener thread |
| **Daemon-managed** | *(runtime install)* | `sentinel_inference_sdk` | `interactive_agent` | `live_listener` | Daemon-supervised sentinel subprocess bridging vendor SDKs (`openai_sdk`, `groq_sdk`, `mistral_sdk`, `gemini_sdk`, …; vendored in 0.7.0 per ADR-012) |
| **Daemon-managed** | *(runtime install)* | `sentinel_hermes_sdk` | `interactive_agent` | `live_listener` | Daemon-supervised Hermes SDK sentinel subprocess |
| **Daemon-managed** | `sentinel_cli` | `sentinel_cli` | `interactive_agent` | `live_listener` | Direct CLI sentinel subprocess (`claude_cli` resolves here) |
| **Attached session** | `claude_code_channel` | `claude_code_channel` | `interactive_agent` | `live_listener` | MCP stdio bridge; attached by Claude Code or compatible client |
| **Polling mailbox** | `pass_through` | `inbox` | `interactive_agent` | `polling_mailbox` | Agent polls and replies interactively; see GATEWAY-PASS-THROUGH-MAILBOX-001 |
| **Polling mailbox** | `inbox` | `inbox` | `background_worker` | `queue_accept` | Queue worker; drains jobs and may summarize rather than reply inline |
| **Polling mailbox** | `service_account` | `inbox` | `service_account` | `notification_source` | Outbound-only; no runtime process |
| **External plugin** | `hermes` | `hermes_plugin` | `interactive_agent` | `live_listener` | Hermes plugin process managed outside the daemon (default hermes path since the `hermes_sentinel` hard cut) |
| **On-demand** | `ollama`, `langgraph`, `langgraph_composio`, `autogen`, `strands` | `exec` | `interactive_agent` | `launch_on_send` | Gateway command bridges; launched on send, exit when done |

`codex_cli` was removed in 0.7.0 (ADR-012). Vendor SDK runtimes in flight
(e.g. `anthropic_sdk`, `cohere_sdk`) join the Daemon-managed class through the
sentinel inference bridge and inherit its signaling contract without changes
to this ADR.

### Signaling Contract

The full signaling contract — which registry fields each agent class is
responsible for, derived fields agents must not set, and constraints on
`sse_connected` — is defined in
[GATEWAY-AGENT-REGISTRY-001](../../specs/GATEWAY-AGENT-REGISTRY-001/spec.md)
(Runtime State and Signaling Fields section).

Every managed agent has three layers of state that the signaling contract
operates on — desired, lifecycle phase, and effective:

![Agent lifecycle states](../images/agent-lifecycle-states.svg)

### Registry Signals vs Platform Heartbeats

![Gateway heartbeat channels](../images/gateway-heartbeat-channels.svg)

Registry signals (this ADR) and platform heartbeats ([ADR-009](ADR-009-platform-heartbeat-contract.md))
are distinct channels. Registry signals are local filesystem writes that the
Gateway daemon reads to derive health state. Platform heartbeats are sent
directly to paxai.app using agent-bound credentials. The two are kept separate
because the gateway is an inbound proxy — the platform sees agent identities,
not the gateway managing them. See ADR-009 for the full decision rationale.
Every first-party signal writer — the runtime listener loop, the hermes
platform adapter, and the channel bridge — beats on the same 30-second cadence
(`RUNTIME_HEARTBEAT_INTERVAL_SECONDS`); the staleness thresholds in ADR-008
derive from it.

## Known Gaps

The following cases represent places where the Gateway does not yet fully
uphold its side of the contract. As a consequence, the UI contains
type-specific checks that compensate (documented in
[ADR-008](ADR-008-agent-status-model.md)), or health signals that overstate
runtime health:

- **External plugin not attached**: the UI checks `externalManaged && !connected`
  directly rather than a gateway-computed reachability value. This is a known
  violation of the principle that all health logic is computed by the gateway.
  A `reachability=plugin_not_attached` value was explored but reverted: the UI
  still needs to combine with `presence` to differentiate stale (yellow, may
  self-reconnect) from offline (red, persistent failure), and
  `external_runtime_managed` is itself a gateway-provided flag rather than
  type-specific logic inferred by the UI. The added complexity of a new
  reachability value did not justify the marginal boundary improvement. The
  correct long-term fix is for the gateway to emit a richer reachability value
  that encodes both the class and the severity, eliminating both checks.
- **Supervised sentinel PID liveness laundering (#295)**: the daemon's
  subprocess monitor stamps `last_seen_at` fresh every 5 seconds while the
  sentinel PID exists, converting process existence into activity freshness. A
  sentinel that is alive but wedged renders green indefinitely because the
  staleness ladder never sees an aging signal. This is the gateway violating
  the registry spec's own rule ("do not report running while a critical
  subsystem is broken") on behalf of the agents it supervises. Tracked in
  [#295](https://github.com/FulcrumDefense/fulcrum-gateway/issues/295).
- **Connection-path taxonomy (#296)**: GATEWAY-AGENT-REGISTRY-001 reserves a
  richer connection-path vocabulary (`tool_listener`, `attached_channel`,
  `doorbell_watcher`) that is not implemented and collides with the
  AVAIL-CONTRACT `connection_path` DTO field. This ADR's classes deliberately
  key on implemented fields (`intake_model`, `placement`, `activation`)
  instead. Tracked in
  [#296](https://github.com/FulcrumDefense/fulcrum-gateway/issues/296).

## Consequences

- **Positive:** New agent types can be classified into one of the five classes
  and immediately inherit the correct signaling contract without bespoke
  handling.
- **Positive:** The Gateway's health derivation logic (`_derive_liveness`,
  `_derive_reachability`, `_derive_confidence` in `ax_cli/gateway_state.py`)
  can be written generically against the contract rather than against
  individual agent types.
- **Negative:** The class boundary for attached sessions is soft — the daemon
  cannot enforce that an attached session reports `sse_connected` accurately.
  This repo ships the reference connectors, but the contract is advisory for
  agent implementations the daemon does not own.
- **Negative:** On-demand agents with long launch times may appear briefly
  stale before the daemon updates `effective_state`. This is a known gap;
  operators should interpret stale on-demand agents as launching, not failed.
