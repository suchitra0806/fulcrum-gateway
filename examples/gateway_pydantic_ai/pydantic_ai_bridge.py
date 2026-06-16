#!/usr/bin/env python3
"""Gateway-managed bridge for a Pydantic AI agent.

This bridge is designed for `ax gateway agents add ... --template pydantic_ai`.
It runs once per inbound mention: read the prompt, route it through a
Pydantic AI `Agent`, and print the reply on stdout.

Three execution tiers, picked at runtime by what is installed and
configured. Mirrors the langgraph plus autogen bridge contracts so an
operator's activity feed looks consistent regardless of which template
a Gateway agent runs.

  1. Real LLM path. If `pydantic-ai` is importable AND GROQ_API_KEY is
     set, the bridge builds a Pydantic AI Agent wired to an
     OpenAIChatModel pointed at https://api.groq.com/openai/v1 (Groq is
     OpenAI-compatible at that endpoint, so the OpenAI provider path
     covers it). The bridge then drives a single turn via
     `agent.run_stream()` so token-level chunks surface as throttled
     ~1s activity events with a rolling preview, keeping the operator's
     activity feed live during the call. Env overrides:
     AX_BRIDGE_LLM_MODEL (default llama-3.3-70b-versatile),
     AX_BRIDGE_SYSTEM_PROMPT (default "Reply concisely.", overrides
     the default trailing instruction in the agent's system prompt
     when set).

  2. Stub agent path. If `pydantic-ai` is importable but Groq is not
     configured, the bridge emits an activity event explaining the
     fallback and returns a stub ack. Pydantic AI's Agent constructor
     requires a model, so "construct but don't call" is not a clean
     pattern the way it is for some frameworks. The same lifecycle
     events still fire.

  3. String fallback path. If `pydantic-ai` itself is not installed,
     the bridge returns a plain string template. Same lifecycle events
     still fire.

The three-tier shape lets the bridge round-trip a reply through the
Gateway end to end in CI / dev without LLM creds, and switch to real
LLM execution in production environments where GROQ_API_KEY is
provisioned. Structured-output validation via Pydantic models and
tool-call decorators (`@agent.tool`) are deliberate follow-ups.

Pydantic AI targets typed, validated agent outputs by default. This V1
bridge ships plain-text agent output to match the existing template
contract; structured-output follow-up would expose Pydantic AI's
distinguishing feature.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, NamedTuple

EVENT_PREFIX = "AX_GATEWAY_EVENT "


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _read_prompt() -> str:
    if len(sys.argv) > 1 and sys.argv[-1] != "-":
        return sys.argv[-1]
    env_prompt = os.environ.get("AX_MENTION_CONTENT", "").strip()
    if env_prompt:
        return env_prompt
    return sys.stdin.read().strip()


def _agent_name() -> str:
    return (
        os.environ.get("AX_GATEWAY_AGENT_NAME", "").strip()
        or os.environ.get("AX_AGENT_NAME", "").strip()
        or "pydantic-ai-bot"
    )


DEFAULT_LLM_MODEL = "llama-3.3-70b-versatile"
GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_SYSTEM_PROMPT_TAIL = "Reply concisely."
ACTIVITY_HEARTBEAT_SECONDS = 1.0
PREVIEW_MAX_CHARS = 180


class RunResult(NamedTuple):
    reply: str
    used_llm: bool


def _system_prompt_tail() -> str:
    return os.environ.get("AX_BRIDGE_SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT_TAIL


def _build_model(model_name: str):
    """Build a Pydantic AI OpenAIChatModel pointed at Groq's OpenAI-compatible endpoint.

    Raises ImportError if `pydantic-ai` is not installed (caller treats
    as a signal to fall back to the stub path). The OpenAI provider
    path is used (not a dedicated GroqModel) because Groq is
    OpenAI-compatible at the openai/v1 endpoint, the OpenAI provider
    surface is stable across pydantic-ai versions, and using the
    provider+base_url path means future vendors that are also
    OpenAI-compatible can be supported by the same operator-facing
    config without runtime changes.
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        api_key=os.environ["GROQ_API_KEY"],
        base_url=GROQ_OPENAI_BASE_URL,
    )
    return OpenAIChatModel(model_name, provider=provider)


def _close_provider_client_best_effort(model: Any) -> None:
    """Close the httpx AsyncClient under the OpenAIProvider attached to
    the model, if discoverable. Best-effort, swallows all exceptions so
    a missing-attribute path on an older pydantic-ai release does not
    leak through to the caller. The bridge process exits immediately
    after the reply lands, so any unclosed connection would terminate
    with the process anyway; this just keeps the stderr clean.
    """
    try:
        provider = getattr(model, "provider", None)
        client = getattr(provider, "client", None) if provider is not None else None
        aclose = getattr(client, "aclose", None) if client is not None else None
        if callable(aclose):
            asyncio.run(aclose())
    except Exception:
        pass


