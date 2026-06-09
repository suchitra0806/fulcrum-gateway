"""Per-module gateway command tests: gateway_auth (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_auth."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_auth as _gw_auth
from ax_cli.commands import gateway_session as _gw_session
from ax_cli.commands import gateway_spaces as _gw_spaces
from ax_cli.main import app
from ax_cli.offline_client import OfflineAxClient
from tests.gateway_cmd_testlib import _FakeLoginClient, _FakeTokenExchanger, _FakeUserClient, _make_429_error, _strip

runner = CliRunner()


def test_gateway_login_saves_gateway_session(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", _FakeTokenExchanger)
    monkeypatch.setattr(_gw_auth, "AxClient", _FakeLoginClient)

    result = runner.invoke(
        app,
        ["gateway", "login", "--token", "axp_u_test.token", "--url", "https://paxai.app", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["base_url"] == "https://paxai.app"
    assert payload["space_id"] == "space-1"
    session = gateway_core.load_gateway_session()
    assert session["token"] == "axp_u_test.token"
    assert session["base_url"] == "https://paxai.app"
    assert "ephemeral" not in session
    assert not (config_dir / "user.toml").exists()
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_login"
    assert recent[-1]["username"] == "madtank"


def test_gateway_login_no_persist_marks_session_ephemeral(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", _FakeTokenExchanger)
    monkeypatch.setattr(_gw_auth, "AxClient", _FakeLoginClient)

    result = runner.invoke(
        app,
        [
            "gateway",
            "login",
            "--token",
            "axp_u_test.token",
            "--url",
            "https://paxai.app",
            "--no-persist",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    session = gateway_core.load_gateway_session()
    assert session["ephemeral"] is True
    assert session["token"] == "axp_u_test.token"


def test_resolve_gateway_login_base_url_explicit_wins(monkeypatch):
    """Explicit --url arg must win over env, user config, and the default."""
    monkeypatch.setenv("AX_USER_BASE_URL", "https://env.example")
    monkeypatch.setattr(_gw_auth, "_load_user_config", lambda: {"base_url": "https://cfg.example"}, raising=False)
    assert _gw_auth._resolve_gateway_login_base_url("https://explicit.example") == "https://explicit.example"


def test_resolve_gateway_login_base_url_env_wins_when_no_explicit(monkeypatch):
    """AX_USER_BASE_URL env wins over the user config and the default."""
    monkeypatch.setenv("AX_USER_BASE_URL", "https://env.example")

    def _fake_load() -> dict:
        return {"base_url": "https://cfg.example"}

    from ax_cli import config as _config

    monkeypatch.setattr(_config, "_load_user_config", _fake_load)
    assert _gw_auth._resolve_gateway_login_base_url(None) == "https://env.example"


def test_resolve_gateway_login_base_url_user_cfg_wins_when_no_env(monkeypatch):
    """User-config base_url is used when no env override is set."""
    monkeypatch.delenv("AX_USER_BASE_URL", raising=False)

    def _fake_load() -> dict:
        return {"base_url": "https://cfg.example"}

    from ax_cli import config as _config

    monkeypatch.setattr(_config, "_load_user_config", _fake_load)
    assert _gw_auth._resolve_gateway_login_base_url(None) == "https://cfg.example"


def test_resolve_gateway_login_base_url_falls_to_paxai_when_unconfigured(monkeypatch):
    """The actual bug from issue #129: with no explicit arg, no env, and
    no axctl login, the gateway login command must default to paxai.app
    matching the --url help text, not the local-dev localhost:8001 that
    the broader resolve_user_base_url() would surface."""
    monkeypatch.delenv("AX_USER_BASE_URL", raising=False)

    from ax_cli import config as _config

    monkeypatch.setattr(_config, "_load_user_config", lambda: {})
    resolved = _gw_auth._resolve_gateway_login_base_url(None)
    assert resolved == "https://paxai.app"
    assert "localhost" not in resolved
    assert "127.0.0.1" not in resolved


def test_gateway_local_connect_infers_home_space_from_agent_rows(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": None,
            "username": "madtank",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "demo-hermes",
            "agent_id": "agent-existing",
            "space_id": "space-from-row",
            "runtime_type": "sentinel_inference_sdk",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_auth, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-local-2", "name": "codex-local"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    fingerprint = {
        "agent_name": "codex-local",
        "pid": 999999,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }

    payload = _gw_session._connect_local_pass_through_agent(agent_name="codex-local", fingerprint=fingerprint)

    assert payload["status"] == "pending"
    assert payload["approval"]["approval_id"] == payload["approval_id"]
    assert payload["approval"]["risk"] == "medium"
    stored = gateway_core.load_gateway_registry()
    entry = gateway_core.find_agent_entry(stored, "codex-local")
    assert entry["space_id"] == "space-from-row"


def test_gateway_local_connect_allows_existing_agent_to_reconnect_when_workdir_is_shared(monkeypatch, tmp_path):
    """Multi-tenant case: cli_god and pulse-cc legitimately share a workdir.

    If pulse-cc was registered first and cli_god's row also exists, cli_god
    re-connecting from the same physical workdir must NOT be rejected as a
    fingerprint collision — the operator has already approved both identities.

    Regression guard for aX task b4ecca83.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "jacob",
        }
    )
    monkeypatch.setattr(_gw_auth, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_spaces, "_hydrate_entry_space_from_database", lambda *a, **k: None)

    shared_fingerprint = {
        "pid": 999999,
        "parent_pid": 1,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "jacob",
    }
    pulse_fingerprint = {**shared_fingerprint, "agent_name": "pulse-cc"}
    cli_god_fingerprint = {**shared_fingerprint, "agent_name": "cli_god"}

    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "pulse-cc",
            "agent_id": "agent-pulse",
            "space_id": "space-1",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "approval_state": "approved",
            "attestation_state": "verified",
            "local_fingerprint": pulse_fingerprint,
        },
        {
            "name": "cli_god",
            "agent_id": "agent-cli-god",
            "space_id": "space-1",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "approval_state": "approved",
            "attestation_state": "verified",
            "local_fingerprint": cli_god_fingerprint,
        },
    ]
    gateway_core.save_gateway_registry(registry)

    # cli_god re-connects from the same workdir pulse-cc also uses.
    # Before the fix this raised ValueError("Gateway identity mismatch: ...
    # already registered as @pulse-cc"); now it should succeed because
    # cli_god's own registry row is found by name first, before the
    # collision check runs.
    result = _gw_session._connect_local_pass_through_agent(agent_name="cli_god", fingerprint=cli_god_fingerprint)
    assert result["agent"]["name"] == "cli_god"
    assert result["agent"]["agent_id"] == "agent-cli-god"


