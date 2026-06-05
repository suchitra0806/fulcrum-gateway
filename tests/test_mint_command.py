import json
import os
from pathlib import Path

import httpx
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class FakeMintClient:
    base_url = "https://paxai.app"

    def list_agents(self):
        return {
            "agents": [
                {
                    "id": "12345678-90ab-cdef-1234-567890abcdef",
                    "name": "orion",
                }
            ]
        }

    def mgmt_issue_agent_pat(self, agent_id, *, name=None, expires_in_days=90, audience="both"):
        return {
            "token": "axp_a_newly_minted.secret",
            "expires_at": "2026-05-13T00:00:00Z",
            "agent_id": agent_id,
            "name": name,
            "audience": audience,
        }


class FakeCreateFallbackClient(FakeMintClient):
    def list_agents(self):
        return {"agents": []}

    def get_agent(self, agent):
        raise httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("GET", f"https://paxai.app/api/v1/agents/manage/{agent}"),
            response=httpx.Response(
                404,
                json={"detail": "not found"},
                request=httpx.Request("GET", f"https://paxai.app/api/v1/agents/manage/{agent}"),
            ),
        )

    def mgmt_create_agent(self, agent, **kwargs):
        raise httpx.HTTPStatusError(
            "Expected JSON but got HTML",
            request=httpx.Request("POST", "https://paxai.app/api/v1/agents/manage/create"),
            response=httpx.Response(
                200,
                text="<!DOCTYPE html><html></html>",
                headers={"content-type": "text/html"},
                request=httpx.Request("POST", "https://paxai.app/api/v1/agents/manage/create"),
            ),
        )

    def create_agent(self, agent, **kwargs):
        return {"id": "agent-created", "name": agent}


