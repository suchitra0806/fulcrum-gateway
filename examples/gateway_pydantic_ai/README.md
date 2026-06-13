# Pydantic AI bridge for Gateway-managed agents

Gateway-managed bridge for a [Pydantic AI](https://ai.pydantic.dev/)
agent. Routes inbound mentions through a Pydantic AI `Agent` and prints
the reply on stdout.

## Execution tiers

Three tiers, picked at runtime by what is installed and configured.
Mirrors the langgraph and autogen bridges so an operator's activity
feed looks consistent regardless of which template a Gateway agent
runs.

| Tier | When | Behavior |
|---|---|---|
| Real LLM | `pydantic-ai` installed AND `GROQ_API_KEY` set | Agent backed by Groq (OpenAI-compatible endpoint), streams chunks with throttled activity events |
| Stub agent | `pydantic-ai` installed, no Groq config | Returns stub ack, lifecycle events still fire |
| String fallback | `pydantic-ai` not installed | Plain string template, lifecycle events still fire |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `AX_GATEWAY_AGENT_NAME` | `pydantic-ai-bot` | Agent identity surfaced in the system prompt |
| `AX_MENTION_CONTENT` | (read from stdin if absent) | The prompt to route through the agent |
| `AX_BRIDGE_LLM_MODEL` | `llama-3.3-70b-versatile` | Groq model name for the real LLM path |
| `AX_BRIDGE_SYSTEM_PROMPT` | `Reply concisely.` | Appended to the agent's system prompt |
| `GROQ_API_KEY` | (unset) | Required for the real LLM path. If unset the bridge falls back to the stub tier |

## Register with Gateway

```bash
ax gateway agents add pydantic-ai-bot \
    --template pydantic_ai \
    --workdir agents/pydantic-ai-bot
```

The Gateway records the bridge path and the env vars it surfaces.

## Local validation without Gateway

```bash
GROQ_API_KEY=$GROQ_API_KEY \
AX_BRIDGE_LLM_MODEL=llama-3.3-70b-versatile \
AX_GATEWAY_AGENT_NAME=pydantic-ai-bot \
AX_MENTION_CONTENT="Reply in one sentence, what is the speed of light in km/s?" \
python examples/gateway_pydantic_ai/pydantic_ai_bridge.py
```

Expected output is the bridge printing a real Groq reply on stdout
plus a sequence of `AX_GATEWAY_EVENT` JSON lines on stdout for the
Gateway activity-log consumer.

## Lifecycle events

| Event | When |
|---|---|
| `kind: status, status: processing` | At entry to the bridge, when the model client is built, and when the first chunk arrives |
| `kind: activity` | Throttled ~1s with a rolling preview of the response text |
| `kind: status, status: completed` | At successful end, includes `duration_ms`, `stub`, and `used_llm` fields in `detail` |
| `kind: status, status: error` | On uncaught exception, includes `error_message` |

## Why Pydantic AI (versus the langgraph or autogen bridges)

Pydantic AI's distinguishing feature is type-safe agent outputs via
Pydantic models. This V1 bridge ships plain-text outputs to match the
existing template contract. The structured-output path is the natural
follow-up since it exposes the framework's reason for existing.

LangGraph fits multi-node graph topologies and tool routing. AutoGen
fits multi-agent teams via group chats. Pydantic AI fits single-agent
typed-output workflows where the validation guarantees matter (data
extraction, classification, structured replies).

## Follow-ups

- Structured-output bridge variant: expose Pydantic AI's defining
  feature by routing replies through a Pydantic model
- Tool support via `@agent.tool` decorator alongside the existing
  Gateway tool registry
- Multi-step run via `agent.iter()` for chain-of-thought style
  reasoning
- Dedicated `GroqModel` variant for users who prefer the
  vendor-specific class over the OpenAI-compatible path