def test_gateway_move_waits_for_listener_ready_after_runtime_start(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )

    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "mover",
            "agent_id": "agent-mover",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": [
                {"space_id": "space-1", "name": "Old Space", "is_default": True},
                {"space_id": "space-2", "name": "New Space", "is_default": False},
            ],
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )
    gateway_core.save_gateway_registry(registry)

    class FakePlacementClient:
        def __init__(self):
            self.space_id = "space-1"

        def set_agent_placement(self, identifier, *, space_id, pinned=False):
            self.space_id = space_id
            return {"agent_id": identifier, "space_id": space_id, "allowed_spaces": ["space-1", "space-2"]}

        def get_agent_placement(self, identifier):
            return {
                "agent_id": identifier,
                "name": "mover",
                "space_id": self.space_id,
                "allowed_spaces": ["space-1", "space-2"],
                "_record": {
                    "id": identifier,
                    "name": "mover",
                    "space_id": self.space_id,
                    "allowed_spaces": ["space-1", "space-2"],
                },
            }

        def get_agent(self, identifier):
            return {"agent": {"id": identifier, "name": "mover", "space_id": self.space_id}}

        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "space-1", "name": "Old Space", "slug": "old-space"},
                    {"id": "space-2", "name": "New Space", "slug": "new-space"},
                ]
            }

    calls = {"recent": 0}

    def fake_recent(*, limit, agent_name):
        calls["recent"] += 1
        event = "runtime_started" if calls["recent"] == 1 else "listener_connected"
        return [{"ts": "9999-01-01T00:00:00+00:00", "event": event, "agent_name": agent_name}]

    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: FakePlacementClient())
    monkeypatch.setattr(_gw_agents, "active_gateway_pid", lambda: 1234)
    monkeypatch.setattr(_gw_agents, "load_recent_gateway_activity", fake_recent)
    monkeypatch.setattr(_gw_agents.time, "sleep", lambda _: None)

    moved = _gw_agents._move_managed_agent_space("mover", "space-2")

    assert moved["space_id"] == "space-2"
    assert calls["recent"] == 2


