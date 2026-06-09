"""Tests for the Hermes SDK runtime adapter.

Currently focused on Windows-vs-POSIX permission warning parity — the
adapter's `_read_token_file` used to log a "loose permissions" warning on
every call against Windows because NTFS reports POSIX mode bits as
0o666/0o644 regardless of the file's actual ACLs. This is the same class
of bug fixed for `ax_cli/token_cache.py` and `ax_cli/config.py` upstream;
the Hermes adapter was a deferred sibling.
"""

from __future__ import annotations

import logging
import sys

import pytest

from ax_cli.runtimes.hermes.runtimes import hermes_sdk


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_read_token_file_warns_on_loose_permissions_posix(tmp_path, caplog):
    token = tmp_path / "codex-token"
    token.write_text("axt_loose")
    token.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="runtime.hermes_sdk"):
        result = hermes_sdk._read_token_file(token)

    assert result == "axt_loose"
    assert any("loose permissions" in record.message for record in caplog.records)


def test_read_token_file_skips_permission_warning_on_windows(tmp_path, caplog, monkeypatch):
    """On Windows the mode check would warn on every read — the guard must suppress it."""
    monkeypatch.setattr(hermes_sdk.sys, "platform", "win32")
    token = tmp_path / "codex-token"
    token.write_text("axt_winsafe")
    if sys.platform != "win32":
        token.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="runtime.hermes_sdk"):
        result = hermes_sdk._read_token_file(token)

    assert result == "axt_winsafe"
    assert not any("loose permissions" in record.message for record in caplog.records), [
        record.message for record in caplog.records
    ]


def test_read_token_file_returns_empty_on_missing_path(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert hermes_sdk._read_token_file(missing) == ""


# ── #151: _install_secure_tools surfaces wrap failures loudly ───────────────


class _RecordingCallback:
    """Minimal StreamCallback that records on_status calls."""

    def __init__(self):
        self.status_calls: list[str] = []

    def on_status(self, status: str) -> None:
        self.status_calls.append(status)


def test_install_secure_tools_returns_true_on_success(monkeypatch, tmp_path):
    """Happy path: wrap installs, helper returns True, no degradation signals fire."""
    called = []
    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", lambda workdir: called.append(workdir))
    cb = _RecordingCallback()

    result = hermes_sdk._install_secure_tools(str(tmp_path), cb=cb)

    assert result is True
    assert called == [str(tmp_path)]
    # Happy path emits nothing on the degradation channels.
    assert cb.status_calls == []


def test_install_secure_tools_surfaces_failure_via_callback(monkeypatch, tmp_path, capsys, caplog):
    """#151: when _secure_hermes_tools raises (typical: tools package not
    importable in an IL2 build), the helper must surface the failure through
    cb.on_status, stderr, AND log.error. Pre-fix this was a single
    log.warning that operator-facing channels never saw, so the agent ran
    with unrestricted read_file / terminal / write_file access without any
    obvious operator-visible signal.
    """

    def _boom(workdir):
        raise ImportError("No module named 'tools'")

    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", _boom)
    cb = _RecordingCallback()

    with caplog.at_level(logging.ERROR, logger="runtime.hermes_sdk"):
        result = hermes_sdk._install_secure_tools(str(tmp_path), cb=cb)

    # Returns False so the caller knows the wrap did not install — but does
    # not raise (loud-but-functional posture, matches the langgraph fix from
    # PR #121; a strict fail-closed mode is a separate follow-up).
    assert result is False

    # Channel 1: callback. The gateway / SSE listener sees a security event.
    assert len(cb.status_calls) == 1, f"expected exactly one on_status, got {cb.status_calls!r}"
    assert "security_wrapper_degraded" in cb.status_calls[0]
    assert "No module named 'tools'" in cb.status_calls[0]

    # Channel 2: stderr. Operators running the runtime directly without a
    # callback consumer see a WARNING line in their terminal.
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tool security setup failed" in captured.err
    assert "run unsandboxed" in captured.err

    # Channel 3: runtime log at error severity (was warning before #151).
    assert any(
        "tool security setup failed" in record.message and record.levelno == logging.ERROR for record in caplog.records
    ), f"expected an ERROR-level runtime-log entry, got {[(r.levelname, r.message) for r in caplog.records]}"


def test_install_secure_tools_does_not_raise_when_callback_is_none(monkeypatch, tmp_path, capsys):
    """The helper accepts cb=None (e.g. runtime invoked outside the SSE path)
    and degrades to just the stderr + log surfaces without raising."""

    def _boom(workdir):
        raise RuntimeError("registry shape changed under us")

    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", _boom)

    result = hermes_sdk._install_secure_tools(str(tmp_path), cb=None)

    assert result is False
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tool security setup failed" in captured.err


def test_install_secure_tools_tolerates_callback_error(monkeypatch, tmp_path, capsys):
    """A misbehaving callback (e.g. raises inside on_status) must not let the
    helper raise — the stderr WARNING is the defense-in-depth path and the
    helper still returns False so the caller proceeds correctly."""

    def _boom(workdir):
        raise ImportError("tools package missing")

    class _ExplodingCallback:
        def on_status(self, status: str) -> None:
            raise RuntimeError("downstream consumer is sick")

    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", _boom)

    result = hermes_sdk._install_secure_tools(str(tmp_path), cb=_ExplodingCallback())

    assert result is False
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tool security setup failed" in captured.err
