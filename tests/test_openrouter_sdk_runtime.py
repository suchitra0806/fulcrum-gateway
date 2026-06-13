"""Tests for the OpenRouter SDK runtime adapter.

The openai package is mocked via sys.modules so these tests run offline
and do not consume API credits. Coverage spans registration discovery,
the missing-API-key path, the happy streaming path, system prompt
threading, typed exception classification (RateLimitError 429,
AuthenticationError 401, PermissionDeniedError 403, APITimeoutError,
InternalServerError 5xx, APIStatusError other 4xx, unexpected), tool use
round trips, partial-failure handling when the stream raises
mid-response, iteration-limit and sentinel-budget timeout exits, the
tool-output truncation cap, the OpenAI(api_key=..., base_url=...)
client construction with the X-Title attribution header, and the
meta-provider model namespace (provider/model).
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest  # noqa: F401

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

from ax_cli.runtimes.hermes.runtimes import openrouter_sdk  # noqa: F401, E402


def _delta(**fields):
    return types.SimpleNamespace(**fields)


def _chunk(*, content=None, tool_calls=None):
    """Build a streaming chunk with `.choices[0].delta` shape that mirrors
    openai SDK's ChatCompletionChunk surface."""
    delta = _delta(content=content, tool_calls=tool_calls)
    choice = _delta(delta=delta, finish_reason=None, index=0)
    return _delta(choices=[choice])


def _tool_call_delta(*, index, tc_id=None, name=None, arguments=None):
    function = _delta(name=name, arguments=arguments) if (name or arguments) else None
    return _delta(index=index, id=tc_id, function=function)


def _install_fake_openai(monkeypatch, fake_client):
    """Swap `openai` in sys.modules with a stub module so the runtime's
    `from openai import OpenAI, RateLimitError, ...` resolves cleanly
    without requiring the real `openai` package.

    The stub exception classes mirror the slice of the real openai SDK's
    exception hierarchy the runtime catches by name. Class identity
    matches between the runtime's `except RateLimitError as e:` and the
    instance the mock raises, because both resolve through this stubbed
    sys.modules entry.
    """

    class _OpenAIError(Exception):
        pass

    class APIStatusError(_OpenAIError):
        def __init__(self, message="", *, status_code=0, **_kwargs):
            super().__init__(message)
            self.message = message
            self.status_code = status_code

    class RateLimitError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class PermissionDeniedError(APIStatusError):
        pass

    class InternalServerError(APIStatusError):
        pass

    class APITimeoutError(_OpenAIError):
        def __init__(self, *_args, **_kwargs):
            super().__init__("API timeout")

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = MagicMock(return_value=fake_client)
    fake_module.RateLimitError = RateLimitError
    fake_module.AuthenticationError = AuthenticationError
    fake_module.PermissionDeniedError = PermissionDeniedError
    fake_module.APITimeoutError = APITimeoutError
    fake_module.InternalServerError = InternalServerError
    fake_module.APIStatusError = APIStatusError
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    return fake_module


class _RecordingCallback:
    def __init__(self):
        self.deltas: list[str] = []
        self.complete: str | None = None
        self.statuses: list[str] = []
        self.tool_starts: list[tuple] = []
        self.tool_ends: list[tuple] = []

    def on_text_delta(self, text: str) -> None:
        self.deltas.append(text)

    def on_text_complete(self, text: str) -> None:
        self.complete = text

    def on_tool_start(self, name, summary) -> None:
        self.tool_starts.append((name, summary))

    def on_tool_end(self, name, output) -> None:
        self.tool_ends.append((name, output))

    def on_status(self, status: str) -> None:
        self.statuses.append(status)


# Tests


def test_openrouter_sdk_registers_under_expected_name():
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    rt = get_runtime("openrouter_sdk")
    assert type(rt).__name__ == "OpenRouterSDKRuntime"
    assert rt.name == "openrouter_sdk"


