"""Per-module local pass-through command tests (gateway split #28 Phase 1 follow-up).

Rewritten counterparts of the ``local`` command tests in the skipped
``test_gateway_commands.py``. Monkeypatches target the owning module
(``ax_cli.commands.gateway_local``) where ``local init``/``connect`` resolve
their helpers. See docs/refactor/split-commands-gateway-removal.md.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from ax_cli.commands import gateway_local as gw
from ax_cli.main import app

runner = CliRunner()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _collapse_rich(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return re.sub(r"\s+", " ", text)


def test_gateway_local_init_writes_tokenless_config(monkeypatch, tmp_path):
    calls = {}

    def fake_request_local_connect(**kwargs):
        calls.update(kwargs)
        return {"status": "approved", "session_token": "local-session", "agent": {"name": kwargs["agent_name"]}}

    monkeypatch.setattr(gw, "_request_local_connect", fake_request_local_connect)

    result = runner.invoke(
        app,
        ["gateway", "local", "init", "mac_backend", "--workdir", str(tmp_path), "--force", "--json"],
    )

    assert result.exit_code == 0, result.output
    config_path = tmp_path / ".ax" / "config.toml"
    assert config_path.exists()
    config_text = config_path.read_text()
    assert 'mode = "local"' in config_text
    assert 'agent_name = "mac_backend"' in config_text
    assert "token" not in config_text
    assert "space_id" not in config_text
    assert calls["agent_name"] == "mac_backend"
    assert calls["space_id"] is None
    assert json.loads(result.output)["token_stored"] is False


def test_gateway_local_init_rejects_missing_workdir_by_default(monkeypatch, tmp_path):
    """Default behavior: --workdir must already exist; bail rather than silently mkdir."""
    monkeypatch.setattr(
        gw,
        "_request_local_connect",
        lambda **kwargs: pytest.fail("connect must not run when workdir is rejected"),
    )
    missing = tmp_path / "agents" / "mac_backend"
    assert not missing.exists()

    result = runner.invoke(app, ["gateway", "local", "init", "mac_backend", "--workdir", str(missing)])

    output = _collapse_rich(result.output)
    assert result.exit_code != 0
    assert "does not exist" in output
    assert "--create-workdir" in output
    assert not missing.exists(), "workdir must not be created without --create-workdir"
    assert not (missing / ".ax").exists()


def test_gateway_local_init_with_create_workdir_provisions_directory(monkeypatch, tmp_path):
    """`--create-workdir` opts in to making the missing folder."""
    calls = {}
    monkeypatch.setattr(
        gw,
        "_request_local_connect",
        lambda **kwargs: (
            calls.setdefault("connect", kwargs)
            or {"status": "approved", "session_token": "tok", "agent": {"name": kwargs["agent_name"]}}
        ),
    )

    new_workdir = tmp_path / "agents" / "fresh"
    assert not new_workdir.exists()

    result = runner.invoke(
        app,
        ["gateway", "local", "init", "fresh", "--workdir", str(new_workdir), "--create-workdir", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert new_workdir.is_dir()
    assert (new_workdir / ".ax" / "config.toml").exists()


def test_gateway_local_init_rejects_workdir_pointing_at_a_file(monkeypatch, tmp_path):
    """If --workdir resolves to an existing file, fail with a clear error."""
    monkeypatch.setattr(
        gw,
        "_request_local_connect",
        lambda **kwargs: pytest.fail("connect must not run when workdir is invalid"),
    )
    file_path = tmp_path / "not-a-dir.txt"
    file_path.write_text("nope")

    result = runner.invoke(app, ["gateway", "local", "init", "x", "--workdir", str(file_path)])

    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_ensure_workdir_helper_no_create_when_exists(tmp_path):
    """The helper is a no-op when the workdir already exists as a directory."""
    existing = tmp_path / "already_here"
    existing.mkdir()
    gw._ensure_workdir(existing, create=False)
    assert existing.is_dir()