def test_with_upstream_429_retry_succeeds_on_second_attempt(monkeypatch):
    """Helper retries on 429 and returns the success result of the next call.

    Wait honors ``Retry-After: 12`` from the server response rather than the
    1s exponential-backoff default — paxai.app's per-user bucket needs the
    full server-advertised cooldown before the retry has any chance of
    succeeding.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(_gw_auth.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _make_429_error()
        return {"agent": "ok"}

    result = _gw_auth._with_upstream_429_retry(call, max_retries=2, base_wait=1.0)
    assert result == {"agent": "ok"}
    assert calls["n"] == 2
    assert sleeps == [12.0]  # max(exp=1.0, retry_after=12)


def test_with_upstream_429_retry_exhausts_then_raises(monkeypatch):
    """All attempts 429 → raises UpstreamRateLimitedError carrying the
    parsed Retry-After hint.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(_gw_auth.time, "sleep", lambda s: sleeps.append(s))

    def call():
        raise _make_429_error()

    with pytest.raises(_gw_auth.UpstreamRateLimitedError) as exc_info:
        _gw_auth._with_upstream_429_retry(call, max_retries=2, base_wait=1.0)
    assert exc_info.value.retries_attempted == 2
    assert exc_info.value.retry_after_seconds == 12  # parsed from header
    # Both retries honor Retry-After: 12 (max of exp backoff 1s/2s and 12s hint).
    assert sleeps == [12.0, 12.0]


def test_with_upstream_429_retry_falls_back_to_exp_backoff_without_retry_after(monkeypatch):
    """If the server omits Retry-After, fall back to the exponential
    backoff schedule. Preserves prior behavior for non-conforming responses.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(_gw_auth.time, "sleep", lambda s: sleeps.append(s))

    request = httpx.Request("POST", "https://paxai.app/api/v1/agents")
    no_hint = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(429, request=request),  # no Retry-After header
    )

    def call():
        raise no_hint

    with pytest.raises(_gw_auth.UpstreamRateLimitedError):
        _gw_auth._with_upstream_429_retry(call, max_retries=2, base_wait=1.0)
    assert sleeps == [1.0, 2.0]  # exp backoff: 1*2^0, 1*2^1


def test_with_upstream_429_retry_caps_wait_at_max(monkeypatch):
    """Pathological Retry-After values are capped at ``max_wait`` so a
    misbehaving server can't hang the CLI for hours.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(_gw_auth.time, "sleep", lambda s: sleeps.append(s))

    request = httpx.Request("POST", "https://paxai.app/api/v1/agents")
    insane = httpx.HTTPStatusError(
        "429",
        request=request,
        response=httpx.Response(429, headers={"retry-after": "999999"}, request=request),
    )

    def call():
        raise insane

    with pytest.raises(_gw_auth.UpstreamRateLimitedError):
        _gw_auth._with_upstream_429_retry(call, max_retries=2, base_wait=1.0, max_wait=30.0)
    assert sleeps == [30.0, 30.0]  # both capped at max_wait


def test_with_upstream_429_retry_propagates_other_errors(monkeypatch):
    """Non-429 httpx errors propagate without retry."""
    monkeypatch.setattr(_gw_auth.time, "sleep", lambda s: None)

    request = httpx.Request("POST", "https://paxai.app/api/v1/agents")
    server_error = httpx.HTTPStatusError(
        "500 Internal Server Error",
        request=request,
        response=httpx.Response(500, request=request),
    )

    def call():
        raise server_error

    with pytest.raises(httpx.HTTPStatusError):
        _gw_auth._with_upstream_429_retry(call, max_retries=3, base_wait=0.1)


