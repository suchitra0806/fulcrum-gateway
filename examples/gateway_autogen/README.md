# Gateway-managed AutoGen template

A one-shot bridge that routes an inbound aX mention through an AutoGen
(`autogen-agentchat`) `AssistantAgent` and prints the reply on stdout.
Designed for `ax gateway agents add --template autogen`.

The bridge runs in three tiers, picked at runtime by what is installed
and configured. The same Gateway lifecycle events fire on every tier,
so an operator's activity feed looks consistent whether the bridge is
wired to a real LLM or a stub. Mirrors the langgraph template contract.

Targets modern AutoGen, the `autogen-agentchat` >=0.4 line (async-first).
Not the legacy `pyautogen` 0.2 line, which Microsoft is phasing out.

## Execution tiers

1. **Real LLM path.** If `autogen-agentchat` and `autogen-ext` are both
   importable and `GROQ_API_KEY` is set, the bridge builds an
   `AssistantAgent` wired to a Groq-backed `OpenAIChatCompletionClient`
   pointed at `https://api.groq.com/openai/v1` (Groq is OpenAI-compatible
   at that endpoint, so no Groq-specific AutoGen extension is needed).
   The bridge calls `agent.on_messages()` to drive a single turn and
   returns the model's reply text.

2. **Stub agent path.** If `autogen-agentchat` is importable but Groq is
   not configured (no `GROQ_API_KEY` set, or `autogen-ext` is not
   installed), the bridge emits an activity event explaining the
   fallback and returns a stub ack without constructing the agent.
   AutoGen's `AssistantAgent` requires a working model client, so the
   "construct but don't call" pattern that works for CrewAI does not
   apply cleanly here.

3. **String fallback path.** If `autogen-agentchat` itself is not
   installed, the bridge returns a plain string template. The same
   lifecycle events still fire, so a Gateway operator sees the round
   trip even in the most stripped-down environment.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (unset) | Routes the bridge onto the real LLM path. Without it, the bridge falls back to the stub agent. |
| `AX_BRIDGE_LLM_MODEL` | `llama-3.3-70b-versatile` | Overrides the Groq model. Useful for swapping to a smaller / cheaper model or in response to a Groq deprecation. |
| `AX_BRIDGE_SYSTEM_PROMPT` | `Reply concisely.` | Replaces the trailing instruction on the Agent system_message. The leading agent-name framing (`You are @<agent>, ...`) is always emitted automatically. |
| `AX_GATEWAY_AGENT_NAME` | `autogen-bot` | Name the bridge replies as. Hyphens are substituted with underscores for AutoGen's identifier rules. Falls back to `AX_AGENT_NAME` then to the default. |
| `AX_MENTION_CONTENT` | (unset) | Prompt source. Also accepted as positional argv or on stdin. |

## Register with Gateway

```bash
ax gateway agents add --template autogen --name my-autogen-bot
```

The template advertises the bridge at
`examples/gateway_autogen/autogen_bridge.py` and runs through the shared
`exec` runtime adapter (same precedent as the langgraph template).

## Local validation

Run the bridge directly against an inbound prompt to verify wiring:

```bash
cd ~/path/to/ax-gateway
set -a; source ../.env; set +a   # GROQ_API_KEY in there for the real LLM path
AX_GATEWAY_AGENT_NAME=autogen-bot \
AX_MENTION_CONTENT="Reply in one short sentence, what is the speed of light in km/s?" \
.venv/bin/python examples/gateway_autogen/autogen_bridge.py
```

Without `GROQ_API_KEY` (or without `autogen-ext` installed), the bridge
takes the stub agent path and echoes the prompt back, still emitting
the full lifecycle event sequence.

## Lifecycle events

The bridge prints `AX_GATEWAY_EVENT <json>` lines to stdout. Gateway
parses them and routes them to the activity feed.

| Status | When | Detail keys |
|---|---|---|
| `processing` (start) | Bridge begins routing the prompt. | `message` |
| `activity` (build) | `building AutoGen AssistantAgent with Groq model client (model=...)` fires before the model client is wired into the agent. | `activity` |
| `processing` (LLM call) | `Calling Groq (...) via AutoGen` fires before `agent.on_messages(...)`. | `message` |
| `completed` | Final status. `used_llm` reports which path ran. `stub` is kept for back-compat with the pre-LLM-validation schema. | `duration_ms`, `used_llm`, `stub` |
| `error` | Uncaught exception. | `error_message` |

## Naming note (AutoGen identifier rules)

AutoGen's `AssistantAgent` constructor validates the `name` arg against
Python identifier rules (no hyphens, no spaces). Gateway agent names
typically include hyphens (e.g. `autogen-bot`). The bridge substitutes
hyphens with underscores so the name is acceptable to AutoGen, while
keeping the Gateway-side agent name (which appears in the
`system_message`) unchanged so the model knows who it is replying as.

## Follow-ups

The intentional scope for the initial cut. Items here are explicit
follow-ups, not gaps.

- Multi-agent teams (`RoundRobinGroupChat`, `SelectorGroupChat`) for
  cases where a single `AssistantAgent` is not enough.
- Tool-call telemetry mapped to Gateway tool bubbles.
- Token-level streaming activity events via `agent.on_messages_stream`
  (the V1 bridge uses non-streaming `on_messages` for simplicity, same
  cadence as the langgraph bridge's initial cut in PR #38 before
  streaming was added in review).
- Provider abstraction so the LLM tier is not Groq-specific.
