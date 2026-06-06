# Agent Permission Model

**Status:** Reference — current state as of 2026-06-05
**Author:** Mark Galpin

This document describes how Gateway controls what actions agents are permitted to take, across all supported runtime types. It covers the distinct permission layers, how they interact, where each is enforced, and the current gaps.

---

## Mental model: three orthogonal layers

Agent permission is not a single system — it is three independent layers that apply simultaneously:

| Layer | What it controls | Where enforced |
|---|---|---|
| **Capability authorization** | Which tools/operations the agent may invoke | Claude Code settings, `--allowedTools`, connector policy |
| **Execution environment** | What credentials and filesystem paths exist | Process environment, IAM role, container image |
| **Network/infrastructure** | Which endpoints are reachable | Network policy, IAM resource policies |

A complete permission model requires all three. Profiles and connector policy address the first layer. The second and third layers require environmental controls (injected credentials, IAM scoping, Kubernetes NetworkPolicy) that operate outside Gateway's configuration surface.

The critical implication: **a container boundary provides process and filesystem isolation but does not replace capability authorization**. An agent running in a container can still invoke MCP tools, connector tools, and shell commands that make outbound network calls. Profiles and connector policy define which of those calls are permitted — the container boundary does not.

---

## Two separate "approval" systems

Gateway has two distinct systems that use the word "approval." They are unrelated.

### 1. Gateway registration/binding approvals

Managed in the gateway registry (`approval_state: pending | approved | rejected`). This governs whether an agent's device binding — the fingerprint of the origin machine and process that registered the agent — has been verified by the operator. Surfaced in the gateway daemon UI at `http://127.0.0.1:8765`.

This is an identity governance mechanism, not a tool permission mechanism.

### 2. Claude Code tool approval dialogs

When Claude Code runs interactively, it presents a per-tool approval dialog to the human at the terminal before executing certain operations. This is a human-in-the-loop mechanism for supervised sessions.

**Headless agents cannot use this mechanism.** In headless (`-p`/`--print`) mode, there is no terminal and no human. Gateway has no mechanism to receive a Claude Code tool approval request, route it to the platform UI, wait for a response, and send it back to Claude. The stream-json event format that Gateway reads (`assistant`, `content_block_delta`, `result`) contains no `permission_request` event type.

The correct model for headless agents is **pre-authorization**: define permitted tools upfront, and the agent operates autonomously within that boundary. This is what `--allowedTools`, `settings.local.json` permissions, and connector policy all provide.

---

## Per-runtime permission model

### `claude_code_channel` — attached session

The operator's Claude Code terminal provides interactive approval for any tool use. This is the only runtime where per-call human approval is practical.

Additional pre-authorization is provided by `settings.local.json` in the agent workdir:

```json
{
  "permissions": {
    "allow": ["mcp__ax-channel__*", "mcp__composio__GITHUB_*"],
    "deny":  ["mcp__composio__GITHUB_*DELETE*"]
  }
}
```

- **`allow`** — tools in this list are pre-authorized; no approval dialog for them
- **`deny`** — tools in this list are blocked even if otherwise allowed; deny takes precedence
- Patterns use fnmatch glob syntax; MCP tools are namespaced `mcp__<server>__<tool>`
- Multiple MCP servers can be addressed independently in the same file
- `settings.local.json` is per-agent-workdir — one file per agent identity

The `profiles` system (`ax agents profiles apply`) manages this file. A profile is a named JSON fragment that deep-merges into `settings.local.json`. The `base` profile for `claude_code_channel` grants `mcp__ax-channel__*` — the minimum needed for the agent to receive and reply to messages via the platform.

### `sentinel_cli` (Claude) — headless Claude CLI subprocess

Gateway spawns `claude -p --output-format stream-json` as a subprocess per message, optionally resuming a saved session.

Two permission flags apply:

- **`--dangerously-skip-permissions`** — hardcoded in every sentinel_cli invocation. Bypasses Claude Code's interactive approval dialog. Required because there is no human at a terminal.
- **`--allowedTools <list>`** — injected from the agent registry entry's `allowed_tools` field when set. Restricts which tools Claude is offered. If Bash is not in the list, no shell commands can be run regardless of what credentials exist in the environment.

**Current gap:** `allowed_tools` is optional. When unset, `--dangerously-skip-permissions` means the agent has unrestricted tool access — any tool Claude Code supports, including Bash, which can invoke any command the process has credentials for. This is the primary unresolved security issue with `sentinel_cli`.

`settings.local.json` in the agent workdir also applies (Claude reads it from `cwd`). The `deny` list in the settings file is enforced at the tool-offering layer independently of `--dangerously-skip-permissions`.

The `profiles` system can address this by writing `settings.local.json` to the workdir, and a future extension could populate `allowed_tools` in the registry entry from a profile's `allow` list.

### `sentinel_cli` (Codex) — headless Codex CLI subprocess

