# NEW: not yet vendored from ax-agents. Pending upstream PR before the next
# vendor sync. See ax_cli/runtimes/hermes/README.md for vendoring guidance.
"""Groq SDK runtime — wraps Groq's chat completions API.

Phase 3: multi-turn agent loop with tool calls. The runtime streams a
chat completion, accumulates text and any tool-call deltas, executes
requested tools via the shared `tools` module, and loops until the
model emits a final text-only reply (or max_turns is hit).

Tool definitions in this codebase are stored in OpenAI Responses-API
shape (flat `name` field). Groq speaks chat completions, which expects
the nested `function: { name, ... }` shape, so we adapt on the way out.

Deferred to Phase 4: SDK_PREAMBLE injection, re-prompt on text-only
first turn, rate-limit backoff polish.

Auth: GROQ_API_KEY environment variable.
Models: https://console.groq.com/docs/models
        (default: llama-3.3-70b-versatile)
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.groq_sdk")

DEFAULT_MODEL = "llama-3.3-70b-versatile"
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


@register("groq_sdk")
class GroqSDKRuntime(BaseRuntime):
    """Runs agent turns via the Groq Python SDK.

    Phase 3: multi-turn loop with tool calling. Streams text deltas
    through StreamCallback.on_text_delta, accumulates tool_call deltas
    by index, executes tools through the shared `tools` module, and
    loops until the model produces a final text-only reply or MAX_TURNS
    is reached.
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
        api_key = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key:
            log.error("groq_sdk: GROQ_API_KEY not set in environment")
            return RuntimeResult(
                text="Agent could not authenticate with Groq (GROQ_API_KEY not set).",
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        try:
            from groq import (
                APIStatusError,
                APITimeoutError,
                AuthenticationError,
                Groq,
                InternalServerError,
                PermissionDeniedError,
                RateLimitError,
            )
        except ImportError as e:
            # pyproject.toml does not declare `groq` as a hard dependency, so
            # packaged axctl installs (and the Docker image, which only runs
            # `pip install .`) will not have it. Surface a clean RuntimeResult
            # so the sentinel can render an actionable message instead of
            # crashing on a bare ModuleNotFoundError.
            #
            # Why bare `pip install` and not the managed `ax gateway runtime
            # install groq_sdk` flow: the groq package is a single small wheel
            # with no native code, no venv scaffolding, and no post-install
            # verification needed (unlike hermes-agent, which the managed
            # installer was built for per AUTOSETUP-001). Bare pip is enough
            # for now. If groq_sdk grows native deps, multi-process state, or
            # post-install verification needs, this is the right moment to
            # move it under the managed-install allowlist.
            log.error(f"groq_sdk: groq Python SDK is not installed ({e})")
            return RuntimeResult(
                text=(
                    "Agent could not start because the `groq` Python package "
                    "is not installed in this runtime environment. "
                    "Install it with `pip install groq` and retry."
                ),
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        # Absolute import matches openai_sdk.py and the other sibling runtimes.
        # The Hermes sentinel prepends ax_cli/runtimes/hermes to sys.path and
        # loads this module as `runtimes.groq_sdk`, so a relative `from ..tools`
        # would escape past the top-level package and raise ImportError at runtime.
        # Tests in tests/test_groq_sdk_runtime.py insert the same hermes directory
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
        client = Groq(api_key=api_key)

        for turn in range(MAX_TURNS):
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                log.warning(
                    f"groq_sdk: timeout exceeded at turn {turn + 1} "
                    f"(budget={timeout}s, elapsed {int(now - start_time)}s)"
                )
                return RuntimeResult(
                    text=(
                        final_text
                        or "Agent timed out before producing a final answer."
                    ),
                    history=history,
                    session_id=None,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="timeout",
                    elapsed_seconds=int(now - start_time),
                )

            log.info(f"groq_sdk: turn {turn + 1}, {len(history)} messages")

            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": instructions},
                        *history,
                    ],
                    tools=tools,
                    stream=True,
                    timeout=remaining,
                )
            except RateLimitError as e:
                # 429. Throttle, surface the status code so the operator can
                # tell rate-limit from auth-fail at a glance.
                log.warning(f"groq_sdk: rate limited (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Groq API rate-limited (HTTP {e.status_code}). Retry after a short delay."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="rate_limited",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except APITimeoutError as e:
                # Connection or read timeout from the Groq SDK (httpx-backed).
                # Distinct from a sentinel-budget timeout, but maps to the same
                # exit_reason since the user-visible cause is identical.
                log.error(f"groq_sdk: API timeout: {e}")
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
            except (AuthenticationError, PermissionDeniedError) as e:
                # 401 / 403. Operator-actionable. Never silently swallow auth
                # failures — the user must see them in the chat reply so they
                # can rotate or fix the GROQ_API_KEY.
                log.error(f"groq_sdk: auth failed (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=f"Groq authentication failed (HTTP {e.status_code}). Check GROQ_API_KEY.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="auth_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except InternalServerError as e:
                # 5xx from Groq. Retry is plausible; signal that to the operator.
                log.error(f"groq_sdk: server error (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Groq API returned HTTP {e.status_code}. Retry may succeed."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="server_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except APIStatusError as e:
                # Any other 4xx not matched above (e.g. 400 BadRequest,
                # 422 UnprocessableEntity, 404 NotFound). Surface the status
                # and the message so the operator knows what to fix.
                log.error(f"groq_sdk: API error (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Groq API error (HTTP {e.status_code}): {e.message}"
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="api_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except Exception as e:
                # Catch-all for anything outside the Groq SDK's typed exception
                # hierarchy (network adapter bugs, connection refused before an
                # APIConnectionError, etc.). Logged with full repr so the
                # underlying type is visible in ops triage.
                log.error(f"groq_sdk: unexpected error opening stream: {e!r}")
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
                for chunk in stream:
                    if not chunk.choices:
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
                log.error(f"groq_sdk: stream error after {len(turn_text)} chars: {e}")
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
                            f"groq_sdk: timeout exceeded before tool "
                            f"{tc['function']['name']} "
                            f"(elapsed {int(now_tool - start_time)}s)"
                        )
                        return RuntimeResult(
                            text=(
                                final_text
                                or "Agent timed out before completing tool calls."
                            ),
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
                    log.info(
                        f"groq_sdk: tool {name}({json.dumps(args, default=str)[:80]})"
                    )
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
                f"groq_sdk: hit MAX_TURNS={MAX_TURNS} without final answer "
                f"(elapsed {elapsed}s, {tool_count} tools)"
            )
            return RuntimeResult(
                text=(
                    final_text
                    or "Agent hit the maximum turn limit without producing a final answer."
                ),
                history=history,
                session_id=None,
                tool_count=tool_count,
                files_written=files_written,
                exit_reason="iteration_limit",
                elapsed_seconds=elapsed,
            )

        elapsed = int(time.time() - start_time)
        log.info(
            f"groq_sdk: done in {elapsed}s, {tool_count} tools, "
            f"{len(final_text)} chars"
        )
        return RuntimeResult(
            text=final_text,
            history=history,
            session_id=None,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason="done",
            elapsed_seconds=elapsed,
        )
