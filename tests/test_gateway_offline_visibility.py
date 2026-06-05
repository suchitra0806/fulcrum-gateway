"""AX_OFFLINE=1 silent-downgrade visibility.

Verifies the operator-facing surfaces that signal offline mode:

1. `ax gateway start` foreground warning when AX_OFFLINE=1 AND a real
   session file is present (the silent-downgrade danger zone).
2. `ax gateway status` OFFLINE indicator driven by the marker file the
   daemon writes at run-time (so the status check works even when the
   operator's current shell does not have AX_OFFLINE set).
3. `ax gateway run` (online path) clears any stale offline-mode marker
   from a SIGKILL'd previous offline daemon so the status indicator
   does not lie after a crash.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway as gateway_cmd
from ax_cli.main import app

runner = CliRunner()


def _seed_real_session(tmp_path, monkeypatch) -> None:
    """Write a real-looking gateway session file under a temp config dir."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_real.token",
            "base_url": "https://paxai.app",
            "space_id": "space-real-1",
        }
    )


# ── start warning surface ────────────────────────────────────────────────


def test_start_warns_when_offline_and_session_both_present(monkeypatch, tmp_path):
    """The silent-downgrade danger zone: real session + AX_OFFLINE=1.

    The operator gets a prominent foreground warning naming the session file
    path and the recovery action (`unset AX_OFFLINE`).
    """
    monkeypatch.setenv("AX_OFFLINE", "1")
    _seed_real_session(tmp_path, monkeypatch)
    monkeypatch.setattr(gateway_cmd, "active_gateway_pid", lambda: 12345)
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pid", lambda: 12346)
    monkeypatch.setattr(gateway_cmd, "ui_status", lambda: {"running": True, "url": "http://127.0.0.1:8765"})

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Warning: AX_OFFLINE=1 is set" in result.output
    assert "real gateway session" in result.output
    assert "unset AX_OFFLINE" in result.output


def test_start_no_warning_when_offline_only(monkeypatch, tmp_path):
    """Intentional offline use: AX_OFFLINE=1 with no real session file.

    No warning fires; this is the offline-development happy path PR #215
    optimized for.
    """
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config_empty"))
    monkeypatch.setattr(gateway_cmd, "active_gateway_pid", lambda: 12345)
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pid", lambda: 12346)
    monkeypatch.setattr(gateway_cmd, "ui_status", lambda: {"running": True, "url": "http://127.0.0.1:8765"})

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Warning: AX_OFFLINE=1" not in result.output
    assert "unset AX_OFFLINE" not in result.output


