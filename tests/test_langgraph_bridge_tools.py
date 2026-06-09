"""Tests for the LangGraph bridge multi-node + security extensions.

Sprint 04 task #976. Covers Sub-A (the two-node llm_call <-> tool_node
agentic loop replacing the original one-node graph when conditions are
met) and Sub-B (the wrap_tool_call security middleware that ports
ax_cli/runtimes/hermes/tools/_check_* path/command guards onto
LangGraph's first-class ToolNode interception API, per PM artifact
#183 LangGraph survey Q3).

LLM responses are constructed as fake LangChain AIMessage objects so
the suite runs offline without GROQ_API_KEY or any real network call.
Where ToolNode itself is exercised, the real langgraph.prebuilt.ToolNode
is used (this is the load-bearing assertion in the design: that
wrap_tool_call actually intercepts as documented).

Test coverage parallels the 10 scenarios called out in the
implementation plan (c:/tmp/sprint-04-976-implementation-plan.md):

  1. _max_iterations clamps to floor/ceiling
  2. _tools_disabled env-var parsing
  3. _strict_security env-var parsing
  4. _default_tools returns the three expected tools with correct names
  5. Security wrapper passes through allowed read paths
  6. Security wrapper rejects write_file outside workdir
  7. Security wrapper rejects bash with dangerous patterns
  8. Security wrapper default-allows unrecognized tool names
  9. Security wrapper strict-mode rejects unrecognized tool names
 10. ToolNode + wrap_tool_call integration: tool dispatch routes through
     the security wrapper end-to-end

Mirrors the conventions in tests/test_gemini_sdk_runtime.py and
tests/test_groq_sdk_runtime.py.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# langgraph + langchain_core need to be importable for the multi-node tests
# below. If the suite is running in an environment without them (e.g. a
# minimal CI matrix), skip the whole module rather than reporting a hard
# failure.
pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

# Bring the bridge module into namespace. The example lives outside ax_cli/
# so we extend sys.path the same way Avrohom's existing tooling does.
_EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "gateway_langgraph",
)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

import langgraph_bridge  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────────────
# _reload_bridge() lived here historically but was moved to
# tests/test_langgraph_bridge_dotenv.py alongside its only caller
# (TestLoadDotenvIntoEnviron). The langgraph-gated tests below do not
# need it.


# ── 1. _max_iterations clamps to floor/ceiling ────────────────────────────


def test_max_iterations_default(monkeypatch):
    monkeypatch.delenv("AX_BRIDGE_MAX_ITERATIONS", raising=False)
    assert langgraph_bridge._max_iterations() == langgraph_bridge.DEFAULT_MAX_ITERATIONS


def test_max_iterations_honors_env(monkeypatch):
    monkeypatch.setenv("AX_BRIDGE_MAX_ITERATIONS", "45")
    assert langgraph_bridge._max_iterations() == 45


def test_max_iterations_clamps_high(monkeypatch):
    monkeypatch.setenv("AX_BRIDGE_MAX_ITERATIONS", "5000")
    assert langgraph_bridge._max_iterations() == langgraph_bridge.MAX_ITERATIONS_CEILING


def test_max_iterations_clamps_low(monkeypatch):
    monkeypatch.setenv("AX_BRIDGE_MAX_ITERATIONS", "0")
    assert langgraph_bridge._max_iterations() == langgraph_bridge.MAX_ITERATIONS_FLOOR


def test_max_iterations_invalid_value_falls_back(monkeypatch):
    monkeypatch.setenv("AX_BRIDGE_MAX_ITERATIONS", "not-a-number")
    assert langgraph_bridge._max_iterations() == langgraph_bridge.DEFAULT_MAX_ITERATIONS


# ── 2 + 3. Env-var parsing for tools-disabled and strict-security ────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("nope", False),
    ],
)
def test_tools_disabled_env(monkeypatch, value, expected):
    monkeypatch.setenv("AX_BRIDGE_TOOLS_DISABLED", value)
    assert langgraph_bridge._tools_disabled() is expected


def test_tools_disabled_unset(monkeypatch):
    monkeypatch.delenv("AX_BRIDGE_TOOLS_DISABLED", raising=False)
    assert langgraph_bridge._tools_disabled() is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_strict_security_env(monkeypatch, value, expected):
    monkeypatch.setenv("AX_BRIDGE_STRICT_SECURITY", value)
    assert langgraph_bridge._strict_security() is expected


# ── 4. _default_tools returns three named tools ───────────────────────────


def test_default_tools_returns_three_named_tools():
    tools = langgraph_bridge._default_tools()
    assert len(tools) == 3
    names = [t.name for t in tools]
    assert sorted(names) == ["echo", "list_dir", "read_file"]


def test_echo_tool_returns_echoed_message():
    tools = langgraph_bridge._default_tools()
    echo = next(t for t in tools if t.name == "echo")
    result = echo.invoke({"message": "hello world"})
    assert result == "echoed: hello world"


def test_list_dir_tool_lists_a_real_directory():
    """list_dir on a tempdir we control should return its entries."""
    tools = langgraph_bridge._default_tools()
    list_dir = next(t for t in tools if t.name == "list_dir")
    with tempfile.TemporaryDirectory() as td:
        # Create a couple of files so the listing has content
        for name in ("alpha.txt", "beta.txt"):
            open(os.path.join(td, name), "w").close()
        result = list_dir.invoke({"path": td})
    assert "alpha.txt" in result
    assert "beta.txt" in result


# ── 5-9. Security wrapper behavior ────────────────────────────────────────
#
# These tests call _make_security_wrap directly with a fake `execute`
# callable so we can observe whether the wrapper passed through to the
# underlying tool or returned a denial ToolMessage.


class _FakeToolCallRequest:
    """Duck-typed ToolCallRequest for testing the security wrapper.

    The real ToolCallRequest dataclass is internal to langgraph; we don't
    need the full thing, just .tool_call lookup. The wrapper code only
    reads request.tool_call dict fields.
    """

    def __init__(self, name: str, args: dict, call_id: str = "test-call-1"):
        self.tool_call = {"name": name, "args": args, "id": call_id}


def _execute_marker(request):
    """Sentinel `execute` callable. If the wrapper calls through to execute,
    we get back this string; if it short-circuits with a ToolMessage, we don't.
    """
    return "EXECUTED"


def test_security_wrapper_passes_through_allowed_read(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    # tmp_path is not under any BLOCKED_READ_PATTERNS prefix
    req = _FakeToolCallRequest("read_file", {"path": str(tmp_path / "foo.txt")})
    result = wrap(req, _execute_marker)
    assert result == "EXECUTED"


def test_security_wrapper_rejects_write_outside_workdir(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    # Writing to /etc/ is outside the agent's workdir and not in any of the
    # allowed prefixes (workdir, /tmp, agent worktrees).
    req = _FakeToolCallRequest("write_file", {"path": "/etc/something.txt"})
    result = wrap(req, _execute_marker)
    # Denial: result is a ToolMessage, not "EXECUTED"
    assert result != "EXECUTED"
    # The ToolMessage carries an error JSON blob in its content field
    content = getattr(result, "content", "")
    assert "Write denied" in content or "error" in content


def test_security_wrapper_rejects_bash_dangerous_pattern(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    # "rm -rf /" is in the bash blocklist
    req = _FakeToolCallRequest("bash", {"command": "rm -rf /"})
    result = wrap(req, _execute_marker)
    assert result != "EXECUTED"
    content = getattr(result, "content", "")
    assert "blocked" in content.lower() or "error" in content.lower()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Hermes BLOCKED_READ_PATTERNS use Unix-style /.ssh/ separators; "
    "os.path.realpath on Windows converts to backslash-separated paths "
    "(e.g. C:\\home\\user\\.ssh\\id_rsa) which the substring check misses. "
    "This is a Hermes-side limitation, not a bridge bug. On Linux/macOS "
    "production hosts (the actual target platform) the test passes. "
    "Tracked as a Hermes vendor follow-up.",
)
def test_security_wrapper_rejects_read_of_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    # ~/.ssh/id_rsa contains the blocked /.ssh/ pattern
    req = _FakeToolCallRequest("read_file", {"path": "/home/user/.ssh/id_rsa"})
    result = wrap(req, _execute_marker)
    assert result != "EXECUTED"
    content = getattr(result, "content", "")
    assert "Access denied" in content or "blocked" in content.lower()


def test_security_wrapper_default_allows_unknown_tool(monkeypatch, tmp_path):
    """Without strict mode, unrecognized tool names pass through."""
    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    req = _FakeToolCallRequest("some_future_tool", {"some_arg": "value"})
    result = wrap(req, _execute_marker)
    assert result == "EXECUTED"


def test_security_wrapper_strict_rejects_unknown_tool(monkeypatch, tmp_path):
    """With AX_BRIDGE_STRICT_SECURITY=1, unrecognized tool names get rejected."""
    monkeypatch.setenv("AX_BRIDGE_STRICT_SECURITY", "1")
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    req = _FakeToolCallRequest("some_future_tool", {"some_arg": "value"})
    result = wrap(req, _execute_marker)
    assert result != "EXECUTED"
    content = getattr(result, "content", "")
    assert "unrecognized" in content.lower() or "strict" in content.lower()


def test_security_wrapper_allows_echo_tool_with_args(monkeypatch, tmp_path):
    """The echo tool is unrecognized to the security dispatcher (no
    filesystem/command interaction) so it falls into the default-allow path.
    The wrapper should not reject it.
    """
    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)
    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))
    req = _FakeToolCallRequest("echo", {"message": "hi"})
    result = wrap(req, _execute_marker)
    assert result == "EXECUTED"


def test_security_wrapper_degraded_emits_status_error_event_and_stderr(monkeypatch, tmp_path, capsys):
    """#111: when ax_cli.runtimes.hermes.tools can't be imported, the wrapper
    degrades to a permissive passthrough. That downgrade must be loud, not
    quiet — both ``ax gateway status`` (via the kind:status,status:error
    event) and an operator running the bridge directly (via stderr) need to
    see it. Pre-fix it emitted a single kind:activity event and nothing on
    stderr; an IL2 operator could miss the loss of tool sandboxing entirely.
    """
    import sys as _sys

    # Force the in-function import to fail.
    monkeypatch.setitem(_sys.modules, "ax_cli.runtimes.hermes.tools", None)
    captured: list[dict] = []
    monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: captured.append(payload))

    wrap = langgraph_bridge._make_security_wrap(str(tmp_path))

    # Status-event side: surfaces in `ax gateway status`.
    status_errors = [e for e in captured if e.get("kind") == "status" and e.get("status") == "error"]
    assert len(status_errors) == 1, f"expected one status:error event, got {captured!r}"
    assert "security wrapper degraded" in str(status_errors[0].get("error_message") or "")
    # Stderr side: surfaces in the operator's terminal.
    err = capsys.readouterr().err
    assert "WARNING" in err and "security wrapper degraded" in err

    # Wrapper still functions (permissive passthrough — the bridge stays
    # usable for non-security demos, just unsandboxed).
    req = _FakeToolCallRequest("bash", {"command": "rm -rf /"})
    result = wrap(req, _execute_marker)
    assert result == "EXECUTED"


# ── 10. ToolNode + wrap_tool_call integration ─────────────────────────────


def _build_toolnode_graph(tools: list, workdir: str):
    """Build a minimal compiled graph wrapping a ToolNode for integration testing.

    LangGraph's ToolNode cannot be invoked standalone — it requires a runtime
    config injected via the graph runner. The minimal valid host is a graph
    with START -> tool_node -> END. That's what we build here.
    """
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    tool_node = ToolNode(
        tools,
        wrap_tool_call=langgraph_bridge._make_security_wrap(workdir),
    )
    graph = StateGraph(MessagesState)
    graph.add_node("tool_node", tool_node)
    graph.add_edge(START, "tool_node")
    graph.add_edge("tool_node", END)
    return graph.compile()


def test_toolnode_with_security_wrap_intercepts_blocked_write(monkeypatch, tmp_path):
    """End-to-end: build a real ToolNode with the bridge's security wrapper
    installed, dispatch a write_file tool call to a path outside the workdir,
    confirm the ToolNode returns a ToolMessage carrying the rejection.

    This is the load-bearing integration test: it verifies that the
    wrap_tool_call API actually intercepts as documented (the assumption
    yesterday's LangGraph survey Q3 made on doc-level evidence; this test
    confirms it on code-level evidence).
    """
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)

    # A write_file tool wired with a sentinel that fires only if the wrap
    # does NOT short-circuit. The test fails if this sentinel is reached.
    sentinel_fired = {"value": False}

    @tool
    def write_file(path: str, content: str) -> str:
        """Writes `content` to `path`."""
        sentinel_fired["value"] = True
        return f"wrote {len(content)} bytes to {path}"

    graph = _build_toolnode_graph([write_file], str(tmp_path))

    # Simulate the LLM returning a tool call requesting write to /etc/
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"path": "/etc/something.txt", "content": "x"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )
    result_state = graph.invoke({"messages": [ai_msg]})

    # The graph state should contain the original AIMessage plus a ToolMessage
    # carrying the rejection error (not a success message).
    new_messages = result_state["messages"]
    # Find the ToolMessage (last message in the state)
    rejection = new_messages[-1]
    content = getattr(rejection, "content", "")
    assert "Write denied" in content or "error" in content.lower()
    # Critical: the underlying tool function must NOT have run
    assert sentinel_fired["value"] is False, "Security wrapper failed to intercept: write_file actually executed"


def test_toolnode_with_security_wrap_passes_through_allowed_call(monkeypatch, tmp_path):
    """Mirror of the previous test for the happy path: an allowed tool call
    DOES reach the underlying tool function.
    """
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool

    monkeypatch.delenv("AX_BRIDGE_STRICT_SECURITY", raising=False)

    sentinel_fired = {"value": False}

    @tool
    def echo(message: str) -> str:
        """Echo the message back."""
        sentinel_fired["value"] = True
        return f"echoed: {message}"

    graph = _build_toolnode_graph([echo], str(tmp_path))

    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "echo",
                "args": {"message": "hello"},
                "id": "call-2",
                "type": "tool_call",
            }
        ],
    )
    result_state = graph.invoke({"messages": [ai_msg]})

    new_messages = result_state["messages"]
    response = new_messages[-1]
    content = getattr(response, "content", "")
    assert "echoed: hello" in content
    assert sentinel_fired["value"] is True


# ── Resolution helpers (small but worth covering) ─────────────────────────


def test_resolve_workdir_uses_explicit_env(monkeypatch):
    monkeypatch.setenv("AX_GATEWAY_WORKDIR", "/some/specific/path")
    assert langgraph_bridge._resolve_workdir() == "/some/specific/path"


def test_resolve_workdir_falls_back_to_cwd(monkeypatch):
    monkeypatch.delenv("AX_GATEWAY_WORKDIR", raising=False)
    assert langgraph_bridge._resolve_workdir() == os.getcwd()


# ── Regression: multi-node graph compiles cleanly ────────────────────────
#
# This test reproduces the failure mode caught during live testing on the VM
# 2026-05-23: `NameError: name 'MessagesState' is not defined` raised at
# graph-compile time when LangGraph's StateGraph used typing.get_type_hints
# to resolve annotations on the nested closure functions. The fix dropped
# the type annotations from the closures (the graph already knows its state
# shape from StateGraph(MessagesState)).
#
# We do not exercise the LLM call itself (would need GROQ_API_KEY); we
# stop just short of `agent.invoke` by mocking _build_groq_chat_model to
# return a stub model. The point is to verify the StateGraph compiles and
# the closures wire up without NameError.


def test_multi_node_graph_compiles_without_nameerror(monkeypatch, tmp_path):
    """Regression test for the live-test failure: NameError on MessagesState.

    The historic bug: nested closures inside _run_graph_with_tools were
    annotated `state: MessagesState`. With `from __future__ import annotations`
    those annotations are strings, but LangGraph resolves them at compile
    time via typing.get_type_hints() against the function's __globals__
    (module globals), which don't contain MessagesState. NameError at
    graph.compile().

    Reproduction: build the graph end-to-end (compile, but don't invoke),
    confirm no NameError. If this regresses (someone re-annotates the
    closures), the test catches it.
    """
    from langchain_core.messages import AIMessage

    monkeypatch.setenv("AX_BRIDGE_MAX_ITERATIONS", "5")

    class _StubChatModel:
        """Stand-in for ChatGroq.bind_tools(...) that returns a final
        assistant message immediately (no tool calls), so the graph
        terminates after one iteration.
        """

        def invoke(self, messages, *args, **kwargs):
            return AIMessage(content="stub final answer", tool_calls=[])

    def _stub_build_groq(model_name, tools):
        return _StubChatModel()

    monkeypatch.setattr(langgraph_bridge, "_build_groq_chat_model", _stub_build_groq)

    tools = langgraph_bridge._default_tools()
    # This is the call that previously raised NameError at graph.compile().
    result = langgraph_bridge._run_graph_with_tools(
        prompt="test prompt",
        model_name="stub-model",
        tools=tools,
        workdir=str(tmp_path),
    )
    assert result.used_llm is True
    assert "stub final answer" in result.reply
