# Gateway Composio Connectors

Use this skill when a user or agent needs to set up, configure, or use outbound tool connectors through Gateway.

## When to use

- Setting up a new connector for third-party tool access (GitHub, Jira, Slack, etc.)
- Configuring tool policy (allow/deny lists) for a connector
- Writing or managing auth credentials for a connector
- Searching for or executing tools through a connector
- Setting up an HTTP MCP connector for self-hosted servers

## Setup flow

1. **Create** the connector:
   ```bash
   ax gateway connectors add <name> --provider composio --managed-auth
   ```

2. **Write auth** credentials:
   ```bash
   ax gateway connectors auth write <name> COMPOSIO_API_KEY=<key>
   ```

3. **Configure** (optional):
   ```bash
   ax gateway connectors set <name> entity_id <entity>
   ax gateway connectors set <name> allowed_tools '["GITHUB_*"]'
   ```

4. **Discover** tools:
   ```bash
   ax gateway connectors tools search <name> --use-case "list pull requests"
   ax gateway connectors tools list <name>
   ```

5. **Execute** a tool:
   ```bash
   ax gateway connectors call <name> --tool GITHUB_LIST_PRS --args-json '{}'
   ```

## Providers

- **composio** — Composio SaaS (500+ integrations). Requires `COMPOSIO_API_KEY`.
- **http_mcp** — Any MCP-compliant server. Requires `base_url` in config.

## Key principles

- Gateway is the trust boundary. Credentials stay in managed auth files (0o600), never in config or logs.
- Tool policy (`allowed_tools`, `denied_tools`) should be set for production connectors.
- All tool executions are logged to `activity.jsonl` for audit.
- Use `--json` flag on any command for machine-readable output.

## Reference

See `docs/composio-integration.md` for full configuration keys, security checklist, and troubleshooting.
