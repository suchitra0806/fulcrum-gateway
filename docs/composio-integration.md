# Composio Integration Guide

Outbound connectors let managed agents invoke third-party tools (GitHub, Jira, Slack, Salesforce, etc.) through Gateway as the trust boundary. This guide covers setup, configuration, and security for the Composio provider.

## Prerequisites

- `ax-cli` installed (`pip install axctl`)
- A running Gateway (`ax gateway up`)
- A [Composio](https://composio.dev) API key

## Quick start

```bash
# 1. Create a connector with managed auth
ax gateway connectors add composio-main --provider composio --managed-auth

# 2. Write credentials (stored at ~/.ax/gateway/connectors/auth/<id>.env, 0o600)
ax gateway connectors auth write composio-main COMPOSIO_API_KEY=ak_your_key_here

# 3. Verify auth
ax gateway connectors auth status composio-main

# 4. Search for tools
ax gateway connectors tools search composio-main --use-case "list github pull requests"

# 5. Execute a tool
ax gateway connectors call composio-main --tool GITHUB_LIST_PRS --args-json '{"owner": "org", "repo": "repo"}'
```

## Providers

```bash
ax gateway connectors providers
```

| Provider | Capabilities | Use case |
|---|---|---|
| `composio` | execute, list_tools, intent_search | 500+ SaaS integrations via Composio API |
| `http_mcp` | execute, list_tools | Self-hosted MCP servers (GovCloud, air-gapped) |

## Configuration keys

Set config values with:
```bash
ax gateway connectors set <ref> <key> <value>
```

### Composio config

| Key | Default | Description |
|---|---|---|
| `composio_base_url` | `https://backend.composio.dev/api/v3` | Composio API base URL |
| `entity_id` | `default` | Composio entity ID for account resolution |
| `connected_account_id` | (none) | Direct account ID (overrides entity_id) |
| `app_name` | (none) | Default app for execute calls |
| `classification` | (none) | IL classification tag (enforcement in PR 3) |

### HTTP MCP config

| Key | Default | Description |
|---|---|---|
| `base_url` | (required) | MCP server endpoint |
| `auth_header_name` | `Authorization` | Header name for API key |
| `auth_prefix` | `Bearer` | Prefix before API key value |

## Tool policy

Restrict which tools agents can discover and execute using fnmatch patterns:

```bash
# Allow only GitHub and Jira tools
ax gateway connectors set composio-main allowed_tools '["GITHUB_*", "JIRA_*"]'

# Block destructive operations
ax gateway connectors set composio-main denied_tools '["*_DELETE_*", "*_REMOVE_*"]'

# Filter by toolkit
ax gateway connectors set composio-main allowed_toolkits '["github", "jira"]'

# Cap discovery results
ax gateway connectors set composio-main tools_limit 100
```

**Semantics:**
- `allowed_tools`: if set, tool name must match at least one pattern
- `denied_tools`: tool name must NOT match any pattern
- Deny always takes precedence over allow
- Policy is enforced at both discovery (`tools list`/`tools search`) and execution (`call`)

## Auth management

Credentials are stored in `~/.ax/gateway/connectors/auth/<id>.env` with `0o600` permissions. Gateway brokers credentials — they never appear in connector config, registry JSON, logs, or agent messages.

```bash
# Write
ax gateway connectors auth write composio-main COMPOSIO_API_KEY=ak_xxx

# Status (shows key names, never values)
ax gateway connectors auth status composio-main

# Clear
ax gateway connectors auth clear composio-main
```

## Activity and audit

Every `connectors call` records events to `~/.ax/gateway/activity.jsonl`:

- `connector_tool_started` — tool invocation began
- `connector_tool_completed` — tool returned successfully (includes `duration_ms`)
- `connector_tool_failed` — tool invocation failed

View with:
```bash
ax gateway activity
ax gateway activity --agent my-agent
```

## Security checklist

- [ ] API keys stored via managed auth (`connectors auth write`), not in config or env
- [ ] Auth files have 0o600 permissions (automatic)
- [ ] Tool policy configured (`allowed_tools` / `denied_tools`) for production connectors
- [ ] Activity logging enabled (automatic via `activity.jsonl`)
- [ ] `connectors show` and `auth status` never expose credential values
- [ ] Connector registry (`connectors.json`) contains no secrets

## Troubleshooting

**"COMPOSIO_API_KEY not found"**
Run `ax gateway connectors auth write <ref> COMPOSIO_API_KEY=<key>`

**"Tool blocked by policy"**
Check policy: `ax gateway connectors show <ref> --json | jq .config`

**"No adapter for provider"**
Verify provider: `ax gateway connectors providers`

**Timeout errors**
Default timeouts: 10s connect, 30s read. For slow integrations, this is not yet configurable per-connector.

**HTTP MCP "base_url not configured"**
Run `ax gateway connectors set <ref> base_url <url>`
