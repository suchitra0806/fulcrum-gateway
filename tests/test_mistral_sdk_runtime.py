"""Tests for the Mistral SDK runtime adapter.

The Mistral SDK is mocked via sys.modules so these tests run offline and
do not consume API credits. Coverage spans registration discovery, the
missing-API-key path, the happy streaming path (callback fan-out,
RuntimeResult shape, history accumulation), system prompt threading,
and partial-failure handling when the stream raises mid-response.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest  # noqa: F401  (pytest is the test runner; import keeps tooling happy)

# The Hermes sentinel prepends ax_cli/runtimes/hermes to sys.path in production
# so vendored runtimes can do `from tools import ...` as an absolute import.
# Replicate that here so the same import path resolves under pytest.
_HERMES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ax_cli",
    "runtimes",
    "hermes",
)
if _HERMES_DIR not in sys.path:
    sys.path.insert(0, _HERMES_DIR)

# Importing the module triggers `@register("mistral_sdk")` at module load time,
# so the runtime is in REGISTRY regardless of which other tests in the suite
# may have already populated it (get_runtime's auto-discovery only fires when
# REGISTRY is fully empty).
from ax_cli.runtimes.hermes.runtimes import mistral_sdk  # noqa: F401, E402

# ── Helpers ────────────────────────────────────────────────────────────────


def _fake_chunk(content: str | None):
    """Build a duck-typed chat.completions chunk holding a single delta."""
    delta = types.SimpleNamespace(content=content, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta, finish_reason=None)
    return types.SimpleNamespace(choices=[choice])


def _fake_tool_call_delta(index, *, call_id=None, name=None, arguments=None):
    """Build one tool_call delta entry as the SDK yields it inside a chunk."""
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(
        index=index,
        id=call_id,
        type="function" if call_id else None,
        function=fn,
    )


def _fake_chunk_with_tool_calls(tool_call_deltas):
    """Build a chat.completions chunk that carries tool_call deltas (no text)."""
    delta = types.SimpleNamespace(content=None, tool_calls=tool_call_deltas)
    choice = types.SimpleNamespace(delta=delta, finish_reason=None)
    return types.SimpleNamespace(choices=[choice])


def _install_fake_mistral(monkeypatch, fake_client):
    """Swap both `mistralai.client.sdk` and `mistralai.client.errors` in
    sys.modules with stub modules so the runtime's typed imports resolve
    cleanly without requiring the real `mistralai` package to be installed.

    mistralai is an optional dependency; CI runs `pip install .` which does
    not pull it in. Same lesson as the Groq stub helper: don't import the
    real package, define stub classes inline.

    The runtime imports:
      - `from mistralai.client.sdk import Mistral`         (the client class)
      - `from mistralai.client.errors import MistralError, SDKError`  (typed catches)

    The stub exception classes mirror the relevant slice of the real Mistral
    SDK's exception tree. SDKError inherits from MistralError because the
    runtime relies on that ordering when classifying status codes (SDKError
    arm catches first, MistralError arm catches the rest).

    Returns a SimpleNamespace with the stub Mistral mock and exception
    classes so individual tests can raise instances that the runtime will
    catch by class identity.
    """
    import types as _types

    class MistralError(Exception):
        def __init__(self, message="", *, status_code=0, **_kwargs):
            super().__init__(message)
            self.message = message
            self.status_code = status_code

    class SDKError(MistralError):
        pass

    sdk_module = types.ModuleType("mistralai.client.sdk")
    sdk_module.Mistral = MagicMock(return_value=fake_client)
    monkeypatch.setitem(sys.modules, "mistralai.client.sdk", sdk_module)

    errors_module = types.ModuleType("mistralai.client.errors")
    errors_module.MistralError = MistralError
    errors_module.SDKError = SDKError
    monkeypatch.setitem(sys.modules, "mistralai.client.errors", errors_module)

    return _types.SimpleNamespace(
        Mistral=sdk_module.Mistral,
        MistralError=MistralError,
        SDKError=SDKError,
        sdk_module=sdk_module,
        errors_module=errors_module,
    )


class _RecordingCallback:
    """Minimal StreamCallback implementation that records what it sees."""

    def __init__(self):
        self.deltas: list[str] = []
        self.complete: str | None = None
        self.statuses: list[str] = []

    def on_text_delta(self, text: str) -> None:
        self.deltas.append(text)

    def on_text_complete(self, text: str) -> None:
        self.complete = text

    def on_tool_start(self, *_args, **_kwargs) -> None:
        pass

    def on_tool_end(self, *_args, **_kwargs) -> None:
        pass

    def on_status(self, status: str) -> None:
        self.statuses.append(status)


# ── Tests ──────────────────────────────────────────────────────────────────


def test_mistral_sdk_registers_under_expected_name():
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    rt = get_runtime("mistral_sdk")
    assert type(rt).__name__ == "MistralSDKRuntime"
    assert rt.name == "mistral_sdk"


def test_mistral_sdk_returns_crashed_when_api_key_missing(monkeypatch):
    """No API key in env should short-circuit before any mistralai import."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    rt = get_runtime("mistral_sdk")
    result = rt.execute("hello", workdir="/tmp")

    assert result.exit_reason == "crashed"
    assert "MISTRAL_API_KEY" in result.text
    assert result.elapsed_seconds == 0


