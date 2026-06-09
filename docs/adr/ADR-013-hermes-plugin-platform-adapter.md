# ADR-013 — Hermes Platform Plugin Adapter

**Status:** Accepted (decision made ~2026-04-25; formalized 2026-06-05)
**Date:** 2026-06-05
**Author:** Mark Galpin

> **Note on numbering:** This decision predates ADR-012 chronologically — the
> implementation landed in `feat/hermes-ax-platform-plugin` before the
> vendor-sdk cleanup in ADR-012. ADRs 007–011 exist in open PRs (#205 and
> #231) and will slot between ADR-006 and ADR-012 when those merge. ADR-013
> is the next available sequence number in this repo's merged history; the
> chronological order is ADR-006 → **ADR-013** (~2026-04-25) → ADR-012
> (2026-06-05).

---

## Context

The Gateway supervisor managed Hermes agents via a **per-mention
sentinel-subprocess pattern**: each @-mention spawned a new `sentinel.py`
subprocess, ran the agent against that single message, and exited. This worked
for stateless one-shot interactions but had compounding problems:

1. **Tools shim collision (task `4bb409ff`):** The sentinel loaded a vendored
   tools shim that collided with system-level tooling, causing unexpected
   behavior for agents that used file or terminal tools. This was the immediate
   forcing function.

2. **No native session continuity:** Each subprocess had no memory of prior
   turns unless the sentinel explicitly rebuilt history from the conversation
   thread. The rebuild was fragile and missed context across long conversations.

3. **Per-mention spawn overhead:** Starting a Python subprocess and
   re-importing all runtime dependencies on every @-mention added latency and
   memory churn, especially in active spaces.

4. **Inherited environment:** The subprocess inherited the launching user's full
   environment, including credentials and ambient filesystem access. Confinement
   relied on operator discipline rather than structural enforcement.

Hermes has a native **platform adapter** architecture: external messaging
platforms register as `BasePlatformAdapter` subclasses. Hermes owns the
agent's full agentic loop, session state, tool callbacks, approval gates, and
platform-specific reply formatting. Implementing a native platform adapter
makes the Gateway-managed platform a first-class Hermes platform alongside
Telegram, Slack, and Discord — without any fork or core changes to
hermes-agent.

---

## Decision

Replace the per-mention sentinel-subprocess pattern for Hermes agents with a
first-class **platform plugin** (`plugins/platforms/ax/`) that registers
the platform as a native Hermes messaging platform.

**Process model:** A single long-lived `hermes gateway run` process serves an
agent continuously. @-mentions arrive via SSE; the `AxAdapter` normalizes them
into Hermes `MessageEvent` objects; Hermes handles the full agentic loop; the
adapter posts replies via REST, threaded under the original mention.

**Key design choices:**

- `SUPPORTS_MESSAGE_EDITING = False` — streaming intermediate edits to a chat
  bubble creates noisy, duplicate messages. Hermes activity updates are routed
  to the original mention's processing-status stream instead (see
  `SUPPORTS_ACTIVITY_STATUS` below).

- `SUPPORTS_ACTIVITY_STATUS = True` — tool call activity (bash invocations,
  file reads, web searches) appears on the original mention's activity bubble,
  not as separate chat messages. The intent is that agents are working, not
  narrating step by step.

- **One AxAdapter = one identity.** Each `AxAdapter` instance is bound to one
  agent PAT and one space. Gateway registers one adapter per agent-in-space.
  Multiple agents on the same host each get a separate `hermes gateway run`
  process.

- **Inbound dedup by message ID.** The platform can emit both `message` and
  `mention` events for the same message. The adapter records recent IDs before
  dispatch so a single @-mention does not trigger two agent turns.

- **Mention normalization.** Users type `@nova /command`; Hermes
  slash-command detection expects `/command`. The adapter strips the leading
  addressed mention before handing text to `handle_message()`.

- **Home channel auto-default.** `_env_enablement` in `adapter.py`
  auto-defaults `AX_HOME_CHANNEL` to `AX_SPACE_ID`, preventing the
  "No home channel set" prompt on first mention without operator intervention.

**Approval gate:** Hermes's default `approvals.mode: on` fires before
dangerous bash commands. The approval request posts to the agent's channel,
where an operator can approve or deny. This is the primary interactive safety
gate for `hermes_plugin` agents — structurally different from `sentinel_cli`
agents, which rely on the Claude Code permissions profile system (ADR-011/012).

**Gateway supervision:** The `hermes_plugin` runtime type in Gateway manages
the lifecycle of the `hermes gateway run` process — start, stop, restart,
health check — via the standard `ax gateway agents start/stop` CLI and the
Gateway UI Start/Stop buttons.

---

## Relationship to sentinel_inference_sdk and sentinel_hermes_sdk (ADR-012)

These are parallel, not overlapping runtimes:

| Runtime | Shape | Agent model | Tool authorization |
| --- | --- | --- | --- |
| `hermes_plugin` | Long-lived `hermes gateway run` process | Full Hermes agentic loop | Hermes approval gates + tool policy |
| `sentinel_hermes_sdk` | Long-lived `sentinel.py` daemon, in-process Hermes AIAgent loop | 90-turn agentic loop; Bedrock, OpenRouter, Anthropic, Codex backends | Connector policy + `_secure_hermes_tools` |
| `sentinel_inference_sdk` | Long-lived `sentinel.py` daemon, SDK-direct API calls | Lightweight single-turn API call, no local framework | Connector policy (`allowed_tools`/`denied_tools`) |

`sentinel_inference_sdk` dispatches to direct vendor SDKs (OpenAI, Groq, Mistral,
Gemini, Leapfrog). No `hermes_plugin` equivalent exists for those providers.
`sentinel_hermes_sdk` carries the in-process Hermes loop and Bedrock IAM
support that previously lived as `hermes_sdk` inside `sentinel_inference_sdk`;
ADR-012 decision 5 promotes it to its own runtime type because its shape
(full agentic loop, custom tool security, framework dependency) is
architecturally distinct from a lightweight vendor API call. See
[ADR-012](ADR-012-vendor-sdk-security-cleanup.md) for the full boundary
analysis.

---

## Consequences

**Replaces:** The per-mention sentinel-subprocess pattern for Hermes agents.
`ax_cli/runtimes/hermes/sentinel.py` is preserved as a compatibility baseline
but is no longer the recommended path for Hermes agents.

**Known limitations at adoption:**

| # | Issue | Severity | Status |
| --- | --- | --- | --- |
| 13 | "final stream delivery not confirmed" warning logs even on successful delivery | low | open |
| 14 | Workdir not shown on the agent row in Gateway UI | UX nice-to-have | open |
| 15 | `terminal.cwd` is soft confinement; absolute paths still work | medium — needs `terminal.backend: docker` for prod | open |
| 16 | Legacy sentinel path had the same sandboxing gap as #15 | medium | resolved — CLI subprocess paths removed in ADR-012 |
| 17 | Gateway UI Start/Stop buttons are no-ops for `hermes_plugin` agents | UX gap | resolved — `_reconcile_runtime` wires `desired_state` to `_start_hermes_plugin_process`; UI start/stop buttons call the handler that sets `desired_state` |

**Operational documentation:** [SETUP-HERMES.md](../SETUP-HERMES.md) is the
canonical operator guide — setup, identity configuration, sandboxing,
troubleshooting, and EC2 deployment notes are maintained there.
