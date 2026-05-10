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
| `upload_file` rejected by proxy | `upload_file` is not in `_LOCAL_PROXY_METHODS` — it goes through a dedicated endpoint, not the generic proxy | Use the `--file` flag on `ax send` which routes through the correct endpoint |
| "Method not on Gateway proxy allowlist" | Trying to call `upload_file` via `/local/proxy` directly | This is by design — `upload_file` without path restriction is a trust boundary violation for inbox agents. Use the dedicated send endpoint |
| Large file timeout | File exceeds the default timeout | Check if `--timeout` flag is available, or upload separately and reference by ID |

## Learning goal

Understanding the trust boundary around file upload. `upload_file` is
intentionally excluded from the generic proxy allowlist (`_LOCAL_PROXY_METHODS`)
because granting it to all agents — including untrusted inbox agents — would
let any local agent write arbitrary files through the operator's credentials.
Trusted send operations go through the dedicated `/local/send` endpoint with
additional validation. See [ADR-002](../adr/ADR-002-flat-proxy-allowlist.md)
and [Agent Authentication — Trust Boundary](../agent-authentication.md#trust-boundary-model).
