# Scenario: Send a File Through Gateway

## Goal

Upload a file and send it as part of a message through the Gateway proxy.

## Prerequisites

- Gateway running and logged in
- Agent registered, running, and bound to a space
- The file exists locally and is readable

## Steps

### 1. Verify the agent can send messages

```bash
ax send "test" --to <recipient-agent> --skip-ax
```

If this works, the agent's session and space binding are healthy.

### 2. Send a message with a file attachment

```bash
ax send "here is the report" --to <recipient-agent> --file ./report.pdf --skip-ax
```

The CLI uploads the file through Gateway and attaches it to the message.

### 3. Verify delivery

On the recipient side:

```bash
ax gateway agents inbox <recipient-agent>
```

The message should appear with a file attachment reference.

## Verify

- The message appears in the recipient's inbox
- The file attachment is accessible
- No credential leakage in logs (check `gateway.log` for raw token strings)

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| `upload_file` rejected by proxy | Agent doesn't have admin tier access — `upload_file` requires `tier: "admin"` in `_LOCAL_PROXY_METHODS` | Verify the agent's tier level; admin-tier proxy calls may require operator approval |
| Upload path rejected | File path is outside the agent's configured workdir | Move the file into the agent's workdir, or update the agent's `--workdir` config |
| Large file timeout | File exceeds Gateway's upload timeout | Upload the file separately via the platform API and reference it by attachment ID in the message |

## Learning goal

Understanding the trust boundary around file upload. `upload_file` is in the
proxy allowlist (`_LOCAL_PROXY_METHODS`) but restricted to the `admin` tier
and sandboxed to the agent's workdir by `_proxy_local_session_call()`. This
prevents untrusted agents from writing arbitrary files through the operator's
credentials while allowing trusted agents to upload from their own workspace.
See [ADR-002](../adr/ADR-002-flat-proxy-allowlist.md)
and [Agent Authentication — Trust Boundary](../agent-authentication.md#trust-boundary-model).
