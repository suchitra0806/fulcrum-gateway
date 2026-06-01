#!/usr/bin/env python3
"""Gateway-managed bridge for a LangGraph agent.

This bridge is designed for `ax gateway agents add ... --template langgraph`.
It runs once per inbound mention: read the prompt, route it through a
LangGraph StateGraph, and print the reply on stdout.

Three execution tiers, picked at runtime by what is installed and
configured.

  1. Real LLM path. If `langgraph` AND `groq` are importable AND
     GROQ_API_KEY is set, the bridge builds a StateGraph powered by
     a Groq chat completion. When tool dispatch is enabled (default),
     the graph is a two-node agentic loop (llm_call <-> tool_node)
     with a conditional edge for the cycle. The agent gets a small
     set of default tools (echo, read_file, list_dir) wrapped by a
     security middleware ported from ax_cli/runtimes/hermes/tools/.
     When tool dispatch is disabled (AX_BRIDGE_TOOLS_DISABLED=1) or
     the imports needed for tool dispatch are missing, the bridge
     falls back to the single-node streaming behavior Avrohom
     originally shipped in PR #38. Env overrides:
     AX_BRIDGE_LLM_MODEL (default llama-3.3-70b-versatile),
     AX_BRIDGE_SYSTEM_PROMPT (default "Reply concisely."),
     AX_BRIDGE_MAX_ITERATIONS (default 30, hard floor 1, hard
     ceiling 200), AX_BRIDGE_TOOLS_DISABLED (operator escape hatch),
     AX_BRIDGE_STRICT_SECURITY (default 0; flips the security
     wrapper from default-allow to default-deny for unrecognized
     tool names).

  2. Stub graph path. If `langgraph` is importable but Groq is not
     configured, the bridge builds the same one-node StateGraph but
     wires it to a synthetic ack node that does not call any LLM.
     This proves the langgraph wiring without requiring credentials.

  3. String fallback path. If `langgraph` itself is not installed,
     the bridge returns a plain string template. Same lifecycle
     events still fire.

The three-tier shape lets the bridge round-trip a reply through the
Gateway end to end in CI / dev without LLM creds, and switch to real
LLM execution in production environments where GROQ_API_KEY is
provisioned. The multi-node + ToolNode extension and the security
middleware land in Sprint 04 task #976; the MCP-to-LangChain tool
adapter is a separate task (Sprint 05 / Sprint 06 once Sprint 05
resumes).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

EVENT_PREFIX = "AX_GATEWAY_EVENT "


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


# ── .env loader (Sprint 04 #976) ─────────────────────────────────────────
#
# Gateway daemon spawns this bridge as a subprocess with a custom env dict
# (see ax_cli/gateway.py::sanitize_exec_env). If GROQ_API_KEY isn't in the
# daemon's own environment at start time, the child won't see it either —
# and the multi-node tier silently degrades to the single-node tier (or
# the stub if no Groq at all). Same env-propagation landmine that bit
# Hermes sentinels in 2026-05-16 (see PM comm #226). The Hermes fix
# patched sentinel.py to call hermes_cli.env_loader.load_hermes_dotenv()
# at startup; this bridge does the equivalent without taking a hermes_cli
# dependency.
#
# Resolution order for the .env file:
#   1. AX_BRIDGE_ENV_FILE env var (explicit override)
#   2. .env in the current working directory
#   3. .env in the same dir as the script (examples/gateway_langgraph/)
#   4. .env one level up from the script (typically the repo root)
#
# Existing env vars take precedence (we don't clobber values the operator
# already set). Quoted values are unwrapped. Lines starting with # are
# comments. Same shape as the Hermes ~/.hermes/.env format.


def _load_dotenv_into_environ() -> None:
    """Load the first .env file we find into os.environ.

    Existing env vars take precedence — we don't overwrite anything the
    operator already set. Silent on file-not-found (this is best-effort).
    """
    candidates: list[Path] = []
    explicit = os.environ.get("AX_BRIDGE_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / ".env")
    candidates.append(script_dir.parent.parent / ".env")  # repo root from examples/gateway_langgraph/

    for path in candidates:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding single or double quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if not key or key in os.environ:
                continue
            os.environ[key] = value
        emit_event(
            {
                "kind": "activity",
                "activity": f"loaded .env from {path}",
            }
        )
        return  # only load the first match


_load_dotenv_into_environ()


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
        or "langgraph-bot"
    )


DEFAULT_LLM_MODEL = "llama-3.3-70b-versatile"
DEFAULT_SYSTEM_PROMPT_TAIL = "Reply concisely."
ACTIVITY_HEARTBEAT_SECONDS = 1.0
PREVIEW_MAX_CHARS = 180


class RunResult(NamedTuple):
    reply: str
    used_llm: bool


def _system_prompt_tail() -> str:
    return os.environ.get("AX_BRIDGE_SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT_TAIL


def _build_llm_node(model: str):
    """Build a LangGraph node that streams a Groq chat completion.

    The node takes a state dict with a "prompt" key and returns a dict
    with a "reply" key holding the model's full text response. A short
    system prompt names the routed agent so the model knows who it is
    replying as; the trailing instruction is overridable via the
    AX_BRIDGE_SYSTEM_PROMPT env var.

    Streams chunks via the Groq SDK's `stream=True` mode, accumulates
    them, and emits throttled activity events (~1s heartbeat with a
    rolling preview) so the aX activity feed stays live during the
    call. Mirrors the Ollama bridge's pattern.

    Raises ImportError if the groq SDK is not installed, which the
    caller treats as a signal to fall back to the stub ack node.
    """
    from groq import Groq

    client = Groq()  # picks up GROQ_API_KEY from the environment
    agent = _agent_name()
    system_message = f"You are @{agent}, an assistant routed through the aX Gateway. {_system_prompt_tail()}"

    def _llm_node(state: dict[str, Any]) -> dict[str, Any]:
        emit_event(
            {
                "kind": "status",
                "status": "processing",
                "message": f"Calling Groq ({model})",
            }
        )
        stream = client.chat.completions.create(
            model=model,
            stream=True,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": state.get("prompt", "")},
            ],
        )

        chunks: list[str] = []
        first_token_seen = False
        last_activity_at = 0.0
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None) or ""
            if not text:
                continue
            chunks.append(text)
            now = time.monotonic()
            if not first_token_seen:
                first_token_seen = True
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

        return {"reply": "".join(chunks)}

    return _llm_node


# ---------------------------------------------------------------------------
# Multi-node + ToolNode extension (Sprint 04 task #976).
#
# When tool dispatch is enabled (default; opt-out via AX_BRIDGE_TOOLS_DISABLED)
# and the necessary imports are available (langgraph.prebuilt.ToolNode +
# langchain_core), _run_graph routes to the two-node llm_call <-> tool_node
# agentic loop instead of the original single-node behavior. The cycle is
# bounded by AX_BRIDGE_MAX_ITERATIONS (default 30, clamped to [1, 200]).
#
# The default tool set ships three tools that exercise the security wrapper
# without external dependencies (echo, read_file, list_dir). The security
# wrapper is a verbatim port of the path/command checks in
# ax_cli/runtimes/hermes/tools/__init__.py installed via ToolNode's first-class
# wrap_tool_call API (see PM artifact #183 "LangGraph survey" Q3 answer).
# ---------------------------------------------------------------------------

DEFAULT_MAX_ITERATIONS = 30
MAX_ITERATIONS_FLOOR = 1
MAX_ITERATIONS_CEILING = 200


def _max_iterations() -> int:
    raw = os.environ.get("AX_BRIDGE_MAX_ITERATIONS", "").strip()
    if not raw:
        return DEFAULT_MAX_ITERATIONS
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_ITERATIONS
    return max(MAX_ITERATIONS_FLOOR, min(MAX_ITERATIONS_CEILING, n))


def _tools_disabled() -> bool:
    raw = os.environ.get("AX_BRIDGE_TOOLS_DISABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _strict_security() -> bool:
    raw = os.environ.get("AX_BRIDGE_STRICT_SECURITY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_workdir() -> str:
    """Resolve the agent's workdir for security wrapper path checks.

    Priority: AX_GATEWAY_WORKDIR > os.getcwd() > /tmp/<agent-name>.
    Matches the sentinel.py resolution order used elsewhere in the repo.
    """
    explicit = os.environ.get("AX_GATEWAY_WORKDIR", "").strip()
    if explicit:
        return explicit
    try:
        return os.getcwd()
    except OSError:
        return f"/tmp/{_agent_name()}"


# ── Default tools ──────────────────────────────────────────────────────────
# Three small tools that ship with the bridge to exercise the multi-node
# loop and the security wrapper. They use plain Python; no extra deps. The
# MCP-to-LangChain tool adapter (Sprint 05 follow-up) plugs into the same
# ToolNode and inherits the same security wrapper transparently.


def _default_tools() -> list:
    """Build the default LangChain tool set for the bridge.

    Imported lazily so the bridge still loads when langchain_core is missing
    (degrades to single-node path).
    """
    from langchain_core.tools import tool

    @tool
    def echo(message: str) -> str:
        """Echo the message back. Useful for verifying the tool dispatch loop."""
        return f"echoed: {message}"

    @tool
    def read_file(path: str) -> str:
        """Read a text file from disk and return its contents.

        Security: rejected by the wrap_tool_call middleware if the resolved
        path matches any BLOCKED_READ_PATTERNS (token files, credentials, ssh
        keys, etc.). Limited to ~64KB; longer files truncated.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read(65536)
            return content
        except OSError as exc:
            return f"error: {exc}"

    @tool
    def list_dir(path: str) -> str:
        """List entries in a directory. Returns one entry per line, sorted.

        Security: same path-resolution check as read_file via the wrap_tool_call
        middleware.
        """
        try:
            entries = sorted(os.listdir(path))
            return "\n".join(entries) if entries else "(empty directory)"
        except OSError as exc:
            return f"error: {exc}"

    return [echo, read_file, list_dir]


