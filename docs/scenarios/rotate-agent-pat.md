# Scenario: Rotate an Agent's PAT

## Goal

Replace a managed agent's PAT credential with a fresh one, verify the
replacement works, then revoke the old credential.

## Prerequisites

- Gateway running and logged in (`ax gateway status` shows running)
- Agent registered and working (`ax gateway agents show <agent>`)
- User PAT with permission to mint and revoke credentials

## Steps

### 1. Inventory existing credentials

```bash
axctl credentials list --json
```

Find the credential entry for your agent. Note the `credential_id` of the
current active key.

### 2. Audit the key state

```bash
axctl credentials audit
```

**Expected:** One active key per agent. If you see two active keys for the same
agent, clean up the stale key before proceeding — having more than two active
PATs for one agent is a security hygiene issue.

For stricter checking in automation:

```bash
axctl credentials audit --strict
```

### 3. Mint a replacement PAT

```bash
axctl token mint <agent-name> \
  --audience <cli|mcp|both> \
  --expires 90 \
  --save-to ~/.ax/gateway/agents/<agent-name>/token.new \
  --profile <profile-name> \
  --no-print-token
```

This creates a new agent-scoped PAT and saves it to a file. The
`--no-print-token` flag keeps the raw token out of terminal history and logs.

**Expected:** Confirmation that the token was minted and saved.

> **Important:** Do NOT revoke the old credential yet. You now have two active
> PATs for this agent — this is acceptable only during the rotation window.

### 4. Verify the new credential

```bash
axctl profile verify <profile-name>
```

Check that the new profile passes all three verifications (token SHA-256,
hostname, workdir):

```bash
axctl auth whoami --json
```

**Expected:** The output shows the correct agent name and space.

### 5. Test the agent with the new credential

```bash
ax send "rotation test" --to <agent-name> --skip-ax
ax gateway agents inbox <agent-name>
```

Verify messages flow correctly using the new credential.

### 6. Revoke the old credential

Only after confirming the new credential works:

```bash
axctl credentials revoke <old-credential-id>
```

**Expected:** Confirmation that the old credential was revoked.

### 7. Final audit

```bash
axctl credentials audit
```

**Expected:** Exactly one active key for the agent (the new one).

## Verify

- `credentials audit` shows one active key per agent
- Agent continues to process messages after the old key is revoked
- No errors in `gateway.log` related to authentication

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| Agent stops working after revoke | Revoked the new key instead of the old one | Mint another replacement immediately |
| "Two active PATs" warning persists | Forgot to revoke the old key | Complete step 6 |
| More than two active PATs | Multiple incomplete rotations | `credentials list --json` to identify and revoke all stale keys, keep only the newest |
| Wrong audience on new token | Mismatched `--audience` flag | Revoke the bad token, re-mint with correct audience (`cli`, `mcp`, or `both`) |
| Profile verification fails | Token file path or workdir mismatch | Re-run `ax profile add` to rebind the profile to the new token file |

## Learning goal

Understanding the credential rotation lifecycle: the safe order is always
**mint → verify → test → revoke**. Never revoke first. A rotation is complete
only when the replacement token works and the previous credential is revoked.
See [Credential Security](../credential-security.md) for the full security
model.
