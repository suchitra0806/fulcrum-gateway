# Scenario: Investigate a 429 Rate-Limiting Storm

## Goal

Diagnose and resolve a burst of HTTP 429 (Too Many Requests) errors from the
aX platform API.

## Prerequisites

- Gateway running with one or more active agents
- Access to Gateway log files

## Background

Gateway manages the rate-limit budget proactively
([ADR-015](../adr/ADR-015-proactive-rate-limit-management.md)): all clients
in a process share one view of the window and sleep *before* sending when it
is nearly drained, so a true 429 storm should be rare. When budget pressure
does occur, two logs tell the story:

- `~/.ax/gateway/api-requests.log` — one JSON line per outbound API request
  (role, method, path, status, rate-limit remaining). On by default; rotates
  at 10MB. This is your primary forensic source.
- `~/.ax/gateway/activity.jsonl` — operator-level events, including
  `rate_limit_wait` entries when a CLI/UI request paused for the window.

## Steps

### 1. Check how much budget pressure you're under

```bash
# Recent requests with the window nearly drained
tail -200 ~/.ax/gateway/api-requests.log | \
  python3 -c "import json,sys; [print(r['ts'], r['role'], r['method'], r['path'], r['remaining']) for r in map(json.loads, sys.stdin) if (r.get('remaining') or 99) < 10]"

# Actual 429s and proactive waits
grep '"status": 429' ~/.ax/gateway/api-requests.log | tail -10
grep -i "rate_limit_wait\|429\|backoff" ~/.ax/gateway/activity.jsonl | tail -20
```

Frequent `rate_limit_wait` events mean the proactive throttle is absorbing
the pressure. Raw 429s in `api-requests.log` mean multiple processes are
overrunning the shared budget (each process coordinates internally, but the
daemon, CLI, and UI server each hold their own view).

### 2. Identify which agents are consuming the budget

```bash
# Requests per agent/role today
grep "$(date +%Y-%m-%d)" ~/.ax/gateway/api-requests.log | \
  python3 -c "import json,sys,collections; c=collections.Counter((r.get('agent_name') or r['role']) for r in map(json.loads, sys.stdin)); [print(f'{n:6d}  {k}') for k,n in c.most_common(10)]"
```

Common culprits:

- Agents polling `list_messages` too frequently
- Multiple agents starting simultaneously, each calling `whoami` + `list_spaces`
- Hermes sentinel runtimes retrying on transient errors
- A UI dashboard left open with a short `--refresh` interval

### 3. Check overall activity volume

```bash
grep "$(date +%Y-%m-%d)" ~/.ax/gateway/activity.jsonl | wc -l
```

Compare total activity today vs. a normal day. A spike often correlates with
an agent restart cascade.

### 4. Check throttle behavior

Clients wait proactively when the window is drained, and CLI commands print
`Rate limit reached — waiting Ns` while doing so. Waits longer than the
2-minute cap fail fast with "try again at HH:MM:SS" instead of hanging. If
you see repeated raw 429s with *no* `rate_limit_wait` events and no wait
messages, the proactive throttle may not be engaging — file a bug (see also
issue #27 for sentinel-side rate-limit awareness).

### 5. Reduce load

If the storm is ongoing, reduce the number of active agents:

```bash
# Stop non-essential agents
ax gateway agents stop echo-bot
ax gateway agents stop monitor-agent

# Keep only critical agents running
ax gateway agents list
```

### 6. Stagger restarts

When restarting agents after a 429 storm, stagger them to avoid a
reconnection stampede:

```bash
ax gateway agents start dev-sentinel
sleep 10
ax gateway agents start review-agent
sleep 10
ax gateway agents start echo-bot
```

### 7. Verify recovery

```bash
ax gateway status
ax gateway agents show dev-sentinel
```

Check that agents are healthy and no new 429 or wait events appear:

```bash
tail -f ~/.ax/gateway/api-requests.log | grep '"status": 429' &
tail -f ~/.ax/gateway/activity.jsonl | grep -i "429\|backoff\|rate_limit_wait"
```

Wait 60 seconds. If no new lines appear, the storm has passed.

## Verify

- No new 429s in `api-requests.log` and no new `rate_limit_wait` events in
  `activity.jsonl` for at least 60 seconds
- All critical agents show `effective_state: running`
- Messages are being delivered normally

## What can go wrong

| Problem | Cause | Fix |
| --- | --- | --- |
| 429s continue after reducing agents | Platform-wide rate limit, not per-agent | Wait for the rate limit window to expire (usually 1-5 minutes) |
| Agent enters error state after 429 storm | Too many consecutive failures triggered a health check failure | Restart the agent after the storm passes |
| Reconcile loop itself triggers 429s | Loop calls upstream API for each agent every ~1 second | Reduce registered agent count, or wait for batch API support |
| CLI command fails with "try again at HH:MM:SS" | Server's advertised cooldown exceeds the 2-minute wait cap | Wait until the printed time; retrying earlier cannot succeed |
| `api-requests.log` is empty | Logging disabled via `AX_LOG_API_REQUESTS=0` | Unset the variable (logging is on by default) |

## Learning goal

Understanding Gateway's relationship with the upstream API rate limits. The
reconcile loop, agent startups, and space resolution all make API calls. With
many agents, these calls can exceed platform rate limits. Operators need to
understand that agent count directly affects API call volume and plan agent
registrations accordingly.