# ── Security wrapper (Sub-B) ──────────────────────────────────────────────


def _make_security_wrap(workdir: str):
    """Return a wrap_tool_call function enforcing AX Gateway's path/command guards.

    This is the LangGraph-side port of ax_cli/runtimes/hermes/runtimes/
    hermes_sdk.py::_secure_hermes_tools(). The check functions are imported
    verbatim from the hermes vendored tools module; only the installation
    pattern differs (LangGraph's first-class wrap_tool_call API instead of
    Hermes's monkey-patch on registry._tools).

    The wrapper dispatches on the LLM-supplied tool name. Unknown tool names
    are default-allow (so the bridge can grow new tools without a strict
    registration burden); set AX_BRIDGE_STRICT_SECURITY=1 to flip the default
    to deny-on-unknown-tool.

    **Degraded mode.** When ``ax_cli.runtimes.hermes.tools`` cannot be
    imported (typical: running this example outside a full ax-cli install,
    e.g. an IL2 bridge-only deployment), no path/command checks are
    enforceable. Rather than refusing to run, the wrapper returns a
    permissive passthrough so non-security demos still work — but it
    publishes a ``kind: status, status: error`` event so the degradation
    surfaces in ``ax gateway status`` and writes a one-line stderr warning
    so it shows up in the operator's terminal too. Do not run a degraded
    bridge in a deployment where tool sandboxing is part of the trust
    boundary.
    """
    import json as _json

    from langchain_core.messages import ToolMessage

    try:
        from ax_cli.runtimes.hermes.tools import (
            BLOCKED_READ_PATTERNS,
            _check_bash_command,
            _check_read_path,
            _check_write_path,
        )
    except ImportError:
        # Hermes vendor not installed (e.g. running this example outside a
        # full ax-cli install). The wrapper degrades to a permissive
        # passthrough so the bridge stays usable for non-security demos.
        # Surface the degradation as both a status event (for `ax gateway
        # status` consumers) and a stderr line (for any operator running
        # the bridge directly) — a single activity-level event is too
        # quiet for a security-relevant downgrade (#111).
        warning = (
            "security wrapper degraded: ax_cli.runtimes.hermes.tools not importable; "
            "tool calls will pass through unchecked. Install ax-cli with hermes runtimes "
            "to enforce path/command guards."
        )
        emit_event(
            {
                "kind": "status",
                "status": "error",
                "error_message": warning,
            }
        )
        print(f"WARNING: {warning}", file=sys.stderr, flush=True)

        def _passthrough(request, execute):
            return execute(request)

        return _passthrough

    strict = _strict_security()

    def secure_tool_call(request, execute):
        name = request.tool_call["name"]
        args = request.tool_call.get("args", {}) or {}
        call_id = request.tool_call["id"]

        err: str | None = None
        recognized = True

        if name == "bash" or name == "terminal":
            err = _check_bash_command(args.get("command", ""))
        elif name in ("read_file", "search_files", "list_dir", "grep", "glob_files"):
            err = _check_read_path(args.get("path", ""))
        elif name == "write_file":
            err = _check_write_path(args.get("path", ""), workdir)
        elif name in ("patch", "edit_file"):
            err = _check_write_path(args.get("file_path", args.get("path", "")), workdir)
        elif name == "execute_code":
            code = args.get("code", "")
            err = next(
                (f"Code blocked: references {p}" for p in BLOCKED_READ_PATTERNS if p in code),
                None,
            )
        else:
            recognized = False

        if not recognized and strict:
            err = f"Strict security: unrecognized tool '{name}' rejected (set AX_BRIDGE_STRICT_SECURITY=0 to allow)"

        if err:
            emit_event(
                {
                    "kind": "status",
                    "status": "processing",
                    "message": f"Tool '{name}' rejected by security wrapper",
                }
            )
            return ToolMessage(content=_json.dumps({"error": err}), tool_call_id=call_id)

        return execute(request)

    return secure_tool_call


