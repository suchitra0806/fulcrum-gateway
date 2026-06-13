"""Regression: Pydantic AI template registers correctly and the bridge
emits the Gateway lifecycle contract.

A Gateway-managed Pydantic AI runtime should be declarable from the
dashboard / CLI like any other template, and the bridge subprocess
should emit AX_GATEWAY_EVENT lines that map to the three signals
operators rely on (online via heartbeat, accept-work via intake_model,
response-path via return_paths).

Mirrors test_gateway_autogen_template.py with the same shape, adapted
for the Pydantic AI bridge across all three execution tiers.

This file locks the contract for:
  - the template appears in agent_template_catalog with the right shape
  - the template appears in the default agent_template_list ordering
  - the bridge file exists at the path the template advertises
  - the bridge emits a "processing" event and a "completed" event
    around a stub-path prompt round trip
  - the bridge wires a Pydantic AI Agent to a Groq-backed
    OpenAIChatModel and runs run_stream() when GROQ_API_KEY is set,
    accumulating text deltas into the final reply and emitting
    throttled rolling-preview activity events during the call
  - AX_BRIDGE_SYSTEM_PROMPT threads into the Agent system_prompt
  - the bridge falls back to the stub agent path when GROQ_API_KEY is
    missing
  - the bridge falls back to the string template when pydantic-ai
    itself is missing
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

from ax_cli.gateway_runtime_types import (
    agent_template_catalog,
    agent_template_definition,
    agent_template_list,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO_ROOT / "examples" / "gateway_pydantic_ai" / "pydantic_ai_bridge.py"


def _install_fake_pydantic_ai(
    monkeypatch,
    run_stream_output="stub pydantic ai reply",
    stream_deltas=None,
):
    """Stub the pydantic_ai package and its OpenAI provider in sys.modules
    so the bridge's lazy imports resolve without requiring the real
    package.

    Implements the minimal surface the bridge uses. Agent constructor
    captures kwargs and exposes an async-context-manager `run_stream`
    method that yields a streaming-result object. The streaming-result
    object has `stream_text(delta=True)` returning an async generator
    of text deltas (from `stream_deltas`) plus `get_output()` returning
    `run_stream_output`. OpenAIChatModel and OpenAIProvider capture
    kwargs for assertion.
    """
    captured = {"agents": [], "models": [], "providers": [], "runs": []}

    class _OpenAIProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            captured["providers"].append(self)

    class _OpenAIChatModel:
        def __init__(self, model_name, *, provider, **kwargs):
            self.model_name = model_name
            self.provider = provider
            self.kwargs = kwargs
            captured["models"].append(self)

    class _StreamResult:
        def __init__(self, deltas, final_output):
            self._deltas = list(deltas)
            self._final_output = final_output

        async def stream_text(self, delta=True):
            for d in self._deltas:
                yield d

        async def get_output(self):
            return self._final_output

    class _RunStreamContext:
        def __init__(self, deltas, final_output):
            self._result = _StreamResult(deltas, final_output)

        async def __aenter__(self):
            return self._result

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Agent:
        def __init__(self, model, *, system_prompt=None, **kwargs):
            self.model = model
            self.system_prompt = system_prompt
            self.kwargs = kwargs
            captured["agents"].append(self)

        def run_stream(self, prompt):
            captured["runs"].append({"prompt": prompt})
            return _RunStreamContext(stream_deltas or [], run_stream_output)

    fake_pa = types.ModuleType("pydantic_ai")
    fake_pa.Agent = _Agent

    fake_pa_models = types.ModuleType("pydantic_ai.models")
    fake_pa_models_openai = types.ModuleType("pydantic_ai.models.openai")
    fake_pa_models_openai.OpenAIChatModel = _OpenAIChatModel

    fake_pa_providers = types.ModuleType("pydantic_ai.providers")
    fake_pa_providers_openai = types.ModuleType("pydantic_ai.providers.openai")
    fake_pa_providers_openai.OpenAIProvider = _OpenAIProvider

    monkeypatch.setitem(sys.modules, "pydantic_ai", fake_pa)
    monkeypatch.setitem(sys.modules, "pydantic_ai.models", fake_pa_models)
    monkeypatch.setitem(sys.modules, "pydantic_ai.models.openai", fake_pa_models_openai)
    monkeypatch.setitem(sys.modules, "pydantic_ai.providers", fake_pa_providers)
    monkeypatch.setitem(sys.modules, "pydantic_ai.providers.openai", fake_pa_providers_openai)

    return captured


# Template registration


def test_pydantic_ai_template_is_registered() -> None:
    catalog = agent_template_catalog()
    assert "pydantic_ai" in catalog, (
        "pydantic_ai template missing from agent_template_catalog. "
        "Should sit alongside langgraph / autogen / strands."
    )

    template = agent_template_definition("pydantic_ai")
    assert template["id"] == "pydantic_ai"
    assert template["runtime_type"] == "exec", (
        "Reuses the exec runtime adapter (same precedent as langgraph and autogen). "
        "A dedicated 'pydantic_ai' runtime_type is a follow-up."
    )
    assert template["intake_model"] == "launch_on_send"
    assert template["return_paths"] == ["inline_reply"]
    assert template["availability"] == "ready"
    assert template["launchable"] is True


def test_pydantic_ai_template_default_exec_command_points_at_bridge() -> None:
    template = agent_template_definition("pydantic_ai")
    defaults = template.get("defaults") or {}
    exec_command = str(defaults.get("exec_command") or "")
    assert "examples/gateway_pydantic_ai/pydantic_ai_bridge.py" in exec_command, (
        f"pydantic_ai template's default exec_command should run the bridge "
        f"at examples/gateway_pydantic_ai/pydantic_ai_bridge.py. Got: {exec_command!r}"
    )


def test_pydantic_ai_template_listed_in_default_ordering() -> None:
    listed_ids = [item["id"] for item in agent_template_list()]
    assert "pydantic_ai" in listed_ids, (
        "pydantic_ai template should appear in the default (non-advanced) "
        "template list so the dashboard's Add Agent modal can render it."
    )


def test_pydantic_ai_bridge_file_exists() -> None:
    assert BRIDGE_PATH.exists(), (
        f"pydantic_ai bridge file missing at {BRIDGE_PATH}. The default "
        "exec_command in the template registration points at it."
    )


# Stub path lifecycle events


def test_pydantic_ai_bridge_emits_lifecycle_events_in_stub_path(monkeypatch, capsys) -> None:
    """Run the bridge's main() inline on the STUB path (no GROQ_API_KEY)
    and confirm it emits processing and completed AX_GATEWAY_EVENT lines
    around the round trip."""
    _install_fake_pydantic_ai(monkeypatch)
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import pydantic_ai_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["pydantic_ai_bridge.py", "test prompt"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "pydantic-ai-test")

    rc = bridge.main()
    captured = capsys.readouterr()

    assert rc == 0, f"bridge main() returned {rc}; expected 0"

    event_lines = [line for line in captured.out.splitlines() if line.startswith(bridge.EVENT_PREFIX)]
    statuses = []
    completed_detail = None
    for line in event_lines:
        payload = json.loads(line[len(bridge.EVENT_PREFIX) :])
        if payload.get("kind") == "status":
            statuses.append(payload.get("status"))
            if payload.get("status") == "completed":
                completed_detail = payload.get("detail") or {}

    assert "processing" in statuses
    assert "completed" in statuses
    assert completed_detail is not None
    assert completed_detail.get("used_llm") is False, (
        f"stub path should report used_llm=False, got {completed_detail!r}"
    )

    reply_lines = [line for line in captured.out.splitlines() if line and not line.startswith(bridge.EVENT_PREFIX)]
    assert reply_lines, "bridge did not print a reply line on stdout"
    assert "test prompt" in reply_lines[-1], (
        f"stub reply should echo the prompt. last line: {reply_lines[-1]!r}"
    )


# Real LLM path


def test_pydantic_ai_bridge_calls_groq_llm_when_configured(monkeypatch, capsys) -> None:
    """When GROQ_API_KEY is set AND pydantic-ai is importable, the
    bridge should build an OpenAIChatModel pointed at Groq's
    OpenAI-compatible endpoint via OpenAIProvider, wire it into an
    Agent, and drive a single turn via run_stream() with the prompt.
    """
    captured = _install_fake_pydantic_ai(
        monkeypatch,
        run_stream_output="The speed of light is approximately 299,792 km/s.",
    )

    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import pydantic_ai_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("AX_BRIDGE_LLM_MODEL", "test-model-x")
    monkeypatch.setattr(sys, "argv", ["pydantic_ai_bridge.py", "what is the speed of light in km/s"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "pydantic-ai-test")

    rc = bridge.main()
    out = capsys.readouterr()

    assert rc == 0, f"bridge main() returned {rc}; expected 0"

    # Exactly one provider, one model, one agent, one run.
    assert len(captured["providers"]) == 1
    assert len(captured["models"]) == 1
    assert len(captured["agents"]) == 1
    assert len(captured["runs"]) == 1

    # Provider was wired with the Groq endpoint + API key.
    provider_kwargs = captured["providers"][0].kwargs
    assert provider_kwargs["api_key"] == "gsk_test", (
        "bridge should pass GROQ_API_KEY through to the OpenAIProvider"
    )
    assert provider_kwargs["base_url"] == "https://api.groq.com/openai/v1", (
        "bridge should point the OpenAIProvider at Groq's OpenAI-compatible endpoint, "
        "not the OpenAI default URL"
    )

    # Model was constructed with the configured model name + provider.
    model = captured["models"][0]
    assert model.model_name == "test-model-x", (
        f"bridge should forward AX_BRIDGE_LLM_MODEL to OpenAIChatModel. got {model.model_name!r}"
    )
    assert model.provider is captured["providers"][0], (
        "Model should be wired to the constructed Groq-pointed provider"
    )

    # Agent was wired with the model and a system_prompt that names the agent.
    agent_kwargs = captured["agents"][0]
    assert agent_kwargs.model is model, "Agent should be wired to the constructed model"
    assert "pydantic-ai-test" in (agent_kwargs.system_prompt or ""), (
        "Agent system_prompt should name the routed agent so the model knows who it is replying as"
    )

    # The prompt was forwarded via run_stream.
    assert captured["runs"][0]["prompt"] == "what is the speed of light in km/s"

    # Completion event reports used_llm=True (and back-compat stub=False).
    event_lines = [line for line in out.out.splitlines() if line.startswith(bridge.EVENT_PREFIX)]
    completed_detail = None
    for line in event_lines:
        payload = json.loads(line[len(bridge.EVENT_PREFIX) :])
        if payload.get("kind") == "status" and payload.get("status") == "completed":
            completed_detail = payload.get("detail") or {}
    assert completed_detail is not None
    assert completed_detail.get("used_llm") is True
    assert completed_detail.get("stub") is False

    # The run_stream final output (not a synthetic ack) lands on stdout.
    reply_lines = [line for line in out.out.splitlines() if line and not line.startswith(bridge.EVENT_PREFIX)]
    assert reply_lines, "bridge did not print a reply line on stdout"
    assert "299,792" in reply_lines[-1], (
        f"bridge reply should be the model's response, not a stub ack. last line: {reply_lines[-1]!r}"
    )


# Streaming activity events


def test_pydantic_ai_bridge_streams_activity_events_during_llm_call(monkeypatch, capsys) -> None:
    """The streaming path should emit a `processing` status when the
    first token arrives and at least one throttled `activity` event
    with a rolling preview. Locks in the chatty-observability contract
    that the langgraph and autogen bridges share. Without per-token
    signals the activity feed would go silent for the duration of the
    LLM call.

    Time is faked so the heartbeat fires deterministically without
    sleeping.
    """
    captured = _install_fake_pydantic_ai(
        monkeypatch,
        run_stream_output="Final answer: 299,792 km/s.",
        stream_deltas=[
            "Light ",
            "travels at ",
            "about ",
            "299,792 km/s.",
        ],
    )

    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import pydantic_ai_bridge as bridge
    finally:
        sys.path.pop(0)

    # Drive time.monotonic deterministically so each yielded delta
    # advances the clock past the ACTIVITY_HEARTBEAT_SECONDS threshold.
    fake_now = iter([0.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0])

    def _next_monotonic() -> float:
        try:
            return next(fake_now)
        except StopIteration:
            return 99.0

    monkeypatch.setattr(bridge.time, "monotonic", _next_monotonic)

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("AX_BRIDGE_LLM_MODEL", "test-model-x")
    monkeypatch.setattr(sys, "argv", ["pydantic_ai_bridge.py", "tell me about light"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "pydantic-ai-test")

    rc = bridge.main()
    out = capsys.readouterr()
    assert rc == 0, f"bridge main() returned {rc}; expected 0"

    event_lines = [line for line in out.out.splitlines() if line.startswith(bridge.EVENT_PREFIX)]
    processing_messages: list[str] = []
    streaming_activities: list[str] = []
    for line in event_lines:
        payload = json.loads(line[len(bridge.EVENT_PREFIX) :])
        if payload.get("kind") == "status" and payload.get("status") == "processing":
            processing_messages.append(payload.get("message", ""))
        elif payload.get("kind") == "activity":
            activity = payload.get("activity", "")
            # Filter for streaming-preview activities (carry the model name
            # plus a snippet of accumulated text). The non-streaming
            # "building Pydantic AI Agent" activity is filtered out.
            if "test-model-x" in activity:
                streaming_activities.append(activity)

    # Processing emitted when first chunk arrives, naming the responding vendor.
    assert any("Groq is responding" in msg for msg in processing_messages), (
        f"bridge should emit a 'Groq is responding' processing status "
        f"when the first delta arrives. got processing messages: {processing_messages!r}"
    )

    # At least one rolling-preview activity event with the accumulated text.
    assert streaming_activities, (
        "bridge should emit at least one throttled activity event with a "
        f"rolling preview during the LLM call. got event_lines: {event_lines!r}"
    )
    # Preview should reflect accumulated content as the stream progressed.
    assert any("Light" in act or "travels" in act for act in streaming_activities), (
        f"streaming activity events should carry a preview of the accumulated text. "
        f"got: {streaming_activities!r}"
    )

    # Sanity: agent was driven via the streaming run_stream, not a sync call.
    assert len(captured["runs"]) == 1


# Fallback paths


def test_pydantic_ai_bridge_falls_back_to_stub_when_no_groq_key(monkeypatch, capsys) -> None:
    """Without GROQ_API_KEY the bridge should land on the stub path and
    not attempt to construct any model client."""
    captured = _install_fake_pydantic_ai(monkeypatch)
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import pydantic_ai_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["pydantic_ai_bridge.py", "ping"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "pydantic-ai-test")

    rc = bridge.main()
    out = capsys.readouterr()
    assert rc == 0

    # No real LLM-path artifacts.
    assert len(captured["agents"]) == 0
    assert len(captured["models"]) == 0
    assert len(captured["providers"]) == 0
    assert len(captured["runs"]) == 0

    # Completion event reports used_llm=False.
    event_lines = [line for line in out.out.splitlines() if line.startswith(bridge.EVENT_PREFIX)]
    completed = next(
        (
            json.loads(line[len(bridge.EVENT_PREFIX) :])
            for line in event_lines
            if json.loads(line[len(bridge.EVENT_PREFIX) :]).get("status") == "completed"
        ),
        None,
    )
    assert completed is not None
    assert completed["detail"]["used_llm"] is False


def test_pydantic_ai_bridge_falls_back_to_string_when_package_missing(monkeypatch, capsys) -> None:
    """When pydantic_ai cannot be imported at all, the bridge should
    return a plain string template ack and complete cleanly. Tests the
    ImportError-on-pydantic_ai path."""
    # Pre-blank the modules so the bridge's lazy import of pydantic_ai fails.
    monkeypatch.delitem(sys.modules, "pydantic_ai", raising=False)

    class _BrokenPydanticAi(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError(f"pydantic_ai.{name} not available in stub")

    monkeypatch.setitem(sys.modules, "pydantic_ai", _BrokenPydanticAi("pydantic_ai"))

    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import pydantic_ai_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["pydantic_ai_bridge.py", "ping"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "pydantic-ai-test")

    rc = bridge.main()
    out = capsys.readouterr()
    assert rc == 0

    # Completion event reports used_llm=False.
    event_lines = [line for line in out.out.splitlines() if line.startswith(bridge.EVENT_PREFIX)]
    completed = next(
        (
            json.loads(line[len(bridge.EVENT_PREFIX) :])
            for line in event_lines
            if json.loads(line[len(bridge.EVENT_PREFIX) :]).get("status") == "completed"
        ),
        None,
    )
    assert completed is not None
    assert completed["detail"]["used_llm"] is False

    # Reply is the string-fallback ack form.
    reply_lines = [line for line in out.out.splitlines() if line and not line.startswith(bridge.EVENT_PREFIX)]
    assert reply_lines
    assert "stub ack" in reply_lines[-1].lower(), (
        f"expected string fallback ack on the package-missing path. got: {reply_lines[-1]!r}"
    )


# System prompt threading


def test_pydantic_ai_bridge_threads_ax_bridge_system_prompt(monkeypatch, capsys) -> None:
    """AX_BRIDGE_SYSTEM_PROMPT should be appended to the agent's
    system_prompt so operators can steer the agent's tone without
    editing the bridge."""
    captured = _install_fake_pydantic_ai(monkeypatch, run_stream_output="ok")
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import pydantic_ai_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("AX_BRIDGE_SYSTEM_PROMPT", "Be very terse.")
    monkeypatch.setattr(sys, "argv", ["pydantic_ai_bridge.py", "hi"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "pydantic-ai-test")

    rc = bridge.main()
    _ = capsys.readouterr()
    assert rc == 0

    assert len(captured["agents"]) == 1
    system_prompt = captured["agents"][0].system_prompt or ""
    assert "Be very terse." in system_prompt, (
        f"AX_BRIDGE_SYSTEM_PROMPT should land in the Agent system_prompt. got: {system_prompt!r}"
    )
