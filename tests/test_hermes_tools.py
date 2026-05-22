"""Tests for ax_cli/runtimes/hermes/tools/__init__.py

Covers ToolResult dataclass, TOOL_DEFINITIONS contents, path security checks,
tool implementations (with filesystem/subprocess mocking), and the
execute_tool() dispatcher.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ax_cli.runtimes.hermes.tools import (
    BLOCKED_READ_PATTERNS,
    TOOL_DEFINITIONS,
    ToolResult,
    _check_bash_command,
    _check_read_path,
    _check_write_path,
    execute_tool,
)

# ── ToolResult dataclass ──────────────────────────────────────────────────


class TestToolResult:
    def test_defaults(self):
        r = ToolResult(output="ok")
        assert r.output == "ok"
        assert r.is_error is False

    def test_error_flag(self):
        r = ToolResult(output="bad", is_error=True)
        assert r.is_error is True
        assert r.output == "bad"


# ── TOOL_DEFINITIONS ──────────────────────────────────────────────────────


class TestToolDefinitions:
    def test_expected_tool_names(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {"read_file", "write_file", "edit_file", "bash", "grep", "glob_files", "connector_search", "connector_call", "connector_apps"}
        assert names == expected

    def test_all_have_type_function(self):
        for t in TOOL_DEFINITIONS:
            assert t["type"] == "function"

    def test_all_have_parameters(self):
        for t in TOOL_DEFINITIONS:
            assert "parameters" in t
            assert "properties" in t["parameters"]

    def test_read_file_has_path_required(self):
        read_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "read_file")
        assert "path" in read_def["parameters"]["required"]

    def test_bash_has_command_required(self):
        bash_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "bash")
        assert "command" in bash_def["parameters"]["required"]


# ── _check_read_path ──────────────────────────────────────────────────────


class TestCheckReadPath:
    def test_blocked_patterns(self):
        for pattern in BLOCKED_READ_PATTERNS:
            path = f"/home/user{pattern}somefile"
            result = _check_read_path(path)
            assert result is not None, f"Expected blocked for pattern {pattern}"
            assert "Access denied" in result

    def test_ax_config_blocked(self):
        assert _check_read_path("/home/user/.ax/config.toml") is not None

    def test_ssh_blocked(self):
        assert _check_read_path("/home/user/.ssh/id_rsa") is not None

    def test_aws_blocked(self):
        assert _check_read_path("/home/user/.aws/credentials") is not None

    def test_env_file_blocked(self):
        assert _check_read_path("/project/.env") is not None

    def test_secrets_blocked(self):
        assert _check_read_path("/project/secrets/key.pem") is not None

    def test_normal_path_allowed(self):
        assert _check_read_path("/home/user/project/main.py") is None

    def test_tmp_allowed(self):
        assert _check_read_path("/tmp/output.txt") is None


# ── _check_write_path ─────────────────────────────────────────────────────


class TestCheckWritePath:
    def test_blocked_read_pattern_also_blocks_write(self):
        result = _check_write_path("/home/user/.ssh/id_rsa", "/home/user")
        assert result is not None
        assert "Access denied" in result

    def test_tmp_allowed(self):
        import sys

        if sys.platform == "darwin":
            pytest.skip("macOS /tmp symlink to /private/tmp causes prefix mismatch")
        assert _check_write_path("/tmp/output.txt", "/some/workdir") is None

    def test_workdir_allowed(self, tmp_path):
        workdir = str(tmp_path)
        target = str(tmp_path / "subdir" / "file.py")
        assert _check_write_path(target, workdir) is None

    def test_outside_workdir_blocked(self, tmp_path):
        workdir = str(tmp_path / "mywork")
        target = "/var/log/syslog"
        result = _check_write_path(target, workdir)
        assert result is not None
        assert "Write denied" in result

    def test_agents_worktrees_allowed(self):
        # Agents can write to worktrees under agents dir
        with patch("os.path.realpath") as mock_realpath:
            mock_realpath.side_effect = lambda p: p
            result = _check_write_path(
                "/home/ax-agent/agents/myagent/worktrees/task/file.py",
                "/some/workdir",
            )
            # Should be allowed (under agents worktrees)
            assert result is None

    def test_agents_workspace_allowed(self):
        with patch("os.path.realpath") as mock_realpath:
            mock_realpath.side_effect = lambda p: p
            result = _check_write_path(
                "/home/ax-agent/agents/myagent/workspace/file.py",
                "/some/workdir",
            )
            assert result is None

    def test_agents_notes_allowed(self):
        with patch("os.path.realpath") as mock_realpath:
            mock_realpath.side_effect = lambda p: p
            result = _check_write_path(
                "/home/ax-agent/agents/myagent/notes/log.txt",
                "/some/workdir",
            )
            assert result is None


# ── _check_bash_command ───────────────────────────────────────────────────


class TestCheckBashCommand:
    def test_cat_ax_config_blocked(self):
        result = _check_bash_command("cat ~/.ax/config.toml")
        assert result is not None
        assert "blocked" in result.lower()

    def test_cat_ssh_blocked(self):
        assert _check_bash_command("cat ~/.ssh/id_rsa") is not None

    def test_rm_rf_root_blocked(self):
        assert _check_bash_command("rm -rf /") is not None

    def test_cat_codex_blocked(self):
        assert _check_bash_command("cat ~/.codex/auth.json") is not None

    def test_cat_home_codex_blocked(self):
        assert _check_bash_command("cat /home/ax-agent/.codex/auth.json") is not None

    def test_normal_command_allowed(self):
        assert _check_bash_command("ls -la") is None

    def test_git_status_allowed(self):
        assert _check_bash_command("git status") is None

    def test_python_run_allowed(self):
        assert _check_bash_command("python3 main.py") is None


# ── Tool implementations ─────────────────────────────────────────────────


class TestReadFile:
    def test_reads_file_with_line_numbers(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        result = execute_tool("read_file", {"path": str(f)}, str(tmp_path))
        assert not result.is_error
        assert "line1" in result.output
        assert "line2" in result.output

    def test_offset_and_limit(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("a\nb\nc\nd\ne\n")
        result = execute_tool("read_file", {"path": str(f), "offset": 2, "limit": 2}, str(tmp_path))
        assert not result.is_error
        assert "b" in result.output
        assert "c" in result.output
        # Should not contain line 1 or line 4
        assert "a\n" not in result.output

    def test_file_not_found(self, tmp_path):
        result = execute_tool("read_file", {"path": str(tmp_path / "nope.txt")}, str(tmp_path))
        assert result.is_error
        assert "File not found" in result.output

    def test_blocked_path_returns_error(self):
        result = execute_tool("read_file", {"path": "/home/user/.ssh/id_rsa"}, "/tmp")
        assert result.is_error
        assert "Access denied" in result.output

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = execute_tool("read_file", {"path": str(f)}, str(tmp_path))
        assert not result.is_error
        assert "(empty file)" in result.output


class TestWriteFile:
    def test_writes_file(self, tmp_path):
        target = str(tmp_path / "out.txt")
        result = execute_tool("write_file", {"path": target, "content": "hello"}, str(tmp_path))
        assert not result.is_error
        assert "Wrote 5 bytes" in result.output
        assert (tmp_path / "out.txt").read_text() == "hello"

    def test_creates_parent_dirs(self, tmp_path):
        target = str(tmp_path / "sub" / "deep" / "out.txt")
        result = execute_tool("write_file", {"path": target, "content": "nested"}, str(tmp_path))
        assert not result.is_error

    def test_blocked_path(self):
        result = execute_tool("write_file", {"path": "/home/user/.ssh/key", "content": "x"}, "/tmp")
        assert result.is_error
        assert "Access denied" in result.output


class TestEditFile:
    def test_replaces_text(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    return 1\n")
        result = execute_tool(
            "edit_file",
            {"path": str(f), "old_text": "return 1", "new_text": "return 42"},
            str(tmp_path),
        )
        assert not result.is_error
        assert "Edited" in result.output
        assert "return 42" in f.read_text()

    def test_old_text_not_found(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello world")
        result = execute_tool(
            "edit_file",
            {"path": str(f), "old_text": "goodbye", "new_text": "hi"},
            str(tmp_path),
        )
        assert result.is_error
        assert "not found" in result.output

    def test_multiple_matches_rejected(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x = 1\ny = 1\n")
        result = execute_tool(
            "edit_file",
            {"path": str(f), "old_text": "= 1", "new_text": "= 2"},
            str(tmp_path),
        )
        assert result.is_error
        assert "matches 2 times" in result.output

    def test_blocked_path(self):
        result = execute_tool(
            "edit_file",
            {"path": "/home/user/.env", "old_text": "x", "new_text": "y"},
            "/tmp",
        )
        assert result.is_error


class TestBash:
    def test_runs_command(self, tmp_path):
        result = execute_tool("bash", {"command": "echo hello"}, str(tmp_path))
        assert not result.is_error
        assert "hello" in result.output

    def test_captures_stderr(self, tmp_path):
        result = execute_tool("bash", {"command": "echo err >&2"}, str(tmp_path))
        assert "err" in result.output

    def test_nonzero_exit_code(self, tmp_path):
        result = execute_tool("bash", {"command": "exit 1"}, str(tmp_path))
        assert "exit code 1" in result.output

    def test_timeout(self, tmp_path):
        result = execute_tool("bash", {"command": "sleep 999", "timeout": 1}, str(tmp_path))
        assert result.is_error
        assert "timed out" in result.output.lower()

    def test_blocked_command(self, tmp_path):
        result = execute_tool("bash", {"command": "cat ~/.ssh/id_rsa"}, str(tmp_path))
        assert result.is_error
        assert "blocked" in result.output.lower()

    def test_no_output_shows_placeholder(self, tmp_path):
        result = execute_tool("bash", {"command": "true"}, str(tmp_path))
        assert not result.is_error
        assert "(no output)" in result.output

    def test_long_output_truncated(self, tmp_path):
        # Generate output > 30000 chars
        cmd = "python3 -c \"print('x' * 40000)\""
        result = execute_tool("bash", {"command": cmd}, str(tmp_path))
        assert not result.is_error
        assert "truncated" in result.output


class TestGrep:
    def test_grep_runs(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    pass\n")
        # We mock subprocess.run since rg may not be installed
        with patch("ax_cli.runtimes.hermes.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test.py:1:def foo():\n", returncode=0)
            result = execute_tool("grep", {"pattern": "def", "path": str(tmp_path)}, str(tmp_path))
        assert not result.is_error
        assert "foo" in result.output

    def test_grep_no_matches(self, tmp_path):
        with patch("ax_cli.runtimes.hermes.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            result = execute_tool("grep", {"pattern": "xyz"}, str(tmp_path))
        assert "(no matches)" in result.output

    def test_grep_with_glob_filter(self, tmp_path):
        with patch("ax_cli.runtimes.hermes.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="found", returncode=0)
            execute_tool(
                "grep",
                {"pattern": "import", "path": str(tmp_path), "glob": "*.py"},
                str(tmp_path),
            )
            call_args = mock_run.call_args[0][0]
            assert "--glob" in call_args
            assert "*.py" in call_args

    def test_grep_rg_not_found(self, tmp_path):
        with patch("ax_cli.runtimes.hermes.tools.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("rg not found")
            result = execute_tool("grep", {"pattern": "test"}, str(tmp_path))
        assert result.is_error
        assert "rg" in result.output.lower()


class TestGlobFiles:
    def test_finds_files(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        result = execute_tool("glob_files", {"pattern": "*.py", "path": str(tmp_path)}, str(tmp_path))
        assert not result.is_error
        assert "a.py" in result.output
        assert "b.py" in result.output

    def test_no_matches(self, tmp_path):
        result = execute_tool("glob_files", {"pattern": "*.xyz", "path": str(tmp_path)}, str(tmp_path))
        assert "(no matches)" in result.output

    def test_uses_workdir_as_default_path(self, tmp_path):
        (tmp_path / "foo.txt").write_text("")
        result = execute_tool("glob_files", {"pattern": "*.txt"}, str(tmp_path))
        assert "foo.txt" in result.output


# ── execute_tool dispatcher ───────────────────────────────────────────────


class TestExecuteTool:
    def test_unknown_tool(self):
        result = execute_tool("nonexistent_tool", {}, "/tmp")
        assert result.is_error
        assert "Unknown tool" in result.output

    def test_exception_handling(self, tmp_path):
        # Force an exception by passing bad args to read_file (missing 'path' key)
        result = execute_tool("read_file", {}, str(tmp_path))
        assert result.is_error
        assert "Error:" in result.output

    def test_dispatches_to_correct_tool(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content")
        result = execute_tool("read_file", {"path": str(f)}, str(tmp_path))
        assert "content" in result.output