# ── Multi-node loop (Sub-A) ────────────────────────────────────────────────


def _build_groq_chat_model(model: str, tools: list):
    """Build a Groq-backed LangChain chat model with tools bound.

    Uses langchain_groq.ChatGroq when available (langchain-groq is the
    canonical integration). Raises ImportError if neither langchain_groq
    nor a usable fallback is available, signaling the caller to drop back
    to single-node behavior.
    """
    from langchain_groq import ChatGroq

    chat = ChatGroq(model=model, temperature=0.0)
    return chat.bind_tools(tools)


def _run_graph_with_tools(prompt: str, model_name: str, tools: list, workdir: str) -> RunResult:
    """Run the two-node llm_call <-> tool_node agentic loop.

    Returns RunResult with the final assistant message. Cycle is bounded
    by AX_BRIDGE_MAX_ITERATIONS to prevent runaway costs.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    max_iter = _max_iterations()
    agent = _agent_name()
    system_message = (
        f"You are @{agent}, an assistant routed through the aX Gateway. "
        f"You have tools available; prefer to call a tool when it would help. "
        f"{_system_prompt_tail()}"
    )

    try:
        chat = _build_groq_chat_model(model_name, tools)
    except ImportError as exc:
        emit_event(
            {
                "kind": "activity",
                "activity": f"langchain_groq not installed ({exc}); cannot bind tools to Groq; falling back to single-node path",
            }
        )
        raise

    emit_event(
        {
            "kind": "activity",
            "activity": (
                f"building two-node StateGraph (llm_call <-> tool_node, "
                f"{len(tools)} tools, max_iter={max_iter}, "
                f"strict_security={int(_strict_security())})"
            ),
        }
    )

    iteration_counter = {"n": 0}

    # Note: nested function annotations are intentionally untyped. With
    # `from __future__ import annotations` at module top, type hints become
    # strings — but LangGraph's StateGraph resolves them at graph-compile
    # time via typing.get_type_hints(), which looks up names in the function's
    # __globals__ (the module globals), not the enclosing function scope where
    # MessagesState lives. Annotating these locals would NameError at compile
    # time. Caught by live testing on Linux/Python 3.13 (2026-05-23). The
    # graph already knows its state shape from StateGraph(MessagesState).
    def llm_call(state):
        iteration_counter["n"] += 1
        emit_event(
            {
                "kind": "status",
                "status": "processing",
                "message": f"Iteration {iteration_counter['n']}/{max_iter}: calling Groq ({model_name})",
            }
        )
        response = chat.invoke(state["messages"])
        return {"messages": [response]}

    def should_continue(state):
        if iteration_counter["n"] >= max_iter:
            emit_event(
                {
                    "kind": "activity",
                    "activity": f"cycle limit reached ({max_iter}); ending agent loop",
                }
            )
            return END
        last_message = state["messages"][-1]
        tool_calls = getattr(last_message, "tool_calls", None) or []
        if tool_calls:
            names = ",".join(tc.get("name", "?") for tc in tool_calls)
            emit_event(
                {
                    "kind": "status",
                    "status": "processing",
                    "message": f"Tool call(s) requested: {names}",
                }
            )
            return "tool_node"
        return END

    # The security wrapper installs at ToolNode construction time via the
    # public wrap_tool_call API (see PM artifact #183 LangGraph survey
    # Q3 answer). Pure-Python check functions (_check_*) port verbatim from
    # ax_cli/runtimes/hermes/tools/__init__.py.
    tool_node = ToolNode(
        tools,
        handle_tool_errors=True,
        wrap_tool_call=_make_security_wrap(workdir),
    )

    graph = StateGraph(MessagesState)
    graph.add_node("llm_call", llm_call)
    graph.add_node("tool_node", tool_node)
    graph.add_edge(START, "llm_call")
    graph.add_conditional_edges("llm_call", should_continue, ["tool_node", END])
    graph.add_edge("tool_node", "llm_call")
    app = graph.compile()

    initial_state: dict[str, Any] = {
        "messages": [
            SystemMessage(content=system_message),
            HumanMessage(content=prompt),
        ]
    }
    final_state = app.invoke(initial_state)
    final_message = final_state["messages"][-1]
    reply = getattr(final_message, "content", "") or ""
    if not isinstance(reply, str):
        # Defensive: content can be a list of content parts in some LangChain
        # versions; flatten to a single string.
        try:
            reply = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in reply)
        except (TypeError, AttributeError):
            reply = str(reply)
    emit_event(
        {
            "kind": "activity",
            "activity": f"agent loop completed after {iteration_counter['n']} iteration(s)",
        }
    )
    return RunResult(reply=reply, used_llm=True)


def _run_graph(prompt: str) -> RunResult:
    """Run a LangGraph agent (real LLM, stub, or string template).

    Tier 1 (real LLM): builds the two-node llm_call <-> tool_node agentic
    loop when langgraph, groq, langchain_groq, langchain_core are all
    importable, GROQ_API_KEY is set, and AX_BRIDGE_TOOLS_DISABLED is not
    set. Falls back to the original one-node streaming behavior if any of
    those prerequisites are missing.

    Tier 2 (stub graph): one-node StateGraph with a synthetic ack, used
    when langgraph is importable but Groq is not configured.

    Tier 3 (string fallback): plain template, used when langgraph itself
    is not installed.

    Returns a RunResult naming the reply and whether the real LLM path
    was taken so main() can report it in the completion event.
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        emit_event(
            {
                "kind": "activity",
                "activity": "langgraph not installed; using stub reply (install langgraph for real graph execution)",
            }
        )
        return RunResult(
            reply=f"LangGraph stub ack from @{_agent_name()}: {prompt}",
            used_llm=False,
        )

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("AX_BRIDGE_LLM_MODEL", DEFAULT_LLM_MODEL).strip() or DEFAULT_LLM_MODEL

    # Try the multi-node + ToolNode path first when conditions are met.
    if groq_key and not _tools_disabled():
        try:
            tools = _default_tools()
            workdir = _resolve_workdir()
            return _run_graph_with_tools(prompt, model, tools, workdir)
        except ImportError as exc:
            emit_event(
                {
                    "kind": "activity",
                    "activity": (
                        f"multi-node path unavailable ({exc}); falling back to single-node "
                        f"(install langchain-groq + langchain-core to enable tool dispatch)"
                    ),
                }
            )
            # fall through to single-node code below
        except Exception as exc:  # noqa: BLE001 - intentional broad catch
            # Any other failure constructing the multi-node graph (e.g.
            # langgraph version skew that changed the ToolNode signature) also
            # falls back. The bridge stays functional even if the new path
            # breaks against an upstream change.
            emit_event(
                {
                    "kind": "status",
                    "status": "error",
                    "error_message": f"multi-node graph build failed: {exc}; falling back to single-node",
                }
            )

    llm_node = None
    if groq_key:
        try:
            llm_node = _build_llm_node(model)
        except ImportError:
            emit_event(
                {
                    "kind": "activity",
                    "activity": "GROQ_API_KEY set but groq SDK not installed; falling back to stub node",
                }
            )

    if llm_node is not None:
        emit_event(
            {
                "kind": "activity",
                "activity": f"building one-node StateGraph with Groq LLM node (model={model})",
            }
        )
        node = llm_node
        used_llm = True
    else:
        emit_event(
            {
                "kind": "activity",
                "activity": "building one-node StateGraph with stub ack node (no LLM configured)",
            }
        )

        def _ack_node(state: dict[str, Any]) -> dict[str, Any]:
            return {"reply": f"LangGraph ack from @{_agent_name()}: {state.get('prompt', '')}"}

        node = _ack_node
        used_llm = False

    graph = StateGraph(dict)
    graph.add_node("node", node)
    graph.add_edge(START, "node")
    graph.add_edge("node", END)
    app = graph.compile()

    result = app.invoke({"prompt": prompt})
    reply = str(result.get("reply") or "")
    return RunResult(reply=reply, used_llm=used_llm)


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
            "message": "Routing prompt through LangGraph bridge",
        }
    )

    try:
        result = _run_graph(prompt)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"LangGraph bridge failed: {exc}", file=sys.stderr)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"LangGraph bridge completed in {duration_ms}ms",
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
    print(result.reply or f"LangGraph bridge for @{_agent_name()} finished without text.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