Gateway spawns `codex exec --json` as a subprocess. Codex's permission model is **sandbox-based**, not tool-name-based. Codex does not expose named tools — it executes shell commands, so there is no `--allowedTools` equivalent.

Permission flags:

- **`--dangerously-bypass-approvals-and-sandbox`** — hardcoded. Disables both the sandbox and approval prompts entirely.
- **`--sandbox workspace-write --ask-for-approval never`** — safer alternative, available via `CODEX_SANDBOX=workspace-write` env var on the agent. Restricts filesystem writes to the agent workdir; reads are allowed anywhere; no approval prompts.

**Important:** `workspace-write` restricts filesystem write scope only. It does not:
- Prevent reading credentials from `~/.aws/credentials` or elsewhere on the filesystem
- Restrict environment variables intrinsic to the process (`AWS_ACCESS_KEY_ID`, etc.)
- Restrict network access — shell commands can call any reachable endpoint

The correct permission model for Codex agents is environmental: do not inject credentials for systems the agent should not be able to modify. IAM role scoping (in containerized deployments) is the authoritative enforcement layer.

The `profiles` system as designed does not apply to Codex. There is no settings file to write and no named-tool allowlist to populate.

### `hermes_plugin` — long-lived Hermes process

Gateway scaffolds `<workdir>/.hermes/` and spawns `hermes gateway run` as a long-lived process. The agent communicates with the aX platform via the bundled ax-platform plugin.

**Hermes plugin agents have no Bash tool by default.** All tools available to the agent come through the Gateway connector system. An agent without a `connector_ref` binding has no outbound tools.

Tool access is controlled by connector policy — see the Connector Model section below.

`hermes_plugin` is immune to the `--dangerously-skip-permissions` problem because it does not use the Claude CLI subprocess model at all.

### `hermes_sentinel` — legacy, deprecated

Uses the same `--dangerously-skip-permissions` model as `sentinel_cli` (Claude). Formally deprecated in the runtime catalog (`deprecated: true, successor_runtime_type: hermes_plugin`). Existing agents should be migrated with `ax gateway agents update <name> --type hermes_plugin`.

### SDK runtimes (`openai_sdk`, `gemini_sdk`, `groq_sdk`, `mistral_sdk`, `leapfrog_sdk`, `xai_sdk`, `palantir_sdk`, `scale_sdk`)

These runtimes call vendor LLM APIs directly (not via Claude CLI). They run as subprocesses under the `hermes_sentinel` supervisor but do not use `--dangerously-skip-permissions`.

Tool access is controlled by the same Gateway connector system as `hermes_plugin`. The model is identical: `connector_search` and `connector_call` are meta-tools the agent uses to discover and invoke connector-backed tools, with connector policy enforced at both search time and call time.

### `exec` and `bedrock_agentcore` — command bridge runtimes

`exec` runs an arbitrary script as a subprocess. `bedrock_agentcore` runs a bridge that invokes Amazon Bedrock AgentCore Runtime via boto3.

Neither has a Gateway-managed tool permission layer. Permission is determined by:
- What the bridge script is written to do
- The AWS IAM role or credentials the process runs with (`bedrock_agentcore` uses `AWS_PROFILE` from the registry entry)

---

## The connector model

Connectors are Gateway-managed outbound tool adapters. They are independent resources (`~/.ax/gateway/connectors.json`) that agents reference by name.

### Provider types

Two provider types are supported:

| Provider | What it connects to |
|---|---|
| `composio` | Composio HTTP API — 500+ SaaS integrations; tool names are Composio slugs (`GITHUB_CREATE_PULL_REQUEST`, etc.) |
| `http_mcp` | Any MCP-compliant server via JSON-RPC (`tools/list`, `tools/call`); tool names are whatever the server defines |

An agent bound to either provider type uses `connector_search` and `connector_call` identically — the provider is an implementation detail transparent to the runtime.

### Tool policy

Each connector has four independent policy fields:

| Field | Type | Behavior |
|---|---|---|
| `allowed_tools` | fnmatch patterns | Only tools matching at least one pattern are available |
| `denied_tools` | fnmatch patterns | Tools matching any pattern are blocked; checked before allow |
| `allowed_toolkits` | fnmatch patterns | Only tools whose app/toolkit field matches are available |
| `denied_toolkits` | fnmatch patterns | Tools from matching apps are blocked |

Policy is evaluated at two points:
- **Search time** (`connector_search`) — filtered tools are returned; blocked tools are invisible to the model
- **Call time** (`connector_call`) — `assert_tool_allowed()` raises `ConnectorPolicyError` if the tool doesn't pass policy; this is a hard enforcement, not just filtering

Deny is checked before allow. Allow is only applied when set — an empty allow list means "all tools permitted" (subject to deny). This is consistent with the principle of least surprise: adding an allow list restricts, removing it opens.

### Granularity

