"""Regression: `ax gateway connectors tools search` query as positional arg.

Original shape required ``--use-case "..."`` which surprised operators —
sibling commands like ``connectors tools list <ref>`` and ``gh search prs <q>``
use a positional query. The flag is preserved as a deprecated alias so
existing automation keeps working but emits a hint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


def _connector_double(monkeypatch):
    """Stub the connectors module so the command never hits the real provider."""
    from ax_cli.connectors import types as connector_types

    row = connector_types.ConnectorRow.create("demo", "composio")
    monkeypatch.setattr("ax_cli.connectors.find_connector", lambda _ref: row)
    monkeypatch.setattr("ax_cli.connectors.read_auth", lambda *_a, **_k: {"COMPOSIO_API_KEY": "ak"})
    captured: dict = {}

    def _search(_row, use_case, _auth_env, *, limit, mode):
        captured["use_case"] = use_case
        captured["mode"] = mode
        captured["limit"] = limit
        return {
            "items": [
                {"name": "GITHUB_LIST_PRS", "displayName": "List PRs", "description": "List PRs"},
            ]
        }

    monkeypatch.setattr("ax_cli.connectors.search_tools", _search)
    return captured


def test_positional_query_threads_through_to_search(monkeypatch):
    captured = _connector_double(monkeypatch)
    result = runner.invoke(
        app,
        ["gateway", "connectors", "tools", "search", "demo", "list github prs", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert captured["use_case"] == "list github prs"
    payload = json.loads(result.stdout)
    assert payload["connector"] == "demo"
    assert payload["query"] == "list github prs"


def test_use_case_flag_still_works_with_deprecation_hint(monkeypatch):
    captured = _connector_double(monkeypatch)
    result = runner.invoke(
        app,
        ["gateway", "connectors", "tools", "search", "demo", "--use-case", "list github prs", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert captured["use_case"] == "list github prs"
    assert "deprecated" in result.output.lower()


def test_both_positional_and_flag_fails_closed(monkeypatch):
    _connector_double(monkeypatch)
    result = runner.invoke(
        app,
        [
            "gateway",
            "connectors",
            "tools",
            "search",
            "demo",
            "list github prs",
            "--use-case",
            "list github prs",
        ],
    )
    assert result.exit_code == 1
    assert "not both" in result.output.lower()


def test_missing_query_fails_with_actionable_message(monkeypatch):
    _connector_double(monkeypatch)
    result = runner.invoke(
        app,
        ["gateway", "connectors", "tools", "search", "demo"],
    )
    assert result.exit_code == 1
    assert "missing query" in result.output.lower()
    # The actionable hint points operators at the new positional shape.
    assert "positional" in result.output.lower()


def test_help_describes_positional_query():
    """The help text must surface QUERY as the second positional so the operator
    instinct ('connectors tools search demo "..."') is documented, not hidden."""
    result = runner.invoke(app, ["gateway", "connectors", "tools", "search", "--help"])
    assert result.exit_code == 0
    # Typer renders the argument name in uppercase on the usage line.
    assert "QUERY" in result.output

    # Replace any patch from import-time isolation; this test only inspects help.
    with patch("ax_cli.connectors.search_tools", lambda *a, **k: {"items": []}):
        pass