def test_mistral_sdk_streams_chunks_and_accumulates_history(monkeypatch):
    """Happy path: deltas fire on the callback, history grows, RuntimeResult is shaped."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")
    fake_client = MagicMock()
    fake_client.chat.stream.return_value = iter(
        [
            _fake_chunk("Hello "),
            _fake_chunk("world."),
        ]
    )
    _install_fake_mistral(monkeypatch, fake_client)

    rt = get_runtime("mistral_sdk")
    cb = _RecordingCallback()
    result = rt.execute(
        "Say hello.",
        workdir="/tmp",
        stream_cb=cb,
    )

    # No incremental deltas should fire; text is buffered until the turn is
    # confirmed text-only (no tool calls), matching openai_sdk.py's pattern.
    assert cb.deltas == []
    # on_text_complete fires with the assembled text.
    assert cb.complete == "Hello world."
    # RuntimeResult fields.
    assert result.exit_reason == "done"
    assert result.text == "Hello world."
    assert result.tool_count == 0
    assert result.files_written == []
    # History records the round trip: user prompt + assistant reply.
    assert len(result.history) == 2
    assert result.history[0] == {"role": "user", "content": "Say hello."}
    assert result.history[1] == {"role": "assistant", "content": "Hello world."}
    # The runtime invoked the streaming entry point. Unlike Groq's
    # chat.completions.create(stream=True), mistralai 2.x has a dedicated
    # chat.stream(...) method, so the assertion is on the method itself
    # rather than on a kwarg flag.
    assert fake_client.chat.stream.call_count == 1


def test_mistral_sdk_threads_system_prompt_into_messages(monkeypatch):
    """The system_prompt arg should become the first message with role=system."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")
    fake_client = MagicMock()
    fake_client.chat.stream.return_value = iter([_fake_chunk("ok")])
    _install_fake_mistral(monkeypatch, fake_client)

    rt = get_runtime("mistral_sdk")
    rt.execute(
        "Question.",
        workdir="/tmp",
        system_prompt="You are a strict reviewer.",
    )

    messages = fake_client.chat.stream.call_args.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a strict reviewer."
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Question."