async def _run_agent_stream(agent, prompt: str, model: str) -> str:
    """Send a single user message to the Pydantic AI agent and consume
    the streaming events, surfacing throttled activity events to the
    Gateway activity feed during the call.

    Uses `agent.run_stream()` (async context manager) so operators see
    the bridge thinking instead of staring at silence during the
    multi-second LLM call. Matches the LangGraph plus AutoGen bridges'
    pattern. Pydantic AI's `stream_text(delta=True)` yields incremental
    text deltas across the stream.

    Returns the final text after the stream completes. Pydantic AI's
    `result.get_output()` is the canonical accessor for the final
    aggregated text once the stream is exhausted.
    """
    chunks: list[str] = []
    first_chunk_seen = False
    last_activity_at = 0.0

    async with agent.run_stream(prompt) as result:
        async for delta in result.stream_text(delta=True):
            if not isinstance(delta, str) or not delta:
                continue

            chunks.append(delta)
            now = time.monotonic()
            if not first_chunk_seen:
                first_chunk_seen = True
                emit_event(
                    {
                        "kind": "status",
                        "status": "processing",
                        "message": f"Groq is responding ({model})",
                    }
                )
            if now - last_activity_at >= ACTIVITY_HEARTBEAT_SECONDS:
                preview = "".join(chunks).strip().replace("\n", " ")
                if len(preview) > PREVIEW_MAX_CHARS:
                    preview = "..." + preview[-(PREVIEW_MAX_CHARS - 3) :]
                emit_event(
                    {
                        "kind": "activity",
                        "activity": (f"{model}: {preview}" if preview else f"Streaming response from {model}..."),
                    }
                )
                last_activity_at = now

        # Prefer the canonical final-output accessor if available, fall
        # back to accumulated chunks for older pydantic-ai versions that
        # may not have get_output(). Narrow the swallow to AttributeError
        # (the version-skew case) so unexpected runtime errors surface as
        # an activity event instead of being silently absorbed.
        final_text = ""
        try:
            final_text = str(await result.get_output() or "")
        except AttributeError:
            pass
        except Exception as exc:
            emit_event(
                {
                    "kind": "activity",
                    "activity": f"final output accessor failed ({exc!r}); using accumulated stream chunks",
                }
            )

    return final_text.strip() or "".join(chunks).strip()


def _run_pydantic_ai(prompt: str) -> RunResult:
    """Run a Pydantic AI Agent (real LLM or stub) if pydantic-ai is
    available, else a plain string template.

    Returns a RunResult naming the reply and whether the real LLM path
    was taken so main() can report it accurately in the completion
    event without a side-channel. See the module docstring for the
    three-tier behavior.
    """
    try:
        from pydantic_ai import Agent
    except ImportError:
        emit_event(
            {
                "kind": "activity",
                "activity": "pydantic-ai not installed; using stub reply (install pydantic-ai for real agent execution)",
            }
        )
        return RunResult(
            reply=f"Pydantic AI stub ack from @{_agent_name()}: {prompt}",
            used_llm=False,
        )

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("AX_BRIDGE_LLM_MODEL", DEFAULT_LLM_MODEL).strip() or DEFAULT_LLM_MODEL

    if not groq_key:
        emit_event(
            {
                "kind": "activity",
                "activity": "Pydantic AI installed but no GROQ_API_KEY set; using stub reply (set GROQ_API_KEY for real agent execution)",
            }
        )
        return RunResult(
            reply=f"Pydantic AI ack from @{_agent_name()}: {prompt}",
            used_llm=False,
        )

    agent_name = _agent_name()
    system_message = f"You are @{agent_name}, an assistant routed through the aX Gateway. {_system_prompt_tail()}"

    try:
        pydantic_model = _build_model(model)
    except ImportError as exc:
        # OpenAIProvider / OpenAIChatModel imports failing means the
        # pydantic-ai install is missing the OpenAI extras. Surface a
        # clean signal rather than crashing.
        emit_event(
            {
                "kind": "activity",
                "activity": f"GROQ_API_KEY set but pydantic-ai OpenAI components not importable ({exc}); falling back to stub",
            }
        )
        return RunResult(
            reply=f"Pydantic AI ack from @{agent_name}: {prompt}",
            used_llm=False,
        )

    emit_event(
        {
            "kind": "activity",
            "activity": f"building Pydantic AI Agent with Groq model (model={model})",
        }
    )
    emit_event(
        {
            "kind": "status",
            "status": "processing",
            "message": f"Calling Groq ({model}) via Pydantic AI",
        }
    )
    agent = Agent(
        pydantic_model,
        system_prompt=system_message,
    )
    try:
        reply = asyncio.run(_run_agent_stream(agent, prompt, model))
    finally:
        # Pydantic AI's OpenAIProvider wraps an httpx AsyncClient that
        # should be closed to avoid "unclosed connection" warnings on
        # subprocess exit. Best-effort, the bridge is one-shot so leaks
        # would not accumulate across runs. Mirrors the autogen bridge's
        # model_client cleanup pattern.
        _close_provider_client_best_effort(pydantic_model)
    return RunResult(reply=reply.strip(), used_llm=True)


def main() -> int:
    prompt = _read_prompt()
    if not prompt:
        print("(no mention content received)", file=sys.stderr)
        return 1

    started = time.monotonic()
    emit_event(
        {
            "kind": "status",
            "status": "processing",
            "message": "Routing prompt through Pydantic AI bridge",
        }
    )

    try:
        result = _run_pydantic_ai(prompt)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"Pydantic AI bridge failed: {exc}", file=sys.stderr)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"Pydantic AI bridge completed in {duration_ms}ms",
            "detail": {
                "duration_ms": duration_ms,
                # `stub` is kept for back-compat with the pre-LLM-validation
                # bridge schema; downstream consumers may still key off it.
                # New consumers should prefer `used_llm`.
                "stub": not result.used_llm,
                "used_llm": result.used_llm,
            },
        }
    )
    print(result.reply or f"Pydantic AI bridge for @{_agent_name()} finished without text.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
