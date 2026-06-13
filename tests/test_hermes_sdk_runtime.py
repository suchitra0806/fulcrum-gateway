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
    # Enriched payload mirrors the stderr framing so SSE-only operators get
    # the same impact context — the wrapped tool list and the credential-exfil
    # consequence — not just the exception repr (#208).
    assert "read_file" in cb.status_calls[0]
    assert "unsandboxed" in cb.status_calls[0]
    assert "credential-bearing" in cb.status_calls[0]

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


def test_install_secure_tools_tolerates_callback_error(monkeypatch, tmp_path, capsys, caplog):
    """A misbehaving callback (e.g. raises inside on_status) must not let the
    helper raise — the stderr WARNING is the defense-in-depth path and the
    helper still returns False so the caller proceeds correctly. The swallow
    is intentional but no longer silent: it logs a WARNING naming the
    third-order callback failure (#208)."""

    def _boom(workdir):
        raise ImportError("tools package missing")

    class _ExplodingCallback:
        def on_status(self, status: str) -> None:
            raise RuntimeError("downstream consumer is sick")

    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", _boom)

    with caplog.at_level(logging.WARNING, logger="runtime.hermes_sdk"):
        result = hermes_sdk._install_secure_tools(str(tmp_path), cb=_ExplodingCallback())

    assert result is False
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tool security setup failed" in captured.err

    # The on_status swallow keeps the degradation path alive, but a WARNING
    # now surfaces the broken consumer to operators at elevated log levels.
    assert any(
        "on_status callback raised" in record.message and record.levelno == logging.WARNING for record in caplog.records
    ), f"expected an on_status-callback WARNING, got {[(r.levelname, r.message) for r in caplog.records]}"


# ── AX_HERMES_STRICT_SECURITY=1: opt-in fail-closed mode (follow-up to #151) ──


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE", "Yes", " 1 ", "\ttrue\n"])
def test_strict_security_enabled_recognizes_truthy_values(monkeypatch, value):
    """Truthy parsing matches the documented variants and tolerates
    whitespace/case so operators don't get surprised by AX_HERMES_STRICT_SECURITY=Yes
    being silently treated as off."""
    monkeypatch.setenv("AX_HERMES_STRICT_SECURITY", value)
    assert hermes_sdk._strict_security_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "FALSE", "nope"])
def test_strict_security_enabled_rejects_falsy_values(monkeypatch, value):
    """Anything that isn't 1/true/yes leaves the helper in its loud-but-functional
    default — explicitly including ``0`` and ``false`` so an operator setting
    ``AX_HERMES_STRICT_SECURITY=0`` to opt OUT keeps the existing PR #191 behavior."""
    monkeypatch.setenv("AX_HERMES_STRICT_SECURITY", value)
    assert hermes_sdk._strict_security_enabled() is False


def test_strict_security_disabled_when_env_unset(monkeypatch):
    """Default (env var not present) is lenient — preserves the PR #191
    backward-compatible posture for deployments that don't opt in."""
    monkeypatch.delenv("AX_HERMES_STRICT_SECURITY", raising=False)
    assert hermes_sdk._strict_security_enabled() is False


def test_install_secure_tools_strict_mode_reraises_on_failure(monkeypatch, tmp_path, capsys, caplog):
    """Strict mode: when AX_HERMES_STRICT_SECURITY=1 AND _secure_hermes_tools
    raises, the helper must fire all three loud-degradation channels FIRST
    (so operators see WHAT failed) and then raise HermesSecuritySetupError
    with the original exception preserved as __cause__ (so the caller can
    include the underlying failure in its operator-facing error)."""
    monkeypatch.setenv("AX_HERMES_STRICT_SECURITY", "1")

    original = ImportError("No module named 'tools'")

    def _boom(workdir):
        raise original

    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", _boom)
    cb = _RecordingCallback()

    with caplog.at_level(logging.ERROR, logger="runtime.hermes_sdk"):
        with pytest.raises(hermes_sdk.HermesSecuritySetupError) as excinfo:
            hermes_sdk._install_secure_tools(str(tmp_path), cb=cb)

    # __cause__ chain preserves the underlying ImportError so the caller can
    # surface a specific operator-facing error rather than a generic refusal.
    assert excinfo.value.__cause__ is original

    # All three loud-degradation channels fire BEFORE the raise so the failure
    # is visible whether or not the caller catches the exception. This is the
    # belt-and-suspenders posture: strict mode adds a refusal, it doesn't
    # replace the loud signal.
    assert any(
        "tool security setup failed" in record.message and record.levelno == logging.ERROR for record in caplog.records
    )
    assert len(cb.status_calls) == 1
    assert "security_wrapper_degraded" in cb.status_calls[0]
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "tool security setup failed" in captured.err


def test_install_secure_tools_lenient_when_env_explicitly_unset(monkeypatch, tmp_path):
    """Lenient mode is the default: when AX_HERMES_STRICT_SECURITY is unset
    AND _secure_hermes_tools raises, the helper returns False without raising
    (preserves the PR #191 loud-but-functional posture, matches PR #121)."""
    monkeypatch.delenv("AX_HERMES_STRICT_SECURITY", raising=False)

    def _boom(workdir):
        raise ImportError("tools package missing")

    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", _boom)

    result = hermes_sdk._install_secure_tools(str(tmp_path), cb=None)

    assert result is False  # no raise


def test_install_secure_tools_strict_mode_returns_true_on_success(monkeypatch, tmp_path):
    """Strict mode AND wrap succeeds: helper returns True normally, no
    spurious raise. The strict-mode branch must be gated on the failure
    path so the happy path is identical to lenient mode."""
    monkeypatch.setenv("AX_HERMES_STRICT_SECURITY", "1")
    called = []
    monkeypatch.setattr(hermes_sdk, "_secure_hermes_tools", lambda workdir: called.append(workdir))

    result = hermes_sdk._install_secure_tools(str(tmp_path), cb=None)

    assert result is True
    assert called == [str(tmp_path)]
