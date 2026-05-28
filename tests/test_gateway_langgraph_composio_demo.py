"""Regression: LangGraph + Composio connector demo template and bridge."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ax_cli import gateway as gateway_core
from ax_cli.connectors.api import (
    ConnectorToolCallResult,
    ConnectorToolMatch,
    ConnectorToolSearchResult,
    execute_connector_tool,
    search_connector_tools,
)
from ax_cli.gateway_runtime_types import (
    agent_template_catalog,
    agent_template_definition,
    agent_template_list,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO_ROOT / "examples" / "gateway_langgraph_composio" / "langgraph_composio_bridge.py"


def test_langgraph_composio_template_registered() -> None:
    catalog = agent_template_catalog()
    assert "langgraph_composio" in catalog
    template = agent_template_definition("langgraph_composio")
    assert template["runtime_type"] == "exec"
    assert "langgraph_composio_bridge.py" in str((template.get("defaults") or {}).get("exec_command") or "")


def test_langgraph_composio_listed_in_default_ordering() -> None:
    listed_ids = [item["id"] for item in agent_template_list()]
    assert "langgraph_composio" in listed_ids
    assert listed_ids.index("langgraph") < listed_ids.index("langgraph_composio")


def test_sanitize_exec_env_passes_connector_ref() -> None:
    env = gateway_core.sanitize_exec_env(
        "hello",
        {"name": "lgc", "agent_id": "a1", "runtime_type": "exec", "connector_ref": "my_composio"},
    )
    assert env["AX_GATEWAY_CONNECTOR_REF"] == "my_composio"


def test_search_connector_tools_maps_items(monkeypatch) -> None:
    from ax_cli.connectors import types as connector_types

    row = connector_types.ConnectorRow.create("demo", "composio")
    monkeypatch.setattr(
        "ax_cli.connectors.api.find_connector",
        lambda _ref: row,
    )
    monkeypatch.setattr("ax_cli.connectors.api.read_auth", lambda *_a, **_k: {"COMPOSIO_API_KEY": "ak"})
    monkeypatch.setattr(
        "ax_cli.connectors.api.search_tools",
        lambda *_a, **_k: {
            "items": [
                {
                    "enum": "GITHUB_LIST_PRS",
                    "displayName": "List PRs",
                    "description": "List pull requests",
                }
            ]
        },
    )

    result = search_connector_tools("demo", "list github prs")
    assert result.successful is True
    assert len(result.tools) == 1
    assert result.tools[0].slug == "GITHUB_LIST_PRS"
    assert result.tools[0].name == "List PRs"


def test_execute_connector_tool_success(monkeypatch) -> None:
    from ax_cli.connectors import types as connector_types

    row = connector_types.ConnectorRow.create("demo", "composio")
    monkeypatch.setattr("ax_cli.connectors.api.find_connector", lambda _ref: row)
    monkeypatch.setattr("ax_cli.connectors.api.read_auth", lambda *_a, **_k: {"COMPOSIO_API_KEY": "ak"})
    monkeypatch.setattr(
        "ax_cli.connectors.api.execute_tool",
        lambda *_a, **_k: {"successful": True, "data": {"count": 2}},
    )

    result = execute_connector_tool("demo", "GITHUB_LIST_PRS", {"owner": "o", "repo": "r"})
    assert result.successful is True
    assert result.data == {"count": 2}


def test_run_connector_round_search_only() -> None:
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import langgraph_composio_bridge as bridge
    finally:
        sys.path.pop(0)

    tool = ConnectorToolMatch(slug="GITHUB_LIST_STARGAZERS", name="List Stargazers", description="stars")
    search_result = ConnectorToolSearchResult(
        mode="intent",
        successful=True,
        tools=[tool],
        session_id="sess-1",
    )

    def _search(_ref: str, _use_case: str) -> ConnectorToolSearchResult:
        return search_result

    reply = bridge.run_connector_round("list stargazers", "my_composio", search_tools=_search)
    assert "GITHUB_LIST_STARGAZERS" in reply
    assert "RUN:" in reply


def test_run_connector_round_executes_run_directive() -> None:
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import langgraph_composio_bridge as bridge
    finally:
        sys.path.pop(0)

    search_result = ConnectorToolSearchResult(mode="catalog", successful=True, tools=[])
    call_result = ConnectorToolCallResult(successful=True, data={"count": 3})

    executed: list[tuple[str, str, dict]] = []

    def _search(_ref: str, _use_case: str) -> ConnectorToolSearchResult:
        return search_result

    def _execute(ref: str, slug: str, args: dict) -> ConnectorToolCallResult:
        executed.append((ref, slug, args))
        return call_result

    prompt = 'RUN:GITHUB_LIST_STARGAZERS {"owner":"ComposioHQ","repo":"composio"}'
    reply = bridge.run_connector_round(
        prompt,
        "my_composio",
        search_tools=_search,
        execute_tool=_execute,
    )
    assert executed == [("my_composio", "GITHUB_LIST_STARGAZERS", {"owner": "ComposioHQ", "repo": "composio"})]
    assert "RUN:GITHUB_LIST_STARGAZERS" in reply


def test_default_execute_passes_agent_identity(monkeypatch) -> None:
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import langgraph_composio_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "lgc-demo")
    monkeypatch.setenv("AX_AGENT_ID", "agent-uuid-1")
    captured: list[dict] = []

    def _fake_execute(*_a, **kwargs):
        captured.append(kwargs)
        return ConnectorToolCallResult(successful=True, data={})

    monkeypatch.setattr(
        "ax_cli.connectors.execute_connector_tool",
        _fake_execute,
    )
    bridge._default_execute("demo", "GITHUB_LIST_PRS", {"owner": "o"})
    assert captured[0]["agent_name"] == "lgc-demo"
    assert captured[0]["agent_id"] == "agent-uuid-1"


def test_bridge_main_requires_connector_ref(monkeypatch, capsys) -> None:
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import langgraph_composio_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.delenv("AX_GATEWAY_CONNECTOR_REF", raising=False)
    monkeypatch.setattr(sys, "argv", ["langgraph_composio_bridge.py", "hello"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = bridge.main()
    assert rc == 1
    assert "AX_GATEWAY_CONNECTOR_REF" in capsys.readouterr().err


def test_bridge_main_emits_lifecycle_with_mocked_round(monkeypatch, capsys) -> None:
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import langgraph_composio_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.setenv("AX_GATEWAY_CONNECTOR_REF", "demo")
    monkeypatch.setattr(sys, "argv", ["langgraph_composio_bridge.py", "find github tools"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(
        bridge,
        "run_connector_round",
        lambda prompt, ref, **kw: f"ok:{ref}:{prompt}",
    )

    rc = bridge.main()
    captured = capsys.readouterr()
    assert rc == 0

    statuses = []
    for line in captured.out.splitlines():
        if not line.startswith(bridge.EVENT_PREFIX):
            continue
        payload = json.loads(line[len(bridge.EVENT_PREFIX) :])
        if payload.get("kind") == "status":
            statuses.append(payload.get("status"))
    assert "processing" in statuses
    assert "completed" in statuses
    assert "ok:demo:find github tools" in captured.out


def test_register_langgraph_composio_requires_connector_ref(tmp_path, monkeypatch) -> None:
    from ax_cli.commands import gateway as gateway_cmd

    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="connector-ref"):
        gateway_cmd._register_managed_agent(
            name="lgc",
            template_id="langgraph_composio",
            connector_ref=None,
        )