def test_mistral_sdk_dispatches_tool_call_and_continues_to_final_answer(monkeypatch):
    """Model emits a tool_call streamed across chunks; runtime executes it, threads
    the result into history with role=tool, and finalizes on the next turn."""
    # Production code imports `from tools import ...` (absolute) because the
    # hermes sentinel puts ax_cli/runtimes/hermes on sys.path. We do the same
    # in module setup above, so this import lands on the same module object
    # that the runtime will read.
    import tools as tools_mod

    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")

    # Turn 1: tool_call streamed across two chunks. First chunk carries
    # id + name; second chunk only accumulates arguments.
    turn1 = iter(
        [
            _fake_chunk_with_tool_calls(
                [
                    _fake_tool_call_delta(0, call_id="call_abc", name="read_file", arguments=""),
                ]
            ),
            _fake_chunk_with_tool_calls(
                [
                    _fake_tool_call_delta(0, arguments='{"path": "/etc/hostname"}'),
                ]
            ),
        ]
    )
    # Turn 2: plain text finalization.
    turn2 = iter([_fake_chunk("The hostname is foo.")])

    fake_client = MagicMock()
    fake_client.chat.stream.side_effect = [turn1, turn2]
    _install_fake_mistral(monkeypatch, fake_client)

    # Stub execute_tool so we do not touch the real filesystem.
    monkeypatch.setattr(
        tools_mod,
        "execute_tool",
        lambda name, args, workdir: tools_mod.ToolResult(output=f"stubbed {name}({args})"),
    )

    rt = get_runtime("mistral_sdk")
    cb = _RecordingCallback()
    result = rt.execute("Read /etc/hostname.", workdir="/tmp", stream_cb=cb)

    assert result.exit_reason == "done"
    assert result.text == "The hostname is foo."
    assert result.tool_count == 1
    # Two turns = two API calls.
    assert fake_client.chat.stream.call_count == 2

    # History shape: user, assistant-with-tool-calls, tool result, final assistant.
    roles = [h.get("role") for h in result.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # Tool call assembled correctly across the two chunks.
    assistant_with_tools = result.history[1]
    tc = assistant_with_tools["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "read_file"
    assert tc["function"]["arguments"] == '{"path": "/etc/hostname"}'
    # Tool message references the call_id.
    assert result.history[2]["tool_call_id"] == "call_abc"
    assert "stubbed read_file" in result.history[2]["content"]
    # Final assistant carries the visible reply.
    assert result.history[3]["content"] == "The hostname is foo."
    # Tool execution surfaces through the callback.
    assert cb.statuses == ["thinking"]


def test_mistral_sdk_preserves_partial_text_on_mid_stream_error(monkeypatch):
    """If the stream raises mid-response, already-received text must not be lost."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")

    def explode_after_two():
        yield _fake_chunk("Partial ")
        yield _fake_chunk("reply")
        raise RuntimeError("stream broke")

    fake_client = MagicMock()
    fake_client.chat.stream.return_value = explode_after_two()
    _install_fake_mistral(monkeypatch, fake_client)

    rt = get_runtime("mistral_sdk")
    cb = _RecordingCallback()
    result = rt.execute("Say hello.", workdir="/tmp", stream_cb=cb)

    # Partial text preserved in both the RuntimeResult and history.
    assert result.text == "Partial reply"
    assert result.exit_reason == "crashed"
    assert any(h.get("role") == "assistant" and h.get("content") == "Partial reply" for h in result.history)
    # Text is buffered locally during the stream and is never emitted as
    # incremental deltas, so the callback sees no on_text_delta calls even
    # though the partial text is preserved in result.text and history.
    assert cb.deltas == []


def test_mistral_sdk_handles_missing_mistralai_package_gracefully(monkeypatch):
    """If the `mistralai` SDK is not installed, return a clean RuntimeResult
    instead of letting ModuleNotFoundError kill the sentinel."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "msk_test")
    # Force `from mistralai.client.sdk import Mistral` to raise ImportError
    # by setting the submodule entry in sys.modules to None (Python treats
    # this as "not importable").
    monkeypatch.setitem(sys.modules, "mistralai.client.sdk", None)

    rt = get_runtime("mistral_sdk")
    result = rt.execute("hello", workdir="/tmp")

    assert result.exit_reason == "crashed"
    # Message should mention the missing package so the operator can act.
    assert "mistralai" in result.text.lower()
    assert "pip install" in result.text


def test_mistral_sdk_clamps_tool_timeout_to_remaining_budget(monkeypatch):
    """A model-supplied `timeout` arg on a tool call should be clamped down
    to the wall-clock budget remaining, so a single tool cannot block the
    listener past the operator's --timeout."""
    from itertools import chain, repeat

    import tools as tools_mod

    from ax_cli.runtimes.hermes.runtimes import get_runtime
    from ax_cli.runtimes.hermes.runtimes import mistral_sdk as mistral_mod

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")

    # Fake clock: start at t=0, every later read returns t=5. With timeout=30,
    # remaining_for_tool ends up ~25s, so a model-supplied 600s timeout must
    # be clamped down to 25.
    clock = chain([0.0], repeat(5.0))
    monkeypatch.setattr(mistral_mod.time, "time", lambda: next(clock))

    # Turn 1: one bash tool call asking for a 600-second budget.
    turn1 = iter(
        [
            _fake_chunk_with_tool_calls(
                [
                    _fake_tool_call_delta(
                        0,
                        call_id="call_bash",
                        name="bash",
                        arguments='{"command":"sleep 999","timeout":600}',
                    ),
                ]
            ),
        ]
    )
    # Turn 2: text-only finalization so the runtime exits cleanly.
    turn2 = iter([_fake_chunk("ok")])

    fake_client = MagicMock()
    fake_client.chat.stream.side_effect = [turn1, turn2]
    _install_fake_mistral(monkeypatch, fake_client)

    captured: list[dict] = []

    def recording_execute_tool(name, args, workdir):
        captured.append({"name": name, "args": dict(args)})
        return tools_mod.ToolResult(output="stubbed")

    monkeypatch.setattr(tools_mod, "execute_tool", recording_execute_tool)

    rt = get_runtime("mistral_sdk")
    rt.execute("run it", workdir="/tmp", timeout=30)

    assert captured, "execute_tool should have been invoked"
    forwarded = captured[0]["args"]
    # The model asked for 600 but only ~25 seconds remained in the budget.
    assert forwarded["timeout"] <= 25
    # And it must still be a positive value (not zero or negative).
    assert forwarded["timeout"] >= 1


def test_mistral_sdk_returns_timeout_when_deadline_exceeded(monkeypatch):
    """When wall-clock exceeds the timeout budget before the first turn can
    open a stream, the runtime should return exit_reason='timeout' rather
    than blocking the sentinel past its configured per-invocation budget."""
    from itertools import chain, repeat

    from ax_cli.runtimes.hermes.runtimes import get_runtime
    from ax_cli.runtimes.hermes.runtimes import mistral_sdk as mistral_mod

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")

    # Fake clock. First call (captures start_time) returns 0; every later
    # call returns 2, which is already past the 1-second timeout.
    clock = chain([0.0], repeat(2.0))
    monkeypatch.setattr(mistral_mod.time, "time", lambda: next(clock))

    fake_client = MagicMock()
    # Should never be called because the deadline check fires first.
    fake_client.chat.stream.side_effect = AssertionError("API should not be called once deadline has passed")
    _install_fake_mistral(monkeypatch, fake_client)

    rt = get_runtime("mistral_sdk")
    result = rt.execute("any prompt", workdir="/tmp", timeout=1)

    assert result.exit_reason == "timeout"
    assert fake_client.chat.stream.call_count == 0
    assert "timed out" in result.text.lower()


def test_mistral_sdk_returns_iteration_limit_when_max_turns_exhausted(monkeypatch):
    """If the model keeps producing tool calls and never finalizes, the runtime
    should exit with exit_reason='iteration_limit' rather than a misleading 'done'."""
    import tools as tools_mod

    from ax_cli.runtimes.hermes.runtimes import get_runtime
    from ax_cli.runtimes.hermes.runtimes.mistral_sdk import MAX_TURNS

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")

    counter = {"n": 0}

    def one_turn_with_tool_call(*_args, **_kwargs):
        counter["n"] += 1
        return iter(
            [
                _fake_chunk_with_tool_calls(
                    [
                        _fake_tool_call_delta(
                            0,
                            call_id=f"call_{counter['n']}",
                            name="bash",
                            arguments='{"command":"ls"}',
                        ),
                    ]
                ),
            ]
        )

    fake_client = MagicMock()
    fake_client.chat.stream.side_effect = one_turn_with_tool_call
    _install_fake_mistral(monkeypatch, fake_client)
    monkeypatch.setattr(
        tools_mod,
        "execute_tool",
        lambda name, args, workdir: tools_mod.ToolResult(output="stubbed"),
    )

    rt = get_runtime("mistral_sdk")
    result = rt.execute("loop forever", workdir="/tmp")

    assert result.exit_reason == "iteration_limit"
    assert result.tool_count == MAX_TURNS
    assert fake_client.chat.stream.call_count == MAX_TURNS
    # User-visible message should reflect the bounded-loop exit.
    assert "turn limit" in result.text.lower()


@pytest.mark.parametrize(
    "exc_kind, expected_exit_reason, expected_text_substring",
    [
        ("sdkerror_429", "rate_limited", "rate-limited"),
        ("sdkerror_401", "auth_error", "authentication failed"),
        ("sdkerror_403", "auth_error", "authentication failed"),
        ("sdkerror_500", "server_error", "500"),
        ("sdkerror_400", "api_error", "400"),
        ("httpx_timeout", "timeout", "timed out"),
        ("mistralerror_validation", "api_error", "mistral sdk error"),
        ("generic_runtime_error", "crashed", "unexpected"),
    ],
)
def test_mistral_sdk_classifies_api_open_error_by_exception_type(
    monkeypatch,
    exc_kind,
    expected_exit_reason,
    expected_text_substring,
):
    """When chat.stream raises a typed Mistral SDK or httpx exception, the
    runtime classifies it by exception class plus status code (not by
    string-matching the message) and returns the matching exit_reason with
    status-code-aware operator text.

    Mistral's exception model is structurally different from Groq's: instead
    of per-status-code exception classes, Mistral funnels HTTP errors through
    SDKError and exposes .status_code for branching. We verify both the
    SDKError status-code dispatch and the sibling httpx.TimeoutException +
    MistralError + catch-all paths.
    """
    import httpx

    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "msk_test")

    fake_client = MagicMock()
    # Install the stubs first so the exception classes used by the factories
    # below are the same class identity that the runtime's typed catches will
    # resolve to.
    stubs = _install_fake_mistral(monkeypatch, fake_client)

    factories = {
        "sdkerror_429": lambda: stubs.SDKError("rate limited", status_code=429),
        "sdkerror_401": lambda: stubs.SDKError("invalid api key", status_code=401),
        "sdkerror_403": lambda: stubs.SDKError("forbidden", status_code=403),
        "sdkerror_500": lambda: stubs.SDKError("server failed", status_code=500),
        "sdkerror_400": lambda: stubs.SDKError("bad request", status_code=400),
        "httpx_timeout": lambda: httpx.TimeoutException("Connection timed out"),
        "mistralerror_validation": lambda: stubs.MistralError("validation failed"),
        "generic_runtime_error": lambda: RuntimeError("network adapter exploded"),
    }
    fake_client.chat.stream.side_effect = factories[exc_kind]()

    rt = get_runtime("mistral_sdk")
    result = rt.execute("test prompt", workdir="/tmp")

    assert result.exit_reason == expected_exit_reason
    assert expected_text_substring in result.text.lower()


def test_mistral_sdk_marks_truncated_tool_output(monkeypatch):
    """Tool output exceeding TOOL_OUTPUT_CAP should be capped AND get a
    visible truncation marker in the role=tool history message, so the
    model can tell that content was clipped rather than fully delivered."""
    import tools as tools_mod

    from ax_cli.runtimes.hermes.runtimes import get_runtime
    from ax_cli.runtimes.hermes.runtimes.mistral_sdk import TOOL_OUTPUT_CAP

    monkeypatch.setenv("MISTRAL_API_KEY", "msk_test")

    turn1 = iter(
        [
            _fake_chunk_with_tool_calls(
                [
                    _fake_tool_call_delta(0, call_id="call_1", name="read_file", arguments='{"path":"/big"}'),
                ]
            ),
        ]
    )
    turn2 = iter([_fake_chunk("done")])

    fake_client = MagicMock()
    fake_client.chat.stream.side_effect = [turn1, turn2]
    _install_fake_mistral(monkeypatch, fake_client)

    oversize_output = "A" * (TOOL_OUTPUT_CAP + 500)
    monkeypatch.setattr(
        tools_mod,
        "execute_tool",
        lambda name, args, workdir: tools_mod.ToolResult(output=oversize_output),
    )

    rt = get_runtime("mistral_sdk")
    result = rt.execute("read it", workdir="/tmp")

    tool_msgs = [h for h in result.history if h.get("role") == "tool"]
    assert tool_msgs, "Expected at least one role=tool history entry"
    tool_content = tool_msgs[0]["content"]
    assert len(tool_content) <= TOOL_OUTPUT_CAP + 32
    assert "[output truncated]" in tool_content
    assert tool_content.startswith("A" * 100)


def test_mistral_sdk_does_not_mark_untruncated_tool_output(monkeypatch):
    """Tool output that fits within TOOL_OUTPUT_CAP should NOT get the
    truncation marker. Negative case complement to the truncation test."""
    import tools as tools_mod

    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("MISTRAL_API_KEY", "msk_test")

    turn1 = iter(
        [
            _fake_chunk_with_tool_calls(
                [
                    _fake_tool_call_delta(0, call_id="call_1", name="read_file", arguments='{"path":"/tiny"}'),
                ]
            ),
        ]
    )
    turn2 = iter([_fake_chunk("done")])

    fake_client = MagicMock()
    fake_client.chat.stream.side_effect = [turn1, turn2]
    _install_fake_mistral(monkeypatch, fake_client)

    monkeypatch.setattr(
        tools_mod,
        "execute_tool",
        lambda name, args, workdir: tools_mod.ToolResult(output="small output"),
    )

    rt = get_runtime("mistral_sdk")
    result = rt.execute("read it", workdir="/tmp")

    tool_msgs = [h for h in result.history if h.get("role") == "tool"]
    assert tool_msgs
    assert tool_msgs[0]["content"] == "small output"
    assert "[output truncated]" not in tool_msgs[0]["content"]


def test_mistral_sdk_per_tool_deadline_aborts_remaining_tools(monkeypatch):
    """If wall-clock passes the deadline between tool dispatches, the runtime
    should stop before executing the next tool and return exit_reason='timeout'.
    Prevents one slow tool from cascading the budget overrun across the rest."""
    from itertools import chain, repeat

    import tools as tools_mod

    from ax_cli.runtimes.hermes.runtimes import get_runtime
    from ax_cli.runtimes.hermes.runtimes import mistral_sdk as mistral_mod

    monkeypatch.setenv("MISTRAL_API_KEY", "gsk_test")

    # Fake clock: start=0, top-of-loop=1, tool 1=5 (within 30s budget),
    # tool 2=35 (past deadline → triggers per-tool timeout return).
    clock = chain([0.0, 1.0, 5.0, 35.0], repeat(35.0))
    monkeypatch.setattr(mistral_mod.time, "time", lambda: next(clock))

    # Single turn with two tool calls. Runtime should execute the first but
    # bail before the second.
    turn1 = iter(
        [
            _fake_chunk_with_tool_calls(
                [
                    _fake_tool_call_delta(0, call_id="call_1", name="bash", arguments='{"command":"ls"}'),
                    _fake_tool_call_delta(1, call_id="call_2", name="bash", arguments='{"command":"pwd"}'),
                ]
            ),
        ]
    )

    fake_client = MagicMock()
    fake_client.chat.stream.return_value = turn1
    _install_fake_mistral(monkeypatch, fake_client)

    executed: list[str] = []

    def recording_execute_tool(name, args, workdir):
        executed.append(args.get("command", name))
        return tools_mod.ToolResult(output="stubbed")

    monkeypatch.setattr(tools_mod, "execute_tool", recording_execute_tool)

    rt = get_runtime("mistral_sdk")
    result = rt.execute("run two tools", workdir="/tmp", timeout=30)

    # Only the first tool should have run; second was blocked by deadline check.
    assert executed == ["ls"]
    assert result.exit_reason == "timeout"
    assert result.tool_count == 1


# ── Real-package integration tests ────────────────────────────────────────
#
# The unit tests above stub `mistralai.client.sdk` and `mistralai.client.errors`
# via sys.modules, so they would keep passing even if Mistral renamed or
# reshuffled those internal paths in a future release. The runtime relies on
# those exact paths because mistralai 2.x's top-level `__init__.py` does not
# re-export Mistral — see the import block in the runtime module's `execute`.
#
# These tests import the real installed package and assert the layout matches
# what the runtime expects. Skipped when `mistralai` is not installed (bare
# `pip install .` CI); run when `pip install '.[mistral]'` has pulled the SDK.


def test_mistralai_real_package_exposes_runtime_import_paths():
    """The runtime's typed imports must resolve against the installed package."""
    pytest.importorskip("mistralai")
    from mistralai.client.errors import MistralError, SDKError
    from mistralai.client.sdk import Mistral

    assert isinstance(Mistral, type)
    assert issubclass(MistralError, Exception)
    # Catch order in the runtime depends on SDKError being a subclass of
    # MistralError, so the SDKError arm fires before the MistralError arm.
    assert issubclass(SDKError, MistralError)


def test_mistralai_real_package_chat_stream_accepts_runtime_kwargs():
    """The kwargs the runtime passes to chat.stream must exist in the SDK signature."""
    import inspect

    pytest.importorskip("mistralai")
    from mistralai.client.sdk import Mistral

    client = Mistral(api_key="not-a-real-key")
    params = inspect.signature(client.chat.stream).parameters
    # `timeout_ms` is the load-bearing one: Speakeasy SDKs take milliseconds,
    # not OpenAI/Groq's seconds. If Mistral renames it back to `timeout`, the
    # runtime's `int(remaining * 1000)` conversion becomes wrong and this test
    # surfaces the drift.
    for kw in ("model", "messages", "tools", "tool_choice", "timeout_ms"):
        assert kw in params, f"mistralai chat.stream is missing expected kwarg: {kw}"
