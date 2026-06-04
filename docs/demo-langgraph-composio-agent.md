# LangGraph + Composio Agent Demo Script

**Audience:** Non-technical staff plus PEOs evaluating connector-driven workflows
**Duration:** ~7 minutes
**Platform:** [https://paxai.app](https://paxai.app) (web UI) plus one terminal for the initial registration
**Template:** `langgraph_composio` (Gateway-managed bridge, see `examples/gateway_langgraph_composio/langgraph_composio_bridge.py`)
**Branch:** `main` (template is already on main)

---

## Before the Demo (presenter only, not shown)

```bash
# 1. Pull main and install editable
cd ~/repositories/fd-ax-gateway
git checkout main
git pull
uv pip install -e .

# 2. Install the optional langgraph extra (gives the bridge its StateGraph wrapper)
uv pip install langgraph

# 3. Confirm Gateway is logged in and running
ax gateway login              # only if not already logged in
ax gateway start              # only if the daemon isn't already running
ax gateway status             # should show daemon.running=true, healthy

# 4. Register the Composio connector (one-time per workspace)
ax gateway connectors add demo --provider composio --managed-auth
ax gateway connectors auth write demo COMPOSIO_API_KEY=<your-composio-key>
ax gateway connectors show demo
# Should show provider=composio, auth.managed=true, auth.complete=true

# 5. Register the LangGraph + Composio agent bound to that connector
ax gateway agents add lgc-demo \
  --template langgraph_composio \
  --connector-ref demo \
  --no-start
ax gateway agents start lgc-demo
ax gateway agents show lgc-demo
# Reachability should read "Gateway can launch this runtime on send"
# (langgraph_composio is exec / launch-on-send, not a live listener. The
# bridge runs as a subprocess each time a mention arrives and Gateway sets
# AX_GATEWAY_CONNECTOR_REF=demo in the exec environment.)

# 6. Smoke test from terminal (no RUN: directive, search-only)
AX_GATEWAY_AGENT_NAME=lgc-demo \
AX_GATEWAY_CONNECTOR_REF=demo \
AX_MENTION_CONTENT="list github pull requests for my org" \
.venv/bin/python examples/gateway_langgraph_composio/langgraph_composio_bridge.py
# Should print a reply listing matched Composio tool slugs and exit cleanly

# 7. Send a UI smoke test to make sure the agent shows up in the workspace
ax send "@lgc-demo list github pull requests for my org" --space ax-gateway
# Should reply within a few seconds with a list of matched tool slugs
```

---

## The Demo

### Opening (1 min). The Problem

> "We have agents that reply in chat. What we have not shown yet is an
> agent that actually does work. Search the right tool, call the right
> API, surface the result back into chat with an audit trail.
>
> Most teams glue that together per framework. AutoGen has its tool
> story, LangGraph has another, every CrewAI integration writes a fourth
> one. The agent author has to repeat the connector wiring every time.
>
> The agent I am demoing today is a LangGraph agent that gets its tools
> from a single Gateway-managed Composio connector. No tokens in the
> agent code. No connector code repeated per agent. The trust boundary
> and the tool inventory live in one place."

---

### Step 1. The workspace (1 min)

Open [https://paxai.app](https://paxai.app) and navigate to the **ax-gateway** workspace.

Point out.

- The familiar message surface
- The `@lgc-demo` agent in the participant list, presence shown as IDLE
- The `Composio` connector visible in the connectors panel (if the panel is shown)

> "This agent was registered through Gateway with one command and a
> connector reference. The agent runtime never sees the Composio API
> key. Gateway brokers it. When the agent calls a tool, Gateway records
> who called it, what they asked for, and what came back."

---

### Step 2. First mention (search-only) (1 min)

In the chat input, type.

```
@lgc-demo list github pull requests for my org
```

Press send. Watch the activity feed.

Expected sequence on screen.

1. **Processing** status appears under the agent's avatar
2. Activity line "building LangGraph connector round-trip"
3. Tool call **composio/search_tools** with status `tool_call`
4. Tool result **composio/search_tools** with `mode`, `successful=true`, and a list of matched `tool_slugs`
5. Reply appears in chat (about 3 to 4 seconds) listing the matched tools

The reply looks like:

```
LangGraph+Composio (@lgc-demo) via connector 'demo':
  search mode = ai-classified, matched = 6
  - GITHUB_LIST_PULL_REQUESTS: List pull requests ...
  - GITHUB_GET_PULL_REQUEST: Get a single pull request ...
  - ...
  Tip: append RUN:<TOOL_SLUG> {"key": "value"} to execute a matched tool.
```

> "The agent took the mention text and asked the Composio connector
> what tools matched. The matched tool slugs come back as part of the
> activity feed, so operators see exactly which tools the agent
> considered. Nothing executed yet. This is intent discovery."

---

### Step 3. Tool execution via RUN: directive (1 min, the wow moment)

In the chat input, append a `RUN:` directive to ask the agent to call a specific tool.

```
@lgc-demo RUN:GITHUB_LIST_PULL_REQUESTS {"owner":"FulcrumDefense","repo":"fulcrum-gateway","state":"open"}
```

Press send. Watch the activity feed.

Expected sequence on screen.

1. **Processing** status, then the same search round as before
2. Tool call **composio/GITHUB_LIST_PULL_REQUESTS** with status `tool_call` and the JSON arguments
3. Tool result **composio/GITHUB_LIST_PULL_REQUESTS** with `tool_complete`, `duration_ms`, and a `data` preview
4. Reply renders with `RUN:GITHUB_LIST_PULL_REQUESTS -> ok` plus a truncated data preview

The reply looks like:

```
LangGraph+Composio (@lgc-demo) via connector 'demo':
  search mode = ai-classified, matched = 6
  - GITHUB_LIST_PULL_REQUESTS: ...
  - ...
  RUN:GITHUB_LIST_PULL_REQUESTS -> ok
    data = [{"number": 209, "title": "fix(hermes): enrich ...", ...}, ...]
```

> "The agent just called a real third-party API through Gateway. We did
> not write any GitHub code here. We did not put a GitHub token in the
> agent. Gateway authenticated, Gateway executed, Gateway recorded the
> call. The agent told it what to do, and the reply came back inline.
>
> Whoever adds the next connector, say Slack or JIRA, this agent picks
> it up the same way. Same RUN: directive shape, same activity events,
> same audit trail. The agent author doesn't change."

---

### Step 4. Compare to a sibling template (1 min, optional)

**Skip this step if no sibling template is registered in the workspace.**
This comparison only lands when an AutoGen or LangGraph chat-only agent
is already live alongside `@lgc-demo`. The point is operator-experience
parity across templates with the tool-use delta shown clearly.

If `@autogen-demo` or `@langgraph-bot` is already registered, mention it with the same prompt.

```
@autogen-demo list github pull requests for my org
```

Compare the activity feed.

- Sibling template replies with a natural-language list (model-generated, no real tool call)
- `@lgc-demo` replies with the matched tool slugs and an actual API call when prompted with RUN:
- Both stream the same lifecycle event shape; only the connector-tool events are template-specific

> "Same operator surface, same activity-feed conventions. The delta is
> that this template went through Gateway to call a real API. The
> AutoGen agent is great at chat. This one is great at chat plus doing
> work. Operators pick the template that matches the workflow."

---

### Step 5. Closing (1 min)

Show `ax gateway connectors activity` in a terminal as the closer.

```
ax gateway connectors activity --connector-ref demo --limit 5
```

Point out.

- Each tool call is timestamped, attributed to `lgc-demo`
- Each row carries the call status (`ok` or `error`) and the elapsed time
- The Composio API key never appears anywhere in the activity log

> "Gateway is the trust boundary and the audit surface. The agent
> author chose LangGraph because the workflow benefits from a graph.
> Tomorrow they could switch to AutoGen or write a custom bridge. The
> connector wiring, the credentials, the audit trail, the policy
> controls all live in Gateway. When a new connector lands, every
> template can use it. When an agent is rotated out, the trail stays."

---

## What this demo is NOT showing yet

Intentional scope cuts for V1 of the langgraph_composio template.

- **Multi-turn agentic tool use.** The current bridge runs one search
  plus zero-or-one execute per mention. Agentic loops where the agent
  decides to call a tool, reads the result, then decides on a follow-up
  tool are a follow-up. The single-turn pattern keeps the demo crisp
  and the audit trail one-step-per-mention readable.
- **Multi-tool execution in a single mention.** Only one `RUN:` directive
  is honored per mention today. Chaining multiple tool calls is a
  follow-up that would extend the directive grammar.
- **Tool-output validation.** The bridge surfaces whatever Composio
  returns. Validating the response shape and translating connector
  errors into operator-friendly messages is a follow-up alongside the
  connector-error classification work in PR #154.
- **LangGraph-side reasoning.** The optional StateGraph wrapper today
  is one-node (search + execute round-trip). A multi-node graph that
  reasons about tool selection, conditional branching, and retries is
  the next-step PR.

These cuts let V1 ship fast and give operators a working tool-calling
baseline today. Each one is a clear next-step PR.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Agent shows `error` state in `agents show` | Composio connector not registered or auth incomplete | `ax gateway connectors show demo` should show `auth.complete=true`; if not, re-run `ax gateway connectors auth write demo COMPOSIO_API_KEY=...` |
| Bridge replies with `search mode = stub` and zero tools matched | Composio API key not set in the connector, or the connector ref env var was not seen by the bridge | Check `ax gateway connectors show demo` then re-register the agent so the new env routes through |
| Bridge replies with `RUN:<SLUG> -> failed` and `error = ...` | The tool slug is real but the arguments JSON is missing required keys or has the wrong types | Read the Composio tool spec for that slug and adjust the JSON in the RUN: directive |
| Bridge raises `RUN:<SLUG> arguments must be valid JSON` | The JSON after the slug is malformed (unbalanced braces, missing quotes) | Pass a valid JSON object; quote keys and string values |
| `exit_reason=crashed` with import errors for `ax_cli.connectors` | Editable install missing or stale | `uv pip install -e .` in the gateway repo |
| Agent does not show up in workspace participant list | Agent registered but Gateway daemon not running | `ax gateway status` to confirm `daemon.running=true`, then `ax gateway start` if needed |
| Activity feed shows tool_start but no tool_result | Composio API timeout or network stall | `ax gateway connectors activity --connector-ref demo` to see what landed server-side; may need to retry |

---

## Related reading

- `examples/gateway_langgraph_composio/langgraph_composio_bridge.py`. Bridge source
- `examples/gateway_langgraph_composio/README.md`. Bridge reference doc with the RUN: directive grammar and lifecycle event shape
- `docs/demo-outbound-connectors.md`. Composio connectors deep dive, more technical audience
- `docs/demo-autogen-agent.md`. AutoGen sibling demo for the chat-only side of the story
- `docs/gateway-demo-script.md`. Broader Gateway shape demo, audience is more technical
