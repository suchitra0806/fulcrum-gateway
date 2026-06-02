#!/usr/bin/env python3
"""Gateway-managed LangGraph bridge with Composio connector toolbelt.

Designed for ``ax gateway agents add ... --template langgraph_composio
--connector-ref <name>``.

Per mention the bridge:
  1. Searches Composio tools via Gateway ``search_connector_tools`` (intent/catalog).
  2. Optionally executes one tool when the prompt contains ``RUN:<SLUG> {json}``.
  3. Prints a short operator-facing reply on stdout.

Gateway records connector activity on execute; the bridge emits AX_GATEWAY_EVENT
tool_start/tool_result lines for search so the monitor shows the round trip.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from typing import Any, Callable

EVENT_PREFIX = "AX_GATEWAY_EVENT "
_RUN_RE = re.compile(
    r"(?i)\bRUN:\s*([A-Z][A-Z0-9_]*)\s*(\{.*\})?\s*$",
    re.MULTILINE,
)


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
        or "langgraph-composio-bot"
    )


def _connector_ref() -> str:
    return os.environ.get("AX_GATEWAY_CONNECTOR_REF", "").strip()


def _parse_run_directive(prompt: str) -> tuple[str | None, dict[str, Any]]:
    match = _RUN_RE.search(prompt)
    if not match:
        return None, {}
    slug = match.group(1).upper()
    raw_args = (match.group(2) or "").strip()
    if not raw_args:
        return slug, {}
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        raise ValueError(f"RUN:{slug} arguments must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"RUN:{slug} arguments must be a JSON object")
    return slug, parsed


def _default_search(connector_ref: str, use_case: str) -> Any:
    from ax_cli.connectors import search_connector_tools

    return search_connector_tools(connector_ref, use_case)


def _runtime_agent_identity() -> tuple[str | None, str | None]:
    name = os.environ.get("AX_GATEWAY_AGENT_NAME", "").strip() or os.environ.get("AX_AGENT_NAME", "").strip()
    agent_id = os.environ.get("AX_GATEWAY_AGENT_ID", "").strip() or os.environ.get("AX_AGENT_ID", "").strip()
    return (name or None, agent_id or None)


def _default_execute(connector_ref: str, tool_slug: str, arguments: dict[str, Any]) -> Any:
    from ax_cli.connectors import execute_connector_tool

    agent_name, agent_id = _runtime_agent_identity()
    return execute_connector_tool(
        connector_ref,
        tool_slug,
        arguments,
        agent_name=agent_name,
        agent_id=agent_id,
    )


def run_connector_round(
    prompt: str,
    connector_ref: str,
    *,
    search_tools: Callable[[str, str], Any] | None = None,
    execute_tool: Callable[[str, str, dict[str, Any]], Any] | None = None,
) -> str:
    """Search (and optionally execute) via Gateway connectors; return reply text."""
    search_fn = search_tools or _default_search
    execute_fn = execute_tool or _default_execute
    run_slug, run_args = _parse_run_directive(prompt)

    search_call_id = f"composio-search-{uuid.uuid4()}"
    emit_event(
        {
            "kind": "tool_start",
            "tool_name": "composio/search_tools",
            "tool_action": "search",
            "tool_call_id": search_call_id,
            "status": "tool_call",
            "arguments": {"use_case": prompt[:500], "connector_ref": connector_ref},
            "message": "Searching Composio tools for mention",
        }
    )
    started = time.monotonic()
    search_result = search_fn(connector_ref, prompt)
    slugs = [tool.slug for tool in search_result.tools]
    emit_event(
        {
            "kind": "tool_result",
            "tool_name": "composio/search_tools",
            "tool_action": "search",
            "tool_call_id": search_call_id,
            "status": "tool_complete",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "initial_data": {
                "mode": search_result.mode,
                "successful": search_result.successful,
                "tool_slugs": slugs,
                "session_id": search_result.session_id,
            },
            "message": f"Composio search returned {len(slugs)} tool(s) after policy",
        }
    )

    lines = [
        f"LangGraph+Composio (@{_agent_name()}) via connector {connector_ref!r}:",
        f"  search mode = {search_result.mode}, matched = {len(search_result.tools)}",
    ]
    if search_result.error:
        lines.append(f"  search note = {search_result.error}")
    for tool in search_result.tools[:8]:
        desc = (tool.description or "").strip()
        if len(desc) > 72:
            desc = desc[:69] + "..."
        lines.append(f"  - {tool.slug}: {tool.name}" + (f" — {desc}" if desc else ""))
    if len(search_result.tools) > 8:
        lines.append(f"  … and {len(search_result.tools) - 8} more")

    if run_slug:
        exec_call_id = f"composio-exec-{uuid.uuid4()}"
        emit_event(
            {
                "kind": "tool_start",
                "tool_name": f"composio/{run_slug}",
                "tool_action": "execute",
                "tool_call_id": exec_call_id,
                "status": "tool_call",
                "arguments": run_args,
                "message": f"Executing {run_slug} via Gateway connector",
            }
        )
        exec_started = time.monotonic()
        try:
            call_result = execute_fn(connector_ref, run_slug, run_args)
        except Exception as exc:
            emit_event(
                {
                    "kind": "tool_result",
                    "tool_name": f"composio/{run_slug}",
                    "tool_action": "execute",
                    "tool_call_id": exec_call_id,
                    "status": "tool_error",
                    "duration_ms": int((time.monotonic() - exec_started) * 1000),
                    "message": str(exc),
                }
            )
            raise
        emit_event(
            {
                "kind": "tool_result",
                "tool_name": f"composio/{run_slug}",
                "tool_action": "execute",
                "tool_call_id": exec_call_id,
                "status": "tool_complete",
                "duration_ms": int((time.monotonic() - exec_started) * 1000),
                "initial_data": call_result.to_dict(),
                "message": "Composio tool execution finished",
            }
        )
        status = "ok" if call_result.successful else "failed"
        lines.append(f"  RUN:{run_slug} → {status}")
        if call_result.error:
            lines.append(f"    error = {call_result.error}")
        elif call_result.data is not None:
            preview = json.dumps(call_result.data, default=str)
            if len(preview) > 240:
                preview = preview[:237] + "..."
            lines.append(f"    data = {preview}")
    else:
        lines.append('  Tip: append RUN:<TOOL_SLUG> {"key": "value"} to execute a matched tool.')

    return "\n".join(lines)


def _run_langgraph(prompt: str, connector_ref: str) -> str:
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        emit_event(
            {
                "kind": "activity",
                "activity": "langgraph not installed; running connector round sequentially",
            }
        )
        return run_connector_round(prompt, connector_ref)

    emit_event({"kind": "activity", "activity": "building LangGraph connector round-trip"})

    def _search_node(state: dict[str, Any]) -> dict[str, Any]:
        reply = run_connector_round(str(state.get("prompt") or ""), connector_ref)
        return {"reply": reply}

    graph = StateGraph(dict)
    graph.add_node("connector_round", _search_node)
    graph.add_edge(START, "connector_round")
    graph.add_edge("connector_round", END)
    app = graph.compile()
    result = app.invoke({"prompt": prompt})
    return str(result.get("reply") or "")


def main() -> int:
    prompt = _read_prompt()
    if not prompt:
        print("(no mention content received)", file=sys.stderr)
        return 1

    connector_ref = _connector_ref()
    if not connector_ref:
        emit_event(
            {
                "kind": "status",
                "status": "error",
                "error_message": (
                    "AX_GATEWAY_CONNECTOR_REF not set. Set via `ax gateway agents add --connector-ref <name>`."
                ),
            }
        )
        print(
            "LangGraph+Composio bridge requires AX_GATEWAY_CONNECTOR_REF "
            "(set via `ax gateway agents add --connector-ref <name>`).",
            file=sys.stderr,
        )
        return 1

    started = time.monotonic()
    emit_event(
        {
            "kind": "status",
            "status": "processing",
            "message": "Routing mention through LangGraph Composio bridge",
        }
    )

    try:
        reply = _run_langgraph(prompt, connector_ref)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"LangGraph Composio bridge failed: {exc}", file=sys.stderr)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"LangGraph Composio bridge completed in {duration_ms}ms",
            "detail": {"duration_ms": duration_ms, "connector_ref": connector_ref},
        }
    )
    print(reply, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
