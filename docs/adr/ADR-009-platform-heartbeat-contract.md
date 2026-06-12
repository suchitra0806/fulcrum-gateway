# ADR-009: Platform Heartbeat Contract

**Status:** Accepted — implemented in PR #23 (`fd/heartbeat-liveness`); contract formalised here. Updated for gateway-identity heartbeats (gateway registration landed after the original decision).

**See also:** [ADR-007](ADR-007-agent-classes-and-signals.md) — Gateway registry signals (distinct from platform heartbeats; lands with PR #205); [ADR-015](ADR-015-proactive-rate-limit-management.md) — rate-limit coordination for the clients that carry these heartbeats

**Spec:** [HEARTBEAT-001](../../specs/HEARTBEAT-001/spec.md) — full protocol, status values, by-class breakdown

## Context

![Gateway heartbeat channels](../images/gateway-heartbeat-channels.svg)

The Gateway acts as a local inbound proxy: the platform routes messages to the
Gateway which delivers them to local agents. Agent runtimes maintain two
independent health reporting channels — registry signals to the local Gateway
(ADR-007) and platform heartbeats to paxai.app (this ADR).

When this ADR was first written the platform saw only agent identities, not
the gateway managing them. That has since changed: `ax gateway login`
registers the gateway with the platform and receives a `gateway_id`, agents
are created carrying that `gateway_id`, and the platform exposes a
gateway-presence heartbeat endpoint. The platform now has a first-class
gateway identity — but the channels remain separate, because each credential
should attest only its own identity's presence (see the generalised principle
below).

PR #23 (`fd/heartbeat-liveness`) moved heartbeats from the daemon sweep to
agent runtimes as a tactical fix: the sweep used a user-level session token
which the platform rejects with 400 "Not a bound agent session", silently
burning rate-limit budget on every reconcile tick without updating platform
presence.

## Decision

### Agent runtimes emit their own heartbeats; the gateway sweep does not

The move from sweep-based to agent-direct heartbeats is now a **deliberate
design decision**, not merely a workaround for the token restriction. Even if
the platform removed the restriction, the current model is semantically correct
and should continue:

A platform heartbeat should mean "this agent identity is alive — its credential
is valid and its process is running." A heartbeat from the gateway's user token
would mean "the gateway believes this agent is running" — a weaker claim that
conflates agent presence with gateway presence.

In the v1 inbound-proxy model, gateway down means no inbound work can reach
the agent regardless. The operational independence of agent-direct heartbeats is
therefore limited in practice. The semantic clarity is still worth preserving:
a heartbeat signed by the agent's own credential is attributable to that agent
on the platform, visible to other agents and space members as that agent's
liveness signal. It also positions the system better for future connectivity
models where agents might connect through multiple gateways or alternative paths.

### Generalised principle: a credential attests only its own identity's presence

The rule underneath the agent-heartbeat decision is broader than agents:
**every heartbeat is sent with the credential of the identity whose presence
it attests.** Agent-bound tokens attest agent presence; the gateway's session
credential attests gateway presence; neither proxies for the other.

### Gateway-presence heartbeats

Since gateway registration landed, the daemon also maintains the gateway's
own platform presence: every 60 seconds the daemon run loop posts
`POST /api/v1/gateways/{gateway_id}/heartbeat` using the session credential,
where `gateway_id` was issued at `ax gateway login`. This is consistent with
the generalised principle — the session credential is the gateway's own
identity, and the heartbeat claims only "this gateway is alive," not anything
about the agents it manages. Gateway-presence heartbeats are best-effort:
failures are swallowed and never disturb the reconcile loop. The first one is
sent at daemon startup, doubling as the rate-limit pre-warm
([ADR-015](ADR-015-proactive-rate-limit-management.md)).

This channel did not exist when the original decision was made and does not
weaken it: agent presence still comes only from agent-bound credentials.

### The sweep loop explicitly must not send agent heartbeats

This prohibition is documented in `_sweep_lifecycle()` and must be maintained:
the sweep is the owner of local registry state, not the agent's platform
presence. Any future work that gives the sweep an agent-bound credential for
other purposes must not use it to proxy heartbeats. (The gateway-presence
heartbeat above is not an exception — it attests the gateway, not an agent,
and is sent from the run loop, not the sweep.)

### Protocol

The full heartbeat protocol — status values, timing, by-agent-class breakdown —
is defined in [HEARTBEAT-001](../../specs/HEARTBEAT-001/spec.md).

## Consequences

- **Positive:** Rate-limit budget is no longer consumed by 400-rejected
  heartbeat requests from the sweep.
- **Positive:** Platform presence is attributable to the specific agent
  identity, not the gateway. Operators and other agents see accurate per-agent
  liveness.
- **Positive:** The decision is forward-compatible: if gateway connectivity
  models evolve (multi-gateway, alternative paths), agent-direct heartbeats
  remain valid regardless.
- **Negative:** Daemon-managed agents that crash without calling `stop()` may
  not send an `offline` heartbeat. The platform marks them offline via its own
  timeout, but there is a gap window.
- **Negative:** Attached sessions (`claude_code_channel`) have no explicit
  platform heartbeat path — presence depends entirely on the SSE connection
  timeout. A broken SSE with a live MCP process creates a gap between platform
  view and local Gateway view (the bug this PR's `sse_connected` field
  addresses at the local level).