For Composio: granularity is at the individual operation level. Examples:
- `allowed_tools: ["GITHUB_GET_*", "GITHUB_LIST_*"]` — read-only GitHub access
- `denied_tools: ["*DELETE*", "*MERGE*", "*CLOSE*"]` — block destructive operations across all apps
- Combined: `allowed_tools: ["GITHUB_*"]` + `denied_tools: ["GITHUB_DELETE*"]` — all GitHub except delete

For `http_mcp`: granularity is whatever tool names the MCP server exposes. If the server names its tools `read_record`, `write_record`, `delete_record`, you can use `allowed_tools: ["read_*"]` or `denied_tools: ["delete_*"]`.

### Per-agent isolation

Connectors are global resources by default — multiple agents can share one connector and its policy. Per-agent tool isolation requires a dedicated connector per agent:

```bash
ax gateway connectors add orion-tools --provider composio
ax gateway connectors set orion-tools allowed_tools '["GITHUB_GET_*", "JIRA_GET_*"]'
ax gateway agents add orion --template hermes --connector-ref orion-tools
```

There is no limit on connector count. The `connector_ref` field on an agent entry binds it to a specific connector.

---

## MCP servers and Claude Code

For `claude_code_channel` and `sentinel_cli` (Claude), MCP servers are configured in `.mcp.json` in the agent workdir. Each server's tools appear in Claude Code's permission namespace as `mcp__<server-name>__<tool-name>`.

`settings.local.json` allow/deny patterns address these names directly. This gives individual tool-level control across multiple MCP servers simultaneously, with server namespacing preventing conflicts between servers that expose tools with the same name.

Enforcement is client-side — Claude Code will not invoke a tool that fails the allow/deny check, regardless of what the MCP server exposes. The MCP server can additionally enforce its own authentication layer, providing two independent enforcement points.

---

## Capability authorization vs environment credentials

`--allowedTools` and `settings.local.json` prevent the agent from *invoking* unauthorized tools. They do not prevent an authorized tool (Bash) from using credentials that happen to be present in the execution environment.

Example: an agent with `--allowedTools Bash` and `AWS_SECRET_ACCESS_KEY` in its environment can run `aws ec2 terminate-instances` even if you intended Bash only for local file operations. The tool is permitted; what the tool does with ambient credentials is not controlled by the permission layer.

The implication: for agents that have Bash in their tool allowlist, environmental credential scoping is as important as the tool allowlist. The deployment model determines what credentials the process inherits:

- **Desktop model** — agent inherits the operator's shell environment; avoid sourcing production credentials in that environment
- **Containerized model** — pod-scoped IAM role via Kubernetes service account; the process identity only has the permissions its role grants, regardless of what commands it runs

Connectors are not subject to this concern: connector tools are invoked through Gateway, which holds the connector credentials, not the agent process. The agent calls `connector_call("my-connector", "GITHUB_CREATE_PR", args)` — it never sees the API key.

---

## Current gaps

| Gap | Affected runtimes | Severity |
|---|---|---|
| `--dangerously-skip-permissions` with no `allowed_tools` | `sentinel_cli` (Claude), `hermes_sentinel` | High — unrestricted tool access |
| `--dangerously-bypass-approvals-and-sandbox` with no sandbox | `sentinel_cli` (Codex) | High — unrestricted shell + filesystem |
| `allowed_tools` field not wired for `hermes_plugin` | `hermes_plugin` | Low — no Bash by default; covered by connector policy |
| Connector policy is per-connector, not per-agent by default | All Hermes/SDK runtimes | Medium — shared connectors share policy |
| No platform-level ACL on which profiles/connectors a user may apply | All runtimes | Gap in the agent-creation product model |
| `profiles` system only manages `permissions.allow`; `deny` list unsupported | `claude_code_channel` | Low — deny list would add defense-in-depth |

---

## Direction: profiles and agent classes

The `ax agents profiles` system (`feat/agents-profiles`) is the management surface for capability authorization. A profile is a named JSON fragment that describes a permission configuration for a specific runtime.

For `claude_code_channel`, profiles write `settings.local.json` fragments into the agent workdir. For `sentinel_cli`, profiles could populate the `allowed_tools` registry field (injected as `--allowedTools` at launch). For Hermes agents, the equivalent artifact is a connector instance with a specific tool policy.

Two levels are envisioned:

**Profiles** — runtime-specific permission fragments. Applied to a specific agent workdir or registry entry. Examples: `claude_code_channel/base` (grants `mcp__ax-channel__*`), `claude_code_channel/github_readonly` (grants `mcp__ax-channel__*` + specific GitHub read tools).

**Agent classes** — higher-level bundles that specify a complete capability envelope: runtime type, profile(s) to apply, connector to bind and its tool policy, system prompt, model. An agent class is what the platform UI offers to users; the deploy command materializes it into a live agent.

Platform-level ACLs on which agent classes a given user may instantiate are planned but not yet specified.