def test_openrouter_sdk_returns_crashed_when_api_key_missing(monkeypatch):
    """No OPENROUTER_API_KEY in env should short-circuit before any openai import."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hello", workdir="/tmp")

    assert result.exit_reason == "crashed"
    assert "OPENROUTER_API_KEY" in result.text
    assert result.elapsed_seconds == 0


def test_openrouter_sdk_returns_crashed_when_openai_package_missing(monkeypatch):
    """If openai package is missing, return crashed with install hint."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delitem(sys.modules, "openai", raising=False)

    class _BrokenOpenAI(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError(f"openai.{name} not available in stub")

    broken = _BrokenOpenAI("openai")
    monkeypatch.setitem(sys.modules, "openai", broken)

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hello", workdir="/tmp")

    assert result.exit_reason == "crashed"
    assert "openai" in result.text.lower()
    assert "pip install" in result.text


def test_openrouter_sdk_constructs_openai_client_with_openrouter_base_url(monkeypatch):
    """The OpenAI client is constructed against openrouter.ai/api/v1, never
    the default OpenAI endpoint. Critical config-diff guard."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter([_chunk(content="ok")])
    fake_module = _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    rt.execute("hi", workdir="/tmp")

    call = fake_module.OpenAI.call_args
    assert call.kwargs["api_key"] == "test-key"
    assert call.kwargs["base_url"] == "https://openrouter.ai/api/v1"


def test_openrouter_sdk_sends_x_title_attribution_header(monkeypatch):
    """OpenRouter recommends an X-Title header for operator dashboard
    attribution. The runtime sends X-Title=ax-gateway via default_headers."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter([_chunk(content="ok")])
    fake_module = _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    rt.execute("hi", workdir="/tmp")

    call = fake_module.OpenAI.call_args
    assert "default_headers" in call.kwargs
    assert call.kwargs["default_headers"].get("X-Title") == "ax-gateway"


def test_openrouter_sdk_streams_text_and_returns_done(monkeypatch):
    """Happy path: text-only response yields done with assembled text."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter(
        [
            _chunk(content="Hello "),
            _chunk(content="world."),
        ]
    )
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    cb = _RecordingCallback()
    result = rt.execute("Say hello.", workdir="/tmp", stream_cb=cb)

    assert cb.deltas == []
    assert cb.complete == "Hello world."
    assert result.exit_reason == "done"
    assert result.text == "Hello world."


def test_openrouter_sdk_threads_system_prompt(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter([_chunk(content="ok")])
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    rt.execute("ping", workdir="/tmp", system_prompt="You are a terse assistant.")

    call = fake_client.chat.completions.create.call_args
    messages = call.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "You are a terse assistant."}


def test_openrouter_sdk_executes_tool_and_completes(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = [
        {"name": "echo", "description": "echo back", "parameters": {"type": "object"}}
    ]
    fake_tools_module.execute_tool = MagicMock(
        return_value=types.SimpleNamespace(output="echo: ping", is_error=False)
    )
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        iter(
            [
                _chunk(
                    tool_calls=[
                        _tool_call_delta(index=0, tc_id="call_01", name="echo"),
                    ]
                ),
                _chunk(
                    tool_calls=[
                        _tool_call_delta(index=0, arguments='{"text"'),
                    ]
                ),
                _chunk(
                    tool_calls=[
                        _tool_call_delta(index=0, arguments=': "ping"}'),
                    ]
                ),
            ]
        ),
        iter([_chunk(content="Result was echo: ping.")]),
    ]
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    cb = _RecordingCallback()
    result = rt.execute("Echo ping", workdir="/tmp", stream_cb=cb)

    assert result.exit_reason == "done"
    assert result.tool_count == 1
    assert result.text == "Result was echo: ping."
    assert cb.tool_starts == [("echo", "echo")]


def test_openrouter_sdk_returns_rate_limited_on_RateLimitError(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_module = _install_fake_openai(monkeypatch, MagicMock())
    fake_client = fake_module.OpenAI.return_value
    fake_client.chat.completions.create.side_effect = fake_module.RateLimitError(
        "slow down", status_code=429
    )

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "rate_limited"
    assert "429" in result.text


def test_openrouter_sdk_returns_auth_error_on_AuthenticationError(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "bad")
    fake_module = _install_fake_openai(monkeypatch, MagicMock())
    fake_client = fake_module.OpenAI.return_value
    fake_client.chat.completions.create.side_effect = fake_module.AuthenticationError(
        "bad key", status_code=401
    )

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "auth_error"
    assert "401" in result.text
    assert "OPENROUTER_API_KEY" in result.text


def test_openrouter_sdk_returns_auth_error_on_PermissionDeniedError(monkeypatch):
    """403 typically indicates the operator's key lacks permission for a
    specific upstream model. The error text names that case."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_module = _install_fake_openai(monkeypatch, MagicMock())
    fake_client = fake_module.OpenAI.return_value
    fake_client.chat.completions.create.side_effect = fake_module.PermissionDeniedError(
        "no perms", status_code=403
    )

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "auth_error"
    assert "403" in result.text
    assert "allowed-model permissions" in result.text


def test_openrouter_sdk_returns_timeout_on_APITimeoutError(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_module = _install_fake_openai(monkeypatch, MagicMock())
    fake_client = fake_module.OpenAI.return_value
    fake_client.chat.completions.create.side_effect = fake_module.APITimeoutError()

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "timeout"


def test_openrouter_sdk_returns_server_error_on_InternalServerError(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_module = _install_fake_openai(monkeypatch, MagicMock())
    fake_client = fake_module.OpenAI.return_value
    fake_client.chat.completions.create.side_effect = fake_module.InternalServerError(
        "upstream down", status_code=502
    )

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "server_error"
    # The error text names the upstream-provider angle since OpenRouter
    # is a meta-provider routing through other vendors.
    assert "Upstream provider may be down" in result.text or "502" in result.text


def test_openrouter_sdk_returns_api_error_on_other_APIStatusError(monkeypatch):
    """404 most commonly fires for a model name typo against the catalog."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_module = _install_fake_openai(monkeypatch, MagicMock())
    fake_client = fake_module.OpenAI.return_value
    fake_client.chat.completions.create.side_effect = fake_module.APIStatusError(
        "model not found", status_code=404
    )

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "api_error"
    assert "404" in result.text


def test_openrouter_sdk_returns_crashed_on_unexpected_exception(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _install_fake_openai(monkeypatch, MagicMock())
    fake_client = sys.modules["openai"].OpenAI.return_value
    fake_client.chat.completions.create.side_effect = RuntimeError("network glitch")

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "crashed"


def test_openrouter_sdk_returns_crashed_when_stream_raises_midway(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    def _exploding_stream():
        yield _chunk(content="Hello ")
        raise RuntimeError("connection reset")

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _exploding_stream()
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp")
    assert result.exit_reason == "crashed"


def test_openrouter_sdk_returns_iteration_limit_when_max_turns_exhausted(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = [
        {"name": "echo", "description": "echo", "parameters": {"type": "object"}}
    ]
    fake_tools_module.execute_tool = MagicMock(
        return_value=types.SimpleNamespace(output="ok", is_error=False)
    )
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    def _tool_only_stream():
        return iter(
            [
                _chunk(
                    tool_calls=[
                        _tool_call_delta(index=0, tc_id="call_x", name="echo"),
                        _tool_call_delta(index=0, arguments='{"text": "x"}'),
                    ]
                ),
            ]
        )

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _tool_only_stream() for _ in range(openrouter_sdk.MAX_TURNS + 2)
    ]
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("loop forever", workdir="/tmp")
    assert result.exit_reason == "iteration_limit"
    assert result.tool_count == openrouter_sdk.MAX_TURNS


def test_openrouter_sdk_returns_timeout_when_deadline_exceeded(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    _install_fake_openai(monkeypatch, MagicMock())

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("hi", workdir="/tmp", timeout=0)
    assert result.exit_reason == "timeout"


def test_openrouter_sdk_appends_truncation_marker_when_tool_output_exceeds_cap(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    big_output = "x" * (openrouter_sdk.TOOL_OUTPUT_CAP + 500)

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = [
        {"name": "read_file", "description": "read", "parameters": {"type": "object"}}
    ]
    fake_tools_module.execute_tool = MagicMock(
        return_value=types.SimpleNamespace(output=big_output, is_error=False)
    )
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        iter(
            [
                _chunk(
                    tool_calls=[
                        _tool_call_delta(index=0, tc_id="call_big", name="read_file"),
                        _tool_call_delta(index=0, arguments='{"path": "/etc/hosts"}'),
                    ]
                ),
            ]
        ),
        iter([_chunk(content="ok")]),
    ]
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    result = rt.execute("read it", workdir="/tmp")
    assert result.exit_reason == "done"
    tool_history = [h for h in result.history if h.get("role") == "tool"]
    assert tool_history[0]["content"].endswith("[output truncated]")


def test_openrouter_sdk_converts_tool_definitions_to_openai_compatible_shape(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = [
        {"name": "echo", "description": "echo back", "parameters": {"type": "object", "properties": {}}}
    ]
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter([_chunk(content="done")])
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    rt.execute("hi", workdir="/tmp")

    call = fake_client.chat.completions.create.call_args
    tools = call.kwargs["tools"]
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "echo"


def test_openrouter_sdk_uses_default_model_when_not_specified(monkeypatch):
    """Without an explicit model arg, the runtime uses DEFAULT_MODEL."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter([_chunk(content="ok")])
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    rt.execute("hi", workdir="/tmp")

    call = fake_client.chat.completions.create.call_args
    assert call.kwargs["model"] == openrouter_sdk.DEFAULT_MODEL


def test_openrouter_sdk_uses_operator_supplied_meta_provider_model(monkeypatch):
    """Explicit model arg overrides DEFAULT_MODEL. Critically, OpenRouter's
    meta-provider model namespace uses `provider/model` form, the runtime
    must pass it through to the API unchanged so OpenRouter routes correctly."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    fake_tools_module = types.ModuleType("tools")
    fake_tools_module.TOOL_DEFINITIONS = []
    fake_tools_module.execute_tool = MagicMock()
    monkeypatch.setitem(sys.modules, "tools", fake_tools_module)

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter([_chunk(content="ok")])
    _install_fake_openai(monkeypatch, fake_client)

    rt = get_runtime("openrouter_sdk")
    rt.execute("hi", workdir="/tmp", model="google/gemini-2.0-flash-exp")

    call = fake_client.chat.completions.create.call_args
    # Provider/model form must pass through untouched. Critical because
    # OpenRouter routes upstream based on the namespace prefix.
    assert call.kwargs["model"] == "google/gemini-2.0-flash-exp"
