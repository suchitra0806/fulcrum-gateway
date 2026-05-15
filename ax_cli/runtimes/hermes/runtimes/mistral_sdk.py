# NEW: not yet vendored from ax-agents. Pending upstream PR before the next
# vendor sync. See ax_cli/runtimes/hermes/README.md for vendoring guidance.
"""Mistral SDK runtime — wraps Mistral's chat completions API.

Multi-turn agent loop with tool calls. The runtime streams a chat
completion, accumulates text and any tool-call deltas, executes
requested tools via the shared `tools` module, and loops until the
model emits a final text-only reply (or MAX_TURNS is hit).

Tool definitions in this codebase are stored in OpenAI Responses-API
shape (flat `name` field). Mistral speaks chat completions, which
expects the nested `function: { name, ... }` shape, so we adapt on
the way out.

Auth: MISTRAL_API_KEY environment variable.
Models: https://docs.mistral.ai/getting-started/models/models_overview/
        (default: mistral-large-latest)
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.mistral_sdk")

DEFAULT_MODEL = "mistral-large-latest"
MAX_TURNS = 25
TOOL_OUTPUT_CAP = 10_000  # bytes of tool output fed back to the model per call


def _to_chat_completion_tool(rd_tool: dict) -> dict:
    """Convert a Responses-API tool definition to chat.completions shape."""
    return {
        "type": "function",
        "function": {
            "name": rd_tool["name"],
            "description": rd_tool.get("description", ""),
            "parameters": rd_tool.get("parameters", {}),
        },
    }


def _tool_display(name: str, args: dict) -> str:
    """Human-readable one-liner for tool activity log."""
    if name in ("read_file", "write_file", "edit_file"):
        p = args.get("path", "")
        verb = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit"}[name]
        tail = p.rsplit("/", 1)[-1] if "/" in p else p
        return f"{verb} {tail}"
    if name == "bash":
        cmd = str(args.get("command", ""))[:60]
        return f"Run: {cmd}"
    if name == "grep":
        return f"Search: {args.get('pattern', '')}"
    if name == "glob_files":
        return f"Find: {args.get('pattern', '')}"
    return name


@register("mistral_sdk")
class MistralSDKRuntime(BaseRuntime):
    """Runs agent turns via the Mistral Python SDK.

    Multi-turn loop with tool calling. Buffers text per turn and emits
    via StreamCallback.on_text_complete once the turn is confirmed
    text-only (prevents pre-tool chatter from leaking to chat).
    Accumulates tool_call deltas by index, executes tools through the
    shared `tools` module, and loops until the model produces a final
    text-only reply or MAX_TURNS is reached.
    """

    def execute(
        self,
        message: str,
        *,
        workdir: str,
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        stream_cb: StreamCallback | None = None,
        timeout: int = 300,
        extra_args: dict | None = None,
    ) -> RuntimeResult:
        api_key = os.environ.get("MISTRAL_API_KEY", "").strip()
        if not api_key:
            log.error("mistral_sdk: MISTRAL_API_KEY not set in environment")
            return RuntimeResult(
                text="Agent could not authenticate with Mistral (MISTRAL_API_KEY not set).",
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        try:
            # mistralai 2.x has no top-level __init__.py re-exporting Mistral.
            # The client class lives at mistralai.client.sdk.Mistral (Speakeasy-
            # generated SDK). Importing from `mistralai` directly raises
            # ImportError with "(unknown location)" because the bare package is
            # a namespace stub.
            #
            # Mistral's exception hierarchy is also imported here so the catch
            # chain on the streaming call below can dispatch by class. Unlike
            # Groq (which has per-status-code exception classes), Mistral
            # surfaces all HTTP errors as SDKError and asks the caller to
            # branch on .status_code. We honor that by catching SDKError once
            # and dispatching by code inside the handler.
            from mistralai.client.errors import MistralError, SDKError
            from mistralai.client.sdk import Mistral
        except ImportError as e:
            # pyproject.toml does not declare `mistralai` as a hard dependency, so
            # packaged axctl installs (and the Docker image, which only runs
            # `pip install .`) will not have it. Surface a clean RuntimeResult
            # so the sentinel can render an actionable message instead of
            # crashing on a bare ModuleNotFoundError.
            #
            # Why bare `pip install` and not the managed `ax gateway runtime
            # install mistral_sdk` flow: the mistralai package is a single
            # wheel with httpx/pydantic/opentelemetry transitive deps that
            # any modern Python project already has. No native code, no venv
            # scaffolding, no post-install verification needed. Bare pip is
            # enough for now. If mistral_sdk grows native deps, multi-process
            # state, or post-install verification needs, this is the right
            # moment to move it under the managed-install allowlist.
            log.error(f"mistral_sdk: mistralai Python SDK is not installed ({e})")
            return RuntimeResult(
                text=(
                    "Agent could not start because the `mistralai` Python package "
                    "is not installed in this runtime environment. "
                    "Install it with `pip install mistralai` and retry."
                ),
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        # Absolute import matches openai_sdk.py and the other sibling runtimes.
        # The Hermes sentinel prepends ax_cli/runtimes/hermes to sys.path and
        # loads this module as `runtimes.mistral_sdk`, so a relative `from ..tools`
        # would escape past the top-level package and raise ImportError at runtime.
        # Tests in tests/test_mistral_sdk_runtime.py insert the same hermes directory
        # into sys.path so the absolute form resolves there too.
        from tools import TOOL_DEFINITIONS, execute_tool

        cb = stream_cb or StreamCallback()
        model = model or DEFAULT_MODEL
        instructions = system_prompt or "You are a helpful coding assistant."

        tools = [_to_chat_completion_tool(t) for t in TOOL_DEFINITIONS]

        start_time = time.time()
        deadline = start_time + timeout
        history: list[dict] = list((extra_args or {}).get("history", []))
        history.append({"role": "user", "content": message})

        final_text = ""
        tool_count = 0
        files_written: list[str] = []
        client = Mistral(api_key=api_key)

        for turn in range(MAX_TURNS):
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                log.warning(
                    f"mistral_sdk: timeout exceeded at turn {turn + 1} "
                    f"(budget={timeout}s, elapsed {int(now - start_time)}s)"
                )
                return RuntimeResult(
                    text=(final_text or "Agent timed out before producing a final answer."),
                    history=history,
                    session_id=None,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="timeout",
                    elapsed_seconds=int(now - start_time),
                )

            log.info(f"mistral_sdk: turn {turn + 1}, {len(history)} messages")

            try:
                # NOTE: mistralai 1.x uses `client.chat.stream(...)` rather than
                # `client.chat.completions.create(stream=True)`. The stream
                # yields wrapper events whose `.data` attribute holds the
                # OpenAI-shaped chunk. See the stream iteration below.
                stream = client.chat.stream(
                    model=model,
                    messages=[
                        {"role": "system", "content": instructions},
                        *history,
                    ],
                    tools=tools,
                    tool_choice="auto",
                )
            except SDKError as e:
                # Mistral's catch-all HTTP error class. Has .status_code,
                # .message, .body. Unlike Groq's per-status exception classes,
                # Mistral funnels all HTTP errors here and expects the caller
                # to branch on .status_code. We honor that by classifying by
                # code so the exit_reason and operator text still match what
                # an operator can act on (rate-limit vs auth vs server vs
                # generic 4xx). Catch order matters — SDKError is a subclass
                # of MistralError, so this arm must precede the MistralError
                # arm below.
                code = getattr(e, "status_code", 0)
                msg = getattr(e, "message", "") or str(e)
                if code == 429:
                    log.warning(f"mistral_sdk: rate limited (HTTP {code}): {msg}")
                    return RuntimeResult(
                        text=(
                            final_text
                            or f"Mistral API rate-limited (HTTP {code}). Retry after a short delay."
                        ),
                        history=history,
                        tool_count=tool_count,
                        files_written=files_written,
                        exit_reason="rate_limited",
                        elapsed_seconds=int(time.time() - start_time),
                    )
                if code in (401, 403):
                    # Operator-actionable. Never silently swallow auth failures
                    # — the user must see them in the chat reply so they can
                    # rotate or fix the MISTRAL_API_KEY.
                    log.error(f"mistral_sdk: auth failed (HTTP {code}): {msg}")
                    return RuntimeResult(
                        text=f"Mistral authentication failed (HTTP {code}). Check MISTRAL_API_KEY.",
                        history=history,
                        tool_count=tool_count,
                        files_written=files_written,
                        exit_reason="auth_error",
                        elapsed_seconds=int(time.time() - start_time),
                    )
                if 500 <= code < 600:
                    log.error(f"mistral_sdk: server error (HTTP {code}): {msg}")
                    return RuntimeResult(
                        text=(
                            final_text
                            or f"Mistral API returned HTTP {code}. Retry may succeed."
                        ),
                        history=history,
                        tool_count=tool_count,
                        files_written=files_written,
                        exit_reason="server_error",
                        elapsed_seconds=int(time.time() - start_time),
                    )
                # Other 4xx (400 BadRequest, 404 NotFound, etc.).
                log.error(f"mistral_sdk: API error (HTTP {code}): {msg}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Mistral API error (HTTP {code}): {msg}"
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="api_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except httpx.TimeoutException as e:
                # mistralai 2.x is built on httpx and lets transport-layer
                # timeouts surface as httpx.TimeoutException rather than
                # wrapping them in an SDK-specific class. Treat them the
                # same as a sentinel-budget timeout from the caller's side.
                log.error(f"mistral_sdk: HTTP timeout: {e!r}")
                return RuntimeResult(
                    text=(
                        final_text
                        or "Agent timed out while waiting for the model."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="timeout",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except MistralError as e:
                # Other Mistral SDK errors that aren't SDKError (validation
                # errors, observability errors, etc.). These don't carry an
                # HTTP status in the same shape, so we surface them as a
                # generic api_error with the message text.
                log.error(f"mistral_sdk: Mistral SDK error: {e!r}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Mistral SDK error: {e!s}"
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="api_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except Exception as e:
                # Catch-all for anything outside the Mistral SDK and httpx
                # exception hierarchies (network adapter bugs, mistral SDK
                # version skew, etc.). Logged with full repr so the
                # underlying type is visible in ops triage.
                log.error(f"mistral_sdk: unexpected error opening stream: {e!r}")
                return RuntimeResult(
                    text=final_text or "Agent encountered an unexpected error and could not complete the task.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="crashed",
                    elapsed_seconds=int(time.time() - start_time),
                )

            # Accumulate text and tool_call deltas across the stream.
            turn_text = ""
            tool_calls_acc: dict[int, dict] = {}

            try:
                for event in stream:
                    # mistralai 1.x wraps each chunk in an event object; the
                    # OpenAI-shaped chunk is under `.data`. Unwrap defensively
                    # so we keep working if a future SDK version yields the
                    # chunk directly.
                    chunk = getattr(event, "data", event)
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta

                    # Buffer text locally for this turn. Don't publish to the
                    # callback yet — if the model pivots into tool calls, this
                    # is pre-tool chatter (e.g. "I'll inspect...") that would
                    # leak as visible chat content and suppress the sentinel's
                    # tool-progress UI. We only emit via on_text_complete when
                    # the turn is confirmed text-only (no tool calls). Mirrors
                    # the buffering pattern in openai_sdk.py.
                    content = getattr(delta, "content", None)
                    if content:
                        turn_text += content

                    tc_deltas = getattr(delta, "tool_calls", None) or []
                    for tc_d in tc_deltas:
                        idx = getattr(tc_d, "index", 0)
                        slot = tool_calls_acc.setdefault(
                            idx,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        tc_id = getattr(tc_d, "id", None)
                        if tc_id:
                            slot["id"] = tc_id
                        fn_delta = getattr(tc_d, "function", None)
                        if fn_delta is not None:
                            fn_name = getattr(fn_delta, "name", None)
                            if fn_name:
                                slot["function"]["name"] = fn_name
                            fn_args = getattr(fn_delta, "arguments", None)
                            if fn_args:
                                slot["function"]["arguments"] += fn_args
            except Exception as e:
                log.error(f"mistral_sdk: stream error after {len(turn_text)} chars: {e}")
                partial = turn_text.strip()
                if partial:
                    history.append({"role": "assistant", "content": partial})
                return RuntimeResult(
                    text=partial or "Agent encountered a stream error mid-response.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="crashed",
                    elapsed_seconds=int(time.time() - start_time),
                )

            tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]

            # If the model requested tools, execute them and continue the loop.
            if tool_calls:
                history.append(
                    {
                        "role": "assistant",
                        "content": turn_text or None,
                        "tool_calls": tool_calls,
                    }
                )

                for tc in tool_calls:
                    # Re-check the deadline before each tool. A long-running
                    # tool can otherwise block the listener well past the
                    # operator's --timeout.
                    now_tool = time.time()
                    remaining_for_tool = deadline - now_tool
                    if remaining_for_tool <= 0:
                        log.warning(
                            f"mistral_sdk: timeout exceeded before tool "
                            f"{tc['function']['name']} "
                            f"(elapsed {int(now_tool - start_time)}s)"
                        )
                        return RuntimeResult(
                            text=(final_text or "Agent timed out before completing tool calls."),
                            history=history,
                            session_id=None,
                            tool_count=tool_count,
                            files_written=files_written,
                            exit_reason="timeout",
                            elapsed_seconds=int(now_tool - start_time),
                        )

                    tool_count += 1
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args = {}

                    # Clamp any model-supplied "timeout" arg to the remaining
                    # wall-clock budget. Tools like `bash` honor args["timeout"]
                    # directly, so without this a model could request a 600s
                    # bash inside a 30s sentinel budget. Tools without a
                    # "timeout" arg are unaffected.
                    if "timeout" in args:
                        try:
                            args["timeout"] = min(
                                int(args["timeout"]),
                                max(1, int(remaining_for_tool)),
                            )
                        except (TypeError, ValueError):
                            args["timeout"] = max(1, int(remaining_for_tool))

                    summary = _tool_display(name, args)
                    log.info(f"mistral_sdk: tool {name}({json.dumps(args, default=str)[:80]})")
                    cb.on_tool_start(name, summary)
                    result = execute_tool(name, args, workdir)

                    if name == "write_file" and not result.is_error:
                        files_written.append(args.get("path", ""))

                    short = result.output[:200] if result.output else ""
                    cb.on_tool_end(name, short)

                    # Cap tool output at TOOL_OUTPUT_CAP bytes to bound context
                    # growth, and surface a truncation marker when we hit the
                    # cap so the model can tell content was clipped (otherwise
                    # it may reason as if it has the full output, e.g. assume a
                    # large file was fully read).
                    full_output = result.output or ""
                    if len(full_output) > TOOL_OUTPUT_CAP:
                        tool_content = full_output[:TOOL_OUTPUT_CAP] + "\n[output truncated]"
                    else:
                        tool_content = full_output
                    history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_content,
                        }
                    )

                cb.on_status("thinking")
                continue  # Next turn: model sees tool results.

            # No tool calls — text-only response. Treat as final.
            visible = turn_text.strip()
            if visible:
                final_text = visible
                cb.on_text_complete(final_text)
                history.append({"role": "assistant", "content": visible})
            break
        else:
            # The for-loop completed without break, meaning every turn produced
            # tool calls and the model never finalized. Surface this as
            # iteration_limit so the sentinel renders a bounded-loop notice
            # rather than a misleading "Completed with no text output".
            elapsed = int(time.time() - start_time)
            log.warning(
                f"mistral_sdk: hit MAX_TURNS={MAX_TURNS} without final answer (elapsed {elapsed}s, {tool_count} tools)"
            )
            return RuntimeResult(
                text=(final_text or "Agent hit the maximum turn limit without producing a final answer."),
                history=history,
                session_id=None,
                tool_count=tool_count,
                files_written=files_written,
                exit_reason="iteration_limit",
                elapsed_seconds=elapsed,
            )

        elapsed = int(time.time() - start_time)
        log.info(f"mistral_sdk: done in {elapsed}s, {tool_count} tools, {len(final_text)} chars")
        return RuntimeResult(
            text=final_text,
            history=history,
            session_id=None,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason="done",
            elapsed_seconds=elapsed,
        )