class TestLoginCommand:
    def test_login_non_user_pat_fails(self, monkeypatch):
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_token", lambda t: "axp_a_badprefix")
        result = runner.invoke(app, ["gateway", "login", "--token", "axp_a_badprefix"])
        assert result.exit_code != 0
        assert "user PAT" in _strip(result.output)

    def test_login_exchanger_fails(self, monkeypatch):
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_token", lambda t: "axp_u_test123")
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_base_url", lambda explicit=None: "https://paxai.app")

        # Mock the TokenExchanger import
        fake_exchanger = MagicMock()
        fake_exchanger.get_token.side_effect = RuntimeError("exchange failed")
        monkeypatch.setattr(_gw_auth, "TokenExchanger", lambda url, tok: fake_exchanger, raising=False)

        result = runner.invoke(app, ["gateway", "login", "--token", "axp_u_test123"])
        assert result.exit_code != 0

    def test_login_json_success(self, monkeypatch):
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_token", lambda t: "axp_u_test123")
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_base_url", lambda explicit=None: "https://paxai.app")

        # Mock TokenExchanger
        fake_exchanger = MagicMock()
        fake_exchanger.get_token.return_value = "jwt_test"

        # We need to patch the from-import that happens inside the function
        import ax_cli.token_cache as tc_mod

        monkeypatch.setattr(tc_mod, "TokenExchanger", lambda url, tok: fake_exchanger)

        fake_client = MagicMock()
        fake_client.whoami.return_value = {"username": "tester", "email": "t@t.com"}
        fake_client.list_spaces.return_value = {"spaces": []}
        monkeypatch.setattr(_gw_auth, "AxClient", lambda **kw: fake_client)

        from ax_cli.commands import auth as auth_cmd

        monkeypatch.setattr(auth_cmd, "_select_login_space", lambda spaces: None)
        monkeypatch.setattr(_gw_auth, "save_gateway_session", lambda p: Path("/tmp/session.json"))
        monkeypatch.setattr(_gw_auth, "load_gateway_registry", lambda: {"gateway": {}})
        monkeypatch.setattr(_gw_auth, "save_gateway_registry", lambda r: None)
        monkeypatch.setattr(_gw_auth, "record_gateway_activity", lambda *a, **kw: None)

        result = runner.invoke(app, ["gateway", "login", "--token", "axp_u_test123", "--json"])
        assert result.exit_code == 0
        # Output contains err_console prefix lines + JSON; find the JSON part
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data = json.loads(output[json_start:])
        assert data["username"] == "tester"

    def test_login_with_space_id(self, monkeypatch):
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_token", lambda t: "axp_u_test123")
        monkeypatch.setattr(_gw_auth, "_resolve_gateway_login_base_url", lambda explicit=None: "https://paxai.app")

        import ax_cli.token_cache as tc_mod

        fake_exchanger = MagicMock()
        fake_exchanger.get_token.return_value = "jwt_test"
        monkeypatch.setattr(tc_mod, "TokenExchanger", lambda url, tok: fake_exchanger)

        fake_client = MagicMock()
        fake_client.whoami.return_value = {"username": "tester", "email": "t@t.com"}
        fake_client.list_spaces.return_value = {"spaces": [{"id": "sp-1", "name": "Work"}]}
        monkeypatch.setattr(_gw_auth, "AxClient", lambda **kw: fake_client)
        monkeypatch.setattr(_gw_auth, "resolve_space_id", lambda client, explicit: "sp-1")
        monkeypatch.setattr(_gw_auth, "save_gateway_session", lambda p: Path("/tmp/session.json"))
        monkeypatch.setattr(_gw_auth, "load_gateway_registry", lambda: {"gateway": {}})
        monkeypatch.setattr(_gw_auth, "save_gateway_registry", lambda r: None)
        monkeypatch.setattr(_gw_auth, "record_gateway_activity", lambda *a, **kw: None)

        from ax_cli.commands import auth as auth_cmd

        monkeypatch.setattr(auth_cmd, "_candidate_space_id", lambda s: "sp-1")

        result = runner.invoke(app, ["gateway", "login", "--token", "axp_u_test123", "--space-id", "sp-1", "--json"])
        assert result.exit_code == 0
        output = result.output
        json_start = output.find("{")
        assert json_start >= 0, f"No JSON found in output: {output!r}"
        data = json.loads(output[json_start:])
        assert data["space_id"] == "sp-1"


def test_load_gateway_session_or_exit_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    session = _gw_auth._load_gateway_session_or_exit()
    assert session["base_url"] == "http://localhost:8765"
    assert session["token"] == "offline"


def test_load_gateway_session_or_exit_offline_custom_url(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_LOCAL_GATEWAY_URL", "http://localhost:9999")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    session = _gw_auth._load_gateway_session_or_exit()
    assert session["base_url"] == "http://localhost:9999"


def test_load_gateway_user_client_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    client = _gw_auth._load_gateway_user_client()
    assert isinstance(client, OfflineAxClient)