def test_start_no_warning_when_session_only(monkeypatch, tmp_path):
    """Normal real-platform use: real session, no AX_OFFLINE.

    No warning fires; backward compatibility with the pre-#215 start flow.
    """
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    _seed_real_session(tmp_path, monkeypatch)
    monkeypatch.setattr(gateway_cmd, "active_gateway_pid", lambda: 12345)
    monkeypatch.setattr(gateway_cmd, "active_gateway_ui_pid", lambda: 12346)
    monkeypatch.setattr(gateway_cmd, "ui_status", lambda: {"running": True, "url": "http://127.0.0.1:8765"})

    result = runner.invoke(app, ["gateway", "start", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Warning: AX_OFFLINE=1" not in result.output


# ── offline-mode lock helpers ────────────────────────────────────────────


def test_write_and_clear_offline_lock_round_trip(monkeypatch, tmp_path):
    """The marker file lifecycle: write at run-time, clear on shutdown."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))

    assert not gateway_cmd._is_offline_mode_active()

    gateway_cmd._write_offline_mode_lock()
    assert gateway_cmd._is_offline_mode_active()
    assert gateway_cmd._offline_mode_lock_path().exists()
    # Lock content is the pid; useful for forensic debugging of stale locks.
    assert gateway_cmd._offline_mode_lock_path().read_text().strip().isdigit()

    gateway_cmd._clear_offline_mode_lock()
    assert not gateway_cmd._is_offline_mode_active()
    assert not gateway_cmd._offline_mode_lock_path().exists()


def test_clear_offline_lock_is_idempotent_when_missing(monkeypatch, tmp_path):
    """Clearing a non-existent lock must not raise (covers the shutdown path
    where the daemon may not have written the lock yet)."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    # Must not raise.
    gateway_cmd._clear_offline_mode_lock()
    assert not gateway_cmd._is_offline_mode_active()


# ── status indicator surface ─────────────────────────────────────────────


def _seed_running_daemon_status(monkeypatch, *, offline: bool):
    """Stub out the heavy daemon_status / registry calls for a focused
    status-rendering test."""
    daemon_payload = {
        "running": True,
        "pid": 99999,
        "registry": {"agents": [], "gateway": {}},
    }
    ui_payload = {
        "running": True,
        "pid": 99998,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "log_path": "/tmp/ui.log",
    }
    monkeypatch.setattr(gateway_cmd, "daemon_status", lambda: daemon_payload)
    monkeypatch.setattr(gateway_cmd, "ui_status", lambda: ui_payload)
    monkeypatch.setattr(gateway_cmd, "list_gateway_approvals", lambda: [])
    monkeypatch.setattr(gateway_cmd, "load_recent_gateway_activity", lambda *a, **kw: [])
    monkeypatch.setattr(gateway_cmd, "_is_offline_mode_active", lambda: offline)


def test_status_shows_offline_indicator_when_lock_present(monkeypatch, tmp_path):
    """When the daemon was started in offline mode (marker file present),
    the status output renders a prominent OFFLINE line at the top of the
    human-mode output."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    _seed_running_daemon_status(monkeypatch, offline=True)

    result = runner.invoke(app, ["gateway", "status"])

    assert result.exit_code == 0, result.output
    assert "MODE" in result.output
    assert "OFFLINE" in result.output
    assert "AX_OFFLINE=1" in result.output
    assert "no platform calls" in result.output


def test_status_no_offline_indicator_when_lock_absent(monkeypatch, tmp_path):
    """When the daemon was not started in offline mode, the status output
    does not show the OFFLINE indicator."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    _seed_running_daemon_status(monkeypatch, offline=False)

    result = runner.invoke(app, ["gateway", "status"])

    assert result.exit_code == 0, result.output
    assert "OFFLINE" not in result.output
    assert "MODE" not in result.output


def test_status_json_includes_offline_mode_field(monkeypatch, tmp_path):
    """The `--json` payload exposes `offline_mode` for scripted consumers
    (operator tooling, CI checks, dashboards)."""
    import json

    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    _seed_running_daemon_status(monkeypatch, offline=True)

    result = runner.invoke(app, ["gateway", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["offline_mode"] is True


# ── stale-lock cleanup on online start ───────────────────────────────────


def test_online_run_clears_stale_offline_lock(monkeypatch, tmp_path):
    """Starting the daemon in online mode (no AX_OFFLINE) clears any stale
    lock from a previous offline run that was SIGKILL'd before its finally
    clause ran. Without this, `ax gateway status` would lie about the
    daemon's mode for the rest of the session.
    """
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))

    # Simulate a SIGKILL'd previous offline daemon: the lock exists on disk
    # but no daemon is actually running in offline mode.
    gateway_cmd._write_offline_mode_lock()
    assert gateway_cmd._is_offline_mode_active(), "precondition: stale lock present"

    # Stub the heavy daemon-bring-up so the run command can exit cleanly
    # without spawning anything or requiring a real session.
    fake_session = {"token": "real", "base_url": "https://paxai.app", "space_id": "s1"}
    monkeypatch.setattr(gateway_cmd, "_load_gateway_session_or_exit", lambda: fake_session)
    fake_daemon = MagicMock()
    fake_daemon.run = MagicMock(return_value=None)
    monkeypatch.setattr(gateway_cmd, "GatewayDaemon", lambda **kw: fake_daemon)

    result = runner.invoke(app, ["gateway", "run", "--once"])

    assert result.exit_code == 0, result.output
    assert not gateway_cmd._is_offline_mode_active(), "stale lock should be cleared at the top of the online run path"
    assert not gateway_cmd._offline_mode_lock_path().exists()
