"""CLI entrypoint must fail fast on unsupported Python versions.

``requires-python = ">=3.12"`` in ``pyproject.toml`` rejects older interpreters
at install time, but that guard is bypassed when axctl runs from a source
checkout or a mis-resolved virtualenv. ``_require_supported_python`` is the
runtime defense-in-depth: it exits early with an actionable upgrade message
instead of letting the user hit a cryptic downstream failure on 3.11.
"""

from __future__ import annotations

import sys

import pytest

from ax_cli.main import _MIN_PYTHON, _require_supported_python


def test_guard_exits_below_minimum(monkeypatch, capsys):
    """A sub-3.12 interpreter exits non-zero with an upgrade instruction."""
    monkeypatch.setattr(sys, "version_info", (3, 11, 9))

    with pytest.raises(SystemExit) as exc:
        _require_supported_python()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "3.12" in err
    assert "3.11.9" in err  # surfaces the actual running version


def test_guard_passes_at_minimum(monkeypatch):
    """Exactly the minimum version is supported — no exit."""
    monkeypatch.setattr(sys, "version_info", (*_MIN_PYTHON, 0))

    _require_supported_python()  # must not raise


def test_guard_passes_above_minimum(monkeypatch):
    """A newer interpreter (e.g. 3.13) is supported — no exit."""
    monkeypatch.setattr(sys, "version_info", (3, 13, 1))

    _require_supported_python()  # must not raise
