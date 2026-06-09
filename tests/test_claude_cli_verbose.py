"""Regression: --verbose must accompany --print + --output-format stream-json
in every Claude Code subprocess command we build.

Without --verbose the Claude CLI rejects the combination on Mac/Linux:
    "When using --print, --output-format=stream-json requires --verbose"
"""

from __future__ import annotations

import inspect

from ax_cli.runtimes.hermes.runtimes.claude_cli import ClaudeCLIRuntime


def test_claude_cli_runtime_execute_constructs_verbose_cmd() -> None:
    src = inspect.getsource(ClaudeCLIRuntime.execute)
    assert '"--verbose"' in src or "'--verbose'" in src, (
        "ClaudeCLIRuntime.execute() builds a Claude cmd missing --verbose. "
        "Claude CLI rejects --print + --output-format stream-json without it."
    )
