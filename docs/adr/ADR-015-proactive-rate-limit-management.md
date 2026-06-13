# ADR-015: Proactive Rate-Limit Management and Request Logging

**Status:** Accepted — implemented in `fd/rate-limit-management`

**See also:** [ADR-009](ADR-009-platform-heartbeat-contract.md) — platform
heartbeat contract (the budget waste from misdirected sweep heartbeats that
motivated this work)

## Context

The platform enforces a per-user rate-limit window, advertised on every
response via `x-ratelimit-remaining` / `x-ratelimit-reset` headers. The
gateway runs many `AxClient` instances against that single budget: one per
managed-agent runtime, one for the daemon sweep, one for the gateway
heartbeat, one per CLI invocation, and one per UI-server request. Before this
work each client discovered the limit independently — by hitting 429 — and
the only mitigation was reactive retry-with-backoff
(`_with_upstream_429_retry`). Under dashboard refresh plus several runtimes,
clients raced each other into the same exhausted window, burned the remaining
budget confirming it was exhausted, and risked escalating the platform's
circuit breaker.

There was also no visibility into *which* requests consumed the budget, so
diagnosing a noisy consumer required guesswork over `activity.jsonl`.

## Decision

### 1. Shared rate-limit facts per process; policy belongs to the caller

A single `_RateLimitState` (in `ax_cli/client.py`) is shared across all
client instances in a process — the daemon threads it into every
`ManagedAgentRuntime`, the sweep client, and the gateway-heartbeat client;
the CLI/UI module shares one state across `_load_gateway_user_client()`
calls. The state holds only what the server said (`remaining`, `reset_at`);
every response records it, and every request first consults the shared state
and **sleeps before sending** when the window is exhausted *for that
caller's threshold*, instead of sending and reacting to the 429. One client
discovering a drained window immediately throttles every other client in the
process.

Exhaustion is derived at check time from the recorded facts, never stored:
with thresholds differing per caller there is no single "exhausted" boolean,
and deriving it also removes the flag-clearing races a sticky boolean would
need lock protocol for.

Thread-safety is deliberately asymmetric: writes (`record`) take a lock,
reads rely on the GIL for atomic attribute access. A stale read at worst
fires one extra request, which the reactive 429 path still handles; sleeping
under a lock would serialize all clients on the slowest waiter.

### 2. Human-initiated vs automated low-water thresholds

Throttling starts at a *low-water mark*, not at zero, and the threshold
follows who is asking, not which process they are in:

- **Automated traffic: 10 remaining** (`RATE_LIMIT_LOW_WATER`). Daemon
  runtimes, the sweep, gateway heartbeats, and UI dashboard auto-refresh
  polling yield early — nothing they do is urgent enough to drain the tail
  of the window.
- **Human-initiated actions: 2 remaining**
  (`RATE_LIMIT_INTERACTIVE_LOW_WATER`). A person clicking or typing gets the
  benefit of the doubt and may run the window nearly to empty.

CLI invocations approximate "human" (acceptable even though some CLI calls
are scripted). The UI server is one process carrying both kinds of traffic,
so the request handler marks dashboard mutations (`/api/...`
POST/PUT/DELETE, excluding the programmatic `/api/v1/*` and `/local/*`
routes) as human-initiated via a per-request context flag; auto-refresh GETs
and local agent traffic stay automated. The flag is set explicitly on every
request because HTTP keep-alive reuses one handler thread across sequential
requests.

### 3. Fail fast when the wait exceeds the caller's budget

Both the proactive path (`RateLimitPreemptedError`) and the reactive path
(`_with_upstream_429_retry` raising `UpstreamRateLimitedError`) fail
immediately when the server's advertised cooldown exceeds the caller's
`max_wait`, instead of capping the sleep and retrying early. Retrying inside
the server's declared cooldown cannot succeed and risks circuit-breaker
escalation; an actionable error carrying `retry_after_seconds`/`reset_at`
("try again at HH:MM:SS") is strictly more useful to the operator than a
doomed retry. This reverses the earlier cap-at-`max_wait`-and-retry behavior.

### 4. Cold-start pre-warm

The shared state starts optimistic (`remaining` unknown), so each process
populates the real window before its first burst of traffic. In the daemon
the **first gateway-presence heartbeat doubles as the pre-warm**: its
response headers carry the window facts, and gateway presence registers at
startup instead of one reconcile tick later — one request, two purposes. A
lightweight GET is the fallback for sessions that predate gateway
registration, and the UI server warms with a GET since it is not the
gateway-presence owner. All pre-warms are best-effort. This stays within
ADR-009's contract: the gateway heartbeat is the gateway's own credential
attesting the gateway's own presence; agent heartbeats still come only from
agent-bound credentials.

### 5. Request logging on by default, with rotation

Every outbound API request is logged as a JSON line to
`~/.ax/gateway/api-requests.log` (timestamp, pid, role daemon/ui_server/cli,
method, path, status, remaining, reset, agent identity). The log rotates at
10MB keeping one backup, so default-on cannot grow unbounded. Set
`AX_LOG_API_REQUESTS=0` to disable. Logging defaults on because budget
exhaustion is exactly the situation where you need history from *before* the
incident; an opt-in flag is always off when you need it.

The log records only request metadata — method, path, status, and rate-limit
headers — never request/response bodies, tokens, or credentials.

## Consequences

- **Positive:** One client discovering exhaustion throttles all clients in
  the process before they burn budget confirming it.
- **Positive:** Long rate-limit windows surface as immediate, actionable
  errors instead of hung commands or circuit-breaker escalation.
- **Positive:** `api-requests.log` makes worst-offender analysis a `grep`
  one-liner instead of guesswork (see
  [the 429-storm scenario](../scenarios/investigate-429-storm.md)).
- **Negative:** State is shared per-process, not machine-wide: the daemon, a
  CLI invocation, and the UI server each maintain their own window view and
  can still collectively overrun the shared budget. A cross-process
  coordination file is possible future work if this bites in practice.
- **Negative:** Proactive sleeps make slow-budget situations visibly slower
  (the CLI prints "Rate limit reached — waiting Ns") — trading latency
  transparency for fewer hard failures.
