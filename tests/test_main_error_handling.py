"""Regression for #73: top-level error handling in ax_cli.main.main().

An HTTPStatusError (or a rejected gateway session PAT) that no command catches
locally used to escape Typer's re-raise and reach the operator as a 30+ line
Rich traceback. main() now maps these to single-line actionable messages.
"""

import httpx
import pytest

import ax_cli.main as main_mod
from ax_cli.commands.gateway import GatewaySessionRejectedError


def _raise(exc):
    def _app():
        raise exc

    return _app


def test_http_status_error_prints_actionable_line_not_traceback(monkeypatch, capsys):
    request = httpx.Request("POST", "https://paxai.app/auth/exchange")
    response = httpx.Response(401, json={"detail": "invalid_credential"}, request=request)
    err = httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
    monkeypatch.setattr(main_mod, "app", _raise(err))

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error 401" in captured.err
    assert "Traceback" not in captured.err
    assert "raise_for_status" not in captured.err


def test_gateway_session_rejected_maps_to_login_hint(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "app", _raise(GatewaySessionRejectedError()))

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "ax gateway login" in err
    assert "session.json" in err
    assert "Traceback" not in err


def test_connect_error_still_handled(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "app", _raise(httpx.ConnectError("no route")))

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main()

    assert exc_info.value.code == 1
    assert "cannot reach aX API" in capsys.readouterr().err
