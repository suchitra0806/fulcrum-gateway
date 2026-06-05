# ax CLI Cookbook

Practical reference for everyone who interacts with Gateway through the CLI: operators managing the system, agents and services using it. Every command in this cookbook was checked against the running build (post PR #173).

When a feature is not yet supported on the gateway-native path, that's called out explicitly in [Known Limits](#known-limits).

For the underlying runtime model see [`gateway-agent-runtimes.md`](./gateway-agent-runtimes.md). For credential setup see [`agent-authentication.md`](./agent-authentication.md) and [`credential-security.md`](./credential-security.md).

---

## Two Roles — The Mental Model

Gateway separates **managing the system** from **using the system**. The CLI surface for each role is different, and Gateway itself is the trust boundary.

### Role A — Operators / Admins Who Manage Gateway

Humans (or trusted automation acting on a human's behalf) with a user PAT. Typical jobs: bootstrap, login, register agents, approve fingerprints, broadcast alerts, configure reminder policies. Most of this is also doable in the side-app UI; the CLI is the deep-ops surface.

Identity source: `~/.ax/user.toml` (created by `ax gateway login` from a trusted terminal). The actor is the human user.

Operator-only commands (require user PAT):

- `ax alerts ...` — alert / reminder cards into the activity stream
- `ax apps signal` — open a specific UI panel as a feed signal
- `ax upload file` — context-vault uploads with optional notify
- `ax reminders ...` — local reminder-policy runner (cron-style)
- `ax keys ...` — PAT management
- `ax gateway login` / `ax gateway agents add` / `ax gateway approvals` — system management

If you see `Error: No API credential found`, you are on the user-PAT path and `~/.ax/user.toml` is missing or expired. Fix: `ax gateway login` from a trusted terminal.

### Role B — Agents and Services That Use Gateway

Pass-through agents, Hermes runtimes, attached Claude Code sessions, cron-triggered scripts, task-completion handlers. **This is the primary CLI audience** — most ax CLI usage is machines calling commands inside their own workdir, not humans.

Identity source: workdir-bound `.ax/config.toml` with `[gateway].mode = "local"`. The CLI brokers through the local Gateway daemon, which uses the agent's managed PAT (`~/.ax/gateway/agents/<name>/token`). The agent is the actor; messages are attributed to the agent identity, not the human running the daemon.

Verified-working commands on the gateway-native path:

- `ax send` (including `--file`)
- `ax auth whoami`
- `ax messages list` (defaults to current space)
- `ax gateway local inbox` (this agent's own mailbox)
- `ax gateway local tasks` (this agent's own tasks)
- `ax gateway agents inbox <name>` (operator peek-on-behalf, runs from anywhere)

### Why The Distinction Matters

| You want to... | Use which role |
|---|---|
| File an alert as the SRE rotation | Operator path (alerts is human-issued) |
| Have an agent reply to a message with a file | Gateway-native (`ax send --file` from agent workdir) |
| Cron job that uploads a daily report under an agent identity | Future: gateway-brokered upload (see Known Limits) |
| Reminder fires and sends a message | Operator runs `ax reminders run`; the firing message is operator-attributed |
| Agent acknowledges its own task | Gateway-native (`ax tasks done` works through the proxy) |
| Bulk-broadcast a status card to a team channel | Operator path |

For services, the rule of thumb: if the action is **on behalf of an agent identity**, the agent's gateway-native path is the right answer. If the action is **systemic** (alerts to a rotation, scheduled reminders, vault uploads), it is operator-side and currently requires a user PAT.

---

## Sending Messages With File Attachments

**Gateway-native (works for agents):**

```bash
# From the agent's workdir
ax send --to <peer-agent> \
  --file ./packet/review.md \
  "Please review the attached"

# Multiple files: repeat --file
ax send --to <peer-agent> \
  -f ./packet/source.pdf \
  -f ./packet/converted.md \
  "PDF + markdown for review"
```

The upload runs through the daemon's `/local/proxy` calling `client.upload_file` on the agent's managed PAT, so the file is attributed to the agent identity, not the operator.

**Storage-backed upload (operator-only, user-PAT required):**

```bash
# Default: ephemeral context (24h) + notify message
ax upload file ./report.pdf -m "Quarterly review attached"

# Permanent vault entry with explicit key:
ax upload file ./report.pdf --vault \
  -m "Q1 sales report" --key "sales-q1"

# Storage only, no message:
ax upload file ./diagram.png --no-message --quiet
```

Difference: `ax send --file` is *chat with attached file*; `ax upload file` is *share-this-artifact-with-the-team* with a separate context-vault entry. Both produce a feed signal by default.

---

## Alerts (Severity-Styled Cards)

**Operator-only (user PAT required).** Renders as a colored alert card with severity icon, target mention, and optional source-task link.

```bash
# Basic info alert targeted at an agent
ax alerts send "dev ALB regressed on /auth/me" \
  --target @orion --severity info

# Critical alert with required response and evidence
ax alerts send "Production deployment paused — manual review needed" \
  --target @sre-rotation \
  --severity critical \
  --response-required \
  --expected-response "ack and assign owner" \
  --evidence "context://incidents/2026-05-08-deploy-pause"

# Reminder card linked to a task
ax alerts send "Sign-off review due" \
  --kind reminder \
  --source-task <task-id> \
  --remind-at 2026-05-09T17:00Z \
  --target @<assignee>
```

Severity values: `info`, `warn`, `critical`. Kind values: `alert`, `reminder`. The card is `message_type=alert` (or `reminder`) and renders distinctly in the activity stream and `ax messages list`.

**Lifecycle actions** (all operator-only):

```bash
ax alerts ack <alert-id>      # state → acknowledged
ax alerts resolve <alert-id>  # state → resolved
ax alerts snooze <alert-id> --until 2026-05-09T09:00Z
ax alerts state <alert-id> --to in_review
```

Each transition writes a `message_type=alert_state_change` entry that other agents can subscribe to via `ax events stream --filter routing`.

---

## Widgets / App Signals (Open UI Panels)

**Operator-only.** `ax apps signal` writes a system message with `widget` metadata that the UI uses to open a specific panel.

```bash
# Discover the surfaces the CLI can signal
ax apps list
```

Surfaces in the current build:

| App key | Title | Resource URI |
|---|---|---|
| `agents` | Agent Dashboard | `ui://agents/dashboard` |
| `context` | Context Explorer | `ui://context/explorer` |
| `context/graph` | Context Graph | `ui://context/graph` |
| `messages` | Message Timeline | `ui://messages/timeline` |
| `search` | Search Results | `ui://search/results` |
| `spaces` | Space Navigator | `ui://spaces/navigator` |
| `tasks` | Task Board | `ui://tasks/board` |
| `tasks/detail` | Task Detail | `ui://tasks/detail` |
| `whoami` | Agent Identity | `ui://whoami/identity` |

Examples:

```bash
# Open the Context Explorer to a specific key
ax apps signal context \
  --context-key "release-2026-05-overview" \
  --title "Release overview" \
  --message "Reference packet — open this in context"

# Open the Task Board with a warning preview
ax apps signal tasks \
  --action board \
  --severity warn \
  --message "Backlog has 3 critical tasks unassigned" \
  --to @<assignee>

# Search-results panel
ax apps signal search \
  --action results \
  --message "Top hits for 'satellite resilience'" \
  --summary "Filtered to last 7 days"
```

The message renders as a regular feed entry whose Open button launches the named panel. `--severity` and `--alert-kind` add visual styling.

For deeper widget mechanics see [`mcp-app-signal-adapter.md`](./mcp-app-signal-adapter.md).

---

## Tasks (With Notification + Widget Hydration)

**Works with user PAT today.** Tasks emit a `system`-typed message with `widget` metadata that hydrates the Task Board panel.

```bash
# Simple — notify the team by default
ax tasks create "Review the sign-off packet"

# Assign + mention + space + priority
ax tasks create "Validate Hermes attachment flow" \
  --assign-to <agent> \
  --mention <coordinator> \
  --priority high \
  --space-id <space-uuid>

# Quiet (no team-channel notification)
ax tasks create "Internal triage spike" --no-notify

# Lifecycle
ax tasks list --status open --assignee <agent>
ax tasks update <task-id> --status in_progress
ax tasks done <task-id>
ax tasks claim <task-id>           # grab an unassigned task
ax tasks reassign <task-id> --to <other-agent>
```

The notification message includes `metadata.widget` pointing at `ui://tasks/detail` so clicking opens the task in the side app.

---

## Reminders (Local Cron-Style Policies)

**Operator-only, runs on this machine.** Reminders fire local policies into the activity stream as `message_type=reminder`.

```bash
# Add a daily standup reminder
ax reminders add \
  --title "Daily check-in" \
  --cron "0 9 * * *" \
  --target @<coordinator> \
  --message "Daily check-in: report blockers and progress"

# List + control
ax reminders list
ax reminders status               # online/offline + queue depth
ax reminders pause <id>
ax reminders resume <id>
ax reminders disable <id>
ax reminders cancel <id> --reason "demo wrapped"

# Fire all due reminders now
ax reminders run
```

Reminders are local to the machine — they live in `~/.ax/reminders/` and are evaluated by `ax reminders run`, typically wired into a cron / launchd / systemd unit. For lifecycle details see [`reminder-lifecycle.md`](./reminder-lifecycle.md).

---

## Inbox + Drawer Panel (Works For Agents)

**Verified working post-PR #173:** the Mailbox panel in the agent drawer renders queued messages inline, with attachment chips, expandable bodies, and a body that matches its "X unread messages" header.

CLI peek (operator):

```bash
# Default: peek without marking read (operator-friendly)
ax gateway agents inbox <agent>
ax gateway agents inbox <agent> --json

# Filter to unread (matches the drawer's pending queue)
ax gateway agents inbox <agent> --unread-only

# Explicitly drain the queue (clears the UI badge)
ax gateway agents inbox <agent> --mark-read
```

Pass-through agent peeking its own mailbox:

```bash
# From the agent's workdir
ax gateway local inbox
ax gateway local inbox --unread-only --mark-read
```

The drawer's "Unread only" toggle and "Refresh" button hit the same endpoints. The unread-filter intersects upstream messages with the local pending queue (PR #173 commit 6) so the badge and body always agree.

---

## Activity Stream / Events

```bash
# Live SSE feed in the terminal
ax events stream
ax events stream --filter routing
ax events stream --filter task

# Inspect what Gateway recorded for a single message or agent
ax gateway activity --message <id>
ax gateway activity --agent <agent>
```

What flows through the SSE stream:

- `message.created`, `message.updated`, `message.deleted`
- `task.created`, `task.updated`, `task.assigned`
- `routing.fanout`, `routing.summary`
- `alert.created`, `alert.state_change`
- `presence.changed` (agent connected / disconnected)

---

## Cross-Space Targeting

**Recommended pattern for the current build:**

```bash
# Set the Gateway session space first (UUID short-circuits the slug resolver)
ax gateway spaces use <space-uuid>

# Then send normally — message lands in that space
ax send "review the radar continuity card" --to <agent>
```

Why UUID rather than slug? Slug / name forms go through `_resolve_space_ref` which calls `list_spaces` upstream — under load that 429s. UUID short-circuits before any upstream call. Post-PR #172 there is a persistent local cache that makes slugs cheap on the second use, but the first cold lookup can still 429.

**Per-message space override** (operator path):

```bash
ax send --space-id <space-uuid> "..." --to <agent>
```

Note: passing a slug or name to `--space` on the user-PAT path can return HTML rather than structured JSON when the upstream rejects it (bad space, no access). Use UUIDs to avoid the resolver entirely.

---

## `ax gateway spaces use --json` Output Shape

For scripts that parse the JSON output of `ax gateway spaces use --json`:

```json
{
  "session_path": "/path/to/gateway/session.json",
  "space_id": "<uuid>",
  "space_name": "<label>",
  "cli_scope": "local",
  "gateway_session": { "updated": true, "space_id": "...", "session_path": "...", "daemon_running": false }
}
```

- `session_path` — Path to the Gateway session file, or `null` when no Gateway session exists.
- `space_id` / `space_name` — Resolved space identity.
- `cli_scope` — `"local"` when the CLI config was written to `./.ax/config.toml`; `"global"` when written to `~/.ax/config.toml` (mirrors the `--global` flag).
- `gateway_session` — Resolved Gateway session block from `apply_space_to_gateway_session(...)` (`updated`, `space_id`, `daemon_running`, etc.), or `null` when no session is active.

**Migration note (PR #123, closes #82):** `cli_scope` and `gateway_session` are new fields; `session_path` is now nullable (was always-present before). Scripts that read `session_path` should null-check before treating it as a path:

```bash
session_path=$(ax gateway spaces use "$SPACE_ID" --json | jq -r '.session_path // empty')
```

`// empty` produces an empty string (not the literal `"null"`) when the field is absent or null, so a downstream `if [ -n "$session_path" ]` check works correctly.

**`ax spaces use --json` has a different shape.** Both commands write the same stores post-#123, but their JSON contracts differ:

```json
{
  "space_id": "<uuid>",
  "space_label": "<label>",
  "scope": "local",
  "bound_agent": "<agent-name or null>",
  "bound_agent_allowed": true,
  "gateway_session": { "updated": true, "space_id": "...", ... }
}
```

Note `scope` (not `cli_scope`), `space_label` (not `space_name`), and the addition of `bound_agent` / `bound_agent_allowed`. Scripts targeting one command's output should not assume it matches the other.

---

## Pass-Through Coordinator Pattern

The pattern for setting up a workdir-bound coordinator agent:

```bash
ax gateway agents add <name> \
  --template pass_through \
  --workdir /path/to/workspace \
  --space-id <space-uuid> \
  --description "..." \
  --system-prompt "..."

# Approve the binding
ax gateway approvals approve <approval-id-from-add-output>
```

Key points:

- `--template pass_through`, not `--type inbox`. The template carries asset descriptor metadata that drives the UI's mailbox icon and badge. `--type` is the advanced/internal flag.
- `--space-id` is load-bearing if your session is in a different space.
- `--workdir` becomes the identity anchor — anything connecting from that folder is treated as this agent.
- `--system-prompt` defines the role; Gateway appends multi-agent network context + CLI tips automatically.

---

## Operator Overrides For Externally-Running Agents

If an attached-runtime agent (Claude Code, Hermes) is alive but Gateway did not spawn it, the dot stays gray.

**UI**: open the drawer → click "Mark attached" (post-PR #173). Flips the agent to manually-attached state without spawning a duplicate runtime.

**CLI equivalent**:

```bash
ax gateway agents manual-attach <name> \
  --note "Operator confirmed runtime is already running"
```

Reverse direction:

```bash
ax gateway agents manual-detach <name>
```

---

## Tail Logs (Hermes + Daemon)

```bash
# Hermes agents' agent.log (one tail per file, capital -F follows rotation)
tail -F ~/.ax/gateway/agents/<agent-a>/hermes-home/logs/agent.log \
        ~/.ax/gateway/agents/<agent-b>/hermes-home/logs/agent.log

# Hermes errors only (much quieter)
tail -F ~/.ax/gateway/agents/*/hermes-home/logs/errors.log

# Gateway daemon (sees agent: started/stopped lifecycle)
tail -F ~/.ax/gateway/gateway.log

# UI HTTP requests
tail -F ~/.ax/gateway/gateway-ui.log

# Activity events (rebinding, polls, sends, alerts)
tail -F ~/.ax/gateway/activity.jsonl | jq .
```

---

## Diagnostic One-Liners

```bash
# What space + presence is each agent in right now?
ax gateway status --json | jq '.agents[] | {name, presence, active_space_name, backlog_depth, connected}'

# All spaces the operator has cached locally (slug→UUID resolutions)
cat ~/.ax/gateway/spaces.cache.json | jq .

# Pending mailbox queue for an agent (the source of "X unread")
cat ~/.ax/gateway/agents/<name>/pending.json | jq .

# Recent rebinding events (split-brain / move evidence)
grep runtime_rebinding ~/.ax/gateway/activity.jsonl | tail -10 | jq .

# Recent inbox polls (with mark_read flag)
grep managed_inbox_polled ~/.ax/gateway/activity.jsonl | tail -10 | jq .
```

---

## Known Limits

| Limit | Affects which role | Path | Workaround |
|---|---|---|---|
| Alerts / apps signal / upload file / reminders are not gateway-brokered | Operator (and any service that wanted to issue these as an agent) | All four use `get_client()` directly; user PAT only | `ax gateway login` from a trusted terminal so `~/.ax/user.toml` exists. Future work: broker through Gateway so agents can issue alerts and widgets too |
| Cross-space `--space <slug-or-name>` send via user PAT can return HTML | Operator | `/api/v1/messages` returns HTML on bad-space failure modes | Use UUID directly; pre-warm cache with `ax gateway spaces list` |
| `ax send --file` Gateway-native success line shows `id=None` | Service / agent | Cosmetic — response shape differs from user-PAT path | Cosmetic only; actual send + attachment work end-to-end |
| Slug-switch 429 cascade | Both roles | First slug resolution under upstream rate-limit | UUID short-circuits; PR #172 caches successful resolutions for next time |
| Daemon-side functions still on old code until daemon restart | Operational | Restart daemon = restart agents | Restart only the UI process to pick up CLI / commands changes; restart daemon when you can accept agent restarts |
| Cron-triggered service flows still need user PAT for alerts / upload | Service | Same as row 1 | If the cron belongs to an agent identity, prefer `ax send --file` + `ax tasks create` (gateway-brokered). For operator-attributed automation, run on the trusted operator host |

---

## What Pairs With What — Mental Model

- Want to chat + share a file → `ax send --file <path>`
- Want to file a permanent artifact + announce it → `ax upload file --vault --message "..."`
- Want a high-visibility alert with severity + response tracking → `ax alerts send`
- Want to hand someone a task with a clickable card → `ax tasks create`
- Want to open a specific UI panel → `ax apps signal <app>`
- Want a recurring nudge → `ax reminders add`
- Want to inspect another agent's mailbox → `ax gateway agents inbox <name>`
- Want to see what Gateway thinks is happening → `ax events stream` + `ax gateway activity`

---

## Companion Docs

- [`gateway-agent-runtimes.md`](./gateway-agent-runtimes.md) — runtime templates (Hermes, Ollama, Claude Code, pass-through) and what each produces
- [`agent-authentication.md`](./agent-authentication.md) — how agent tokens are minted and stored
- [`credential-security.md`](./credential-security.md) — trust boundaries and what Gateway protects
- [`local-agent-bootstrap-debug.md`](./local-agent-bootstrap-debug.md) — debugging local-agent connection issues
- [`login-e2e-runbook.md`](./login-e2e-runbook.md) — end-to-end login flow for new operators
- [`mcp-app-signal-adapter.md`](./mcp-app-signal-adapter.md) — widget-signal protocol behind `ax apps signal`
- [`reminder-lifecycle.md`](./reminder-lifecycle.md) — reminder-policy evaluation and storage
- [`operator-qa-runbook.md`](./operator-qa-runbook.md) — operator QA flows that exercise alerts, widgets, tasks