def test_token_mint_prints_token_when_not_saving(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" in result.output


def test_token_mint_create_falls_back_to_agents_api_when_management_route_is_frontend(
    monkeypatch, write_config, tmp_path
):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeCreateFallbackClient())

    result = runner.invoke(
        app,
        [
            "token",
            "mint",
            "new-agent",
            "--create",
            "--save-to",
            str(tmp_path),
            "--no-print-token",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Created:" in result.output
    assert "new-agent" in result.output
    assert "axp_a_newly_minted.secret" not in result.output


def test_token_mint_hides_token_when_saved(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--save-to", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" not in result.output
    assert "not printed" in result.output
    assert (tmp_path / ".ax" / "orion_token").read_text() == "axp_a_newly_minted.secret"


def test_token_mint_json_hides_token_when_saved(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    (tmp_path / ".ax" / "config.toml").chmod(0o600)
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--save-to", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "token" not in payload
    assert payload["token_redacted"] is True
    assert payload["token_printed"] is False
    assert payload["token_file"].endswith(".ax/orion_token")


def test_token_mint_can_print_saved_token_when_explicit(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    (tmp_path / ".ax" / "config.toml").chmod(0o600)
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--save-to", str(tmp_path), "--print-token", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["token"] == "axp_a_newly_minted.secret"
    assert payload["token_printed"] is True


def test_token_mint_uses_user_login_when_local_config_is_agent(monkeypatch, write_config):
    write_config(
        token="axp_a_agent.secret",
        base_url="https://paxai.app",
        agent_name="orion",
        agent_id="agent-orion",
    )
    user_config_dir = Path(os.environ["AX_CONFIG_DIR"])
    user_config_dir.mkdir(parents=True, exist_ok=True)
    (user_config_dir / "user.toml").write_text(
        'token = "axp_u_user.secret"\nbase_url = "https://paxai.app"\nprincipal_type = "user"\n'
    )
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" in result.output


def test_token_mint_env_selects_named_user_login(monkeypatch, write_config):
    write_config(token="axp_a_agent.secret", base_url="https://paxai.app", agent_name="orion")
    monkeypatch.setenv("AX_USER_TOKEN", "axp_u_dev.secret")

    def fake_get_user_client():
        assert os.environ["AX_USER_ENV"] == "dev"
        return FakeMintClient()

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", fake_get_user_client)

    result = runner.invoke(app, ["token", "mint", "orion", "--env", "dev"])

    assert result.exit_code == 0, result.output
    assert "axp_a_newly_minted.secret" in result.output


# --- _resolve_agent_id ---


def test_resolve_agent_id_by_uuid(monkeypatch):
    client = FakeMintClient()
    client.get_agent = lambda aid: {"agent": {"id": aid, "name": "orion"}}

    from ax_cli.commands.mint import _resolve_agent_id

    aid, aname = _resolve_agent_id(client, "12345678-90ab-cdef-1234-567890abcdef")
    assert aid == "12345678-90ab-cdef-1234-567890abcdef"
    assert aname == "orion"


def test_resolve_agent_id_by_uuid_exception_fallback():
    client = FakeMintClient()
    client.get_agent = lambda aid: (_ for _ in ()).throw(Exception("fail"))

    from ax_cli.commands.mint import _resolve_agent_id

    aid, aname = _resolve_agent_id(client, "12345678-90ab-cdef-1234-567890abcdef")
    assert aid == "12345678-90ab-cdef-1234-567890abcdef"
    assert aname == "12345678-90ab-cdef-1234-567890abcdef"


def test_resolve_agent_id_list_http_error_fallback_to_get():
    from ax_cli.commands.mint import _resolve_agent_id

    class Client:
        def list_agents(self):
            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(403, request=httpx.Request("GET", "http://x")),
            )

        def get_agent(self, name):
            return {"agent": {"id": "found-id", "name": name}}

    aid, aname = _resolve_agent_id(Client(), "myagent")
    assert aid == "found-id"


def test_resolve_agent_id_both_fail():
    from ax_cli.commands.mint import _resolve_agent_id

    class Client:
        def list_agents(self):
            return {"agents": []}

        def get_agent(self, name):
            raise httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
            )

    aid, aname = _resolve_agent_id(Client(), "myagent")
    assert aid is None
    assert aname == "myagent"


def test_resolve_agent_id_get_agent_returns_id_in_data():
    from ax_cli.commands.mint import _resolve_agent_id

    class Client:
        def list_agents(self):
            return {"agents": []}

        def get_agent(self, name):
            return {"id": "direct-id", "name": name}

    aid, aname = _resolve_agent_id(Client(), "myagent")
    assert aid == "direct-id"


# --- _is_management_route_miss_error ---


def test_is_management_route_miss_html_content_type():
    from ax_cli.commands.mint import _is_management_route_miss_error

    exc = httpx.HTTPStatusError(
        "err",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(
            200,
            text="<html></html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("POST", "http://x"),
        ),
    )
    assert _is_management_route_miss_error(exc) is True


def test_is_management_route_miss_html_body():
    from ax_cli.commands.mint import _is_management_route_miss_error

    exc = httpx.HTTPStatusError(
        "err",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(
            200,
            text="<!DOCTYPE html>",
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "http://x"),
        ),
    )
    assert _is_management_route_miss_error(exc) is True


def test_is_management_route_miss_404():
    from ax_cli.commands.mint import _is_management_route_miss_error

    exc = httpx.HTTPStatusError(
        "err",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(
            404,
            text="not found",
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "http://x"),
        ),
    )
    assert _is_management_route_miss_error(exc) is True


def test_is_management_route_miss_false_for_500():
    from ax_cli.commands.mint import _is_management_route_miss_error

    exc = httpx.HTTPStatusError(
        "err",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(
            500,
            text='{"error": "internal"}',
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", "http://x"),
        ),
    )
    assert _is_management_route_miss_error(exc) is False


# --- _create_agent_for_mint ---


def test_create_agent_for_mint_uses_mgmt(monkeypatch):
    from ax_cli.commands.mint import _create_agent_for_mint

    captured = {}

    class Client:
        def mgmt_create_agent(self, name, **kwargs):
            captured.update(kwargs)
            return {"agent": {"id": "mgmt-id", "name": name}}

    result = _create_agent_for_mint(Client(), "test-agent")
    assert result["id"] == "mgmt-id"
    assert captured.get("agent_type") == "direct"


def test_create_agent_for_mint_falls_back(monkeypatch):
    from ax_cli.commands.mint import _create_agent_for_mint

    captured = {}

    class Client:
        def mgmt_create_agent(self, name, **kwargs):
            raise httpx.HTTPStatusError(
                "html",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(
                    200,
                    text="<!DOCTYPE html>",
                    headers={"content-type": "text/html"},
                    request=httpx.Request("POST", "http://x"),
                ),
            )

        def create_agent(self, name, **kwargs):
            captured.update(kwargs)
            return {"agent": {"id": "fallback-id", "name": name}}

    result = _create_agent_for_mint(Client(), "test-agent")
    assert result["id"] == "fallback-id"
    assert captured.get("agent_type") == "direct"


def test_create_agent_for_mint_raises_on_real_error():
    import pytest

    from ax_cli.commands.mint import _create_agent_for_mint

    class Client:
        def mgmt_create_agent(self, name, **kwargs):
            raise httpx.HTTPStatusError(
                "real error",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(
                    500,
                    text='{"error": "fail"}',
                    headers={"content-type": "application/json"},
                    request=httpx.Request("POST", "http://x"),
                ),
            )

    with pytest.raises(httpx.HTTPStatusError):
        _create_agent_for_mint(Client(), "test-agent")


# --- mint command edge cases ---


def test_mint_no_user_token(monkeypatch, write_config):
    write_config(base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.resolve_user_token", lambda: None)

    result = runner.invoke(app, ["token", "mint", "orion"])
    assert result.exit_code == 1
    assert "No user token" in result.output


def test_mint_agent_pat_rejected(monkeypatch, write_config):
    write_config(base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.resolve_user_token", lambda: "axp_a_agent.secret")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])
    assert result.exit_code == 1
    assert "Cannot mint with an agent PAT" in result.output


def test_mint_unknown_token_prefix_warns(monkeypatch, write_config):
    write_config(base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.resolve_user_token", lambda: "unknown_prefix_token")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])
    assert result.exit_code == 0
    assert "not a recognized PAT type" in result.output


def test_mint_agent_not_found_no_create_non_tty(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")

    class NoAgentClient(FakeMintClient):
        def list_agents(self):
            return {"agents": []}

        def get_agent(self, name):
            raise httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
            )

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: NoAgentClient())

    result = runner.invoke(app, ["token", "mint", "missing-agent", "--json"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_mint_issue_pat_http_error(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")

    class FailMintClient(FakeMintClient):
        def mgmt_issue_agent_pat(self, agent_id, **kwargs):
            raise httpx.HTTPStatusError(
                "fail",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(403, request=httpx.Request("POST", "http://x")),
            )

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FailMintClient())

    result = runner.invoke(app, ["token", "mint", "orion"])
    assert result.exit_code == 1


def test_mint_no_token_in_response(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")

    class EmptyTokenClient(FakeMintClient):
        def mgmt_issue_agent_pat(self, agent_id, **kwargs):
            return {"name": "test"}

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: EmptyTokenClient())

    result = runner.invoke(app, ["token", "mint", "orion"])
    assert result.exit_code == 1
    assert "no token in response" in result.output


def test_mint_no_token_in_response_json(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")

    class EmptyTokenClient(FakeMintClient):
        def mgmt_issue_agent_pat(self, agent_id, **kwargs):
            return {"name": "test", "debug": True}

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: EmptyTokenClient())

    result = runner.invoke(app, ["token", "mint", "orion", "--json"])
    assert result.exit_code == 1


def test_mint_with_profile_creates_profile(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    from ax_cli.commands import profile as profile_mod

    profiles_dir = tmp_path / "profiles"
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(
        app,
        [
            "token",
            "mint",
            "orion",
            "--profile",
            "my-profile",
            "--no-print-token",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Profile created" in result.output
    assert (profiles_dir / "my-profile" / "profile.toml").exists()


def test_mint_with_profile_and_save_to(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    from ax_cli.commands import profile as profile_mod

    profiles_dir = tmp_path / "prof"
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", profiles_dir)

    save_dir = tmp_path / "save"
    save_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "token",
            "mint",
            "orion",
            "--save-to",
            str(save_dir),
            "--profile",
            "saved-profile",
            "--no-print-token",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Profile created" in result.output


def test_mint_create_with_create_http_error(monkeypatch, write_config):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")

    class FailCreateClient(FakeMintClient):
        def list_agents(self):
            return {"agents": []}

        def get_agent(self, name):
            raise httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
            )

        def mgmt_create_agent(self, name):
            raise httpx.HTTPStatusError(
                "forbidden",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(
                    403,
                    text='{"error": "forbidden"}',
                    headers={"content-type": "application/json"},
                    request=httpx.Request("POST", "http://x"),
                ),
            )

    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FailCreateClient())

    result = runner.invoke(app, ["token", "mint", "new-agent", "--create"])
    assert result.exit_code == 1


def test_mint_json_output_with_profile(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    from ax_cli.commands import profile as profile_mod

    profiles_dir = tmp_path / "profiles"
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", profiles_dir)

    result = runner.invoke(
        app,
        [
            "token",
            "mint",
            "orion",
            "--profile",
            "jp",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["profile"] == "jp"
    assert payload["token_redacted"] is True


def test_mint_save_to_with_ax_suffix(monkeypatch, write_config, tmp_path):
    write_config(token="axp_u_user.secret", base_url="https://paxai.app")
    monkeypatch.setattr("ax_cli.commands.mint.get_user_client", lambda: FakeMintClient())

    save_dir = tmp_path / "agent_home" / ".ax"
    save_dir.mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "token",
            "mint",
            "orion",
            "--save-to",
            str(save_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (save_dir / "orion_token").exists()
    assert (save_dir / "config.toml").exists()
