"""Unit tests for ``examples/gateway_langgraph/langgraph_bridge._load_dotenv_into_environ``.

Carved out of ``tests/test_langgraph_bridge_tools.py`` (PR #148 review
finding #1) so the .env-loader coverage runs in environments without
``langgraph`` installed. The bridge module's top-level imports are
stdlib-only and ``_load_dotenv_into_environ`` itself is pure stdlib
(``os`` + ``pathlib``); langgraph is lazy-loaded inside the tier
helpers, so importing the bridge module does not require langgraph.
This file deliberately does NOT carry ``pytest.importorskip("langgraph")``,
so the dotenv coverage actually closes #112 on every CI matrix.

Pre-this-split the tests sat behind the same module-level
``pytest.importorskip("langgraph")`` as the multi-node / ToolNode
coverage and silently skipped wherever langgraph was absent, leaving
the import-time side-effect uncovered exactly where the gap was filed.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest  # noqa: F401  — kept for parity with the sibling test module

# Bring the bridge module into namespace. The example lives outside ax_cli/
# so we extend sys.path the same way the sibling tools-test module does.
_EXAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "gateway_langgraph",
)
if _EXAMPLES_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLES_DIR)

import langgraph_bridge  # noqa: E402


def _reload_bridge():
    """Reload the bridge module so module-level state observes fresh env vars.

    Used by `test_reload_bridge_helper_loads_explicit_env_file` to exercise
    the module-import-time `_load_dotenv_into_environ()` side effect (#112).
    """
    importlib.reload(langgraph_bridge)


# ── #112: _load_dotenv_into_environ() unit coverage ──────────────────────────
#
# The .env loader (~50 lines in the bridge) runs at module import as an
# os.environ side effect. Pre-#148 there were no tests for any of its
# behaviors. Sean filed #112 from PR #86 review finding #3 calling out the
# gap. This class pins the six behaviors named in the issue plus a hermetic
# missing-file fallback, the emit_event surface, and the previously-dead
# `_reload_bridge()` helper.


class TestLoadDotenvIntoEnviron:
    """#112: unit coverage for _load_dotenv_into_environ()."""

    # Test keys prefixed so they cannot collide with any real .env that may
    # have leaked into os.environ at the original module import.
    KEY_A = "_TEST_LANGGRAPH_DOTENV_KEY_A"
    KEY_B = "_TEST_LANGGRAPH_DOTENV_KEY_B"

    def _clean_keys(self, monkeypatch):
        for k in (self.KEY_A, self.KEY_B):
            monkeypatch.delenv(k, raising=False)

    def _isolate_loader_candidates(self, monkeypatch, tmp_path):
        """Force all four loader candidate paths to resolve under tmp_path.

        The loader's candidate order is:
            1. AX_BRIDGE_ENV_FILE        (env var, can be neutralized via setenv)
            2. Path.cwd() / ".env"       (cwd, neutralized via monkeypatch.chdir)
            3. script_dir / ".env"       (Path(__file__).resolve().parent)
            4. script_dir.parent.parent / ".env"  (repo root from examples/...)

        Without this helper, candidates 3 and 4 resolve to real paths in the
        developer's checkout (PR #148 review finding #2: test_silent_on_missing_file
        was leaking the real repo-root .env into the test process). Pointing
        the bridge module's __file__ at a deep tmp_path location keeps all
        four candidates inside tmp_path, where nothing exists by default.
        """
        fake_script = tmp_path / "fake_script_dir" / "examples" / "gateway_langgraph" / "fake_bridge.py"
        fake_script.parent.mkdir(parents=True)
        monkeypatch.setattr(langgraph_bridge, "__file__", str(fake_script))
        monkeypatch.chdir(tmp_path)

    def test_parses_simple_key_value_pairs(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "explicit.env"
        env_file.write_text(f"{self.KEY_A}=alpha\n{self.KEY_B}=beta\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "alpha"
        assert os.environ[self.KEY_B] == "beta"

    def test_strips_double_quotes(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "quoted.env"
        env_file.write_text(f'{self.KEY_A}="double quoted value"\n')
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "double quoted value"

    def test_strips_single_quotes(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "quoted.env"
        env_file.write_text(f"{self.KEY_A}='single quoted value'\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "single quoted value"

    def test_preserves_mismatched_quotes(self, monkeypatch, tmp_path):
        # The strip condition requires matching outer quotes; a mismatched
        # pair (open with " close with ') is left intact, so the operator
        # sees the literal value rather than a silently-mangled string.
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "mismatched.env"
        env_file.write_text(f"{self.KEY_A}=\"not really closed'\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "\"not really closed'"

    def test_does_not_overwrite_existing_env_var(self, monkeypatch, tmp_path):
        # Pre-set the env var; the loader must not clobber it.
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        monkeypatch.setenv(self.KEY_A, "preset-by-operator")
        env_file = tmp_path / "explicit.env"
        env_file.write_text(f"{self.KEY_A}=loaded-from-file\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "preset-by-operator"

    def test_explicit_env_file_takes_precedence_over_cwd(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        explicit = tmp_path / "explicit.env"
        explicit.write_text(f"{self.KEY_A}=from-explicit\n")
        (tmp_path / ".env").write_text(f"{self.KEY_A}=from-cwd\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(explicit))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "from-explicit"

    def test_returns_after_first_match(self, monkeypatch, tmp_path):
        # When the explicit file is found and loaded, cwd/.env must NOT be
        # consulted; KEY_B (only present in cwd/.env) stays unset.
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        explicit = tmp_path / "explicit.env"
        explicit.write_text(f"{self.KEY_A}=from-explicit\n")
        (tmp_path / ".env").write_text(f"{self.KEY_B}=from-cwd\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(explicit))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "from-explicit"
        assert self.KEY_B not in os.environ

    def test_skips_comment_lines(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "commented.env"
        env_file.write_text(
            f"# a comment that should be ignored\n{self.KEY_A}=alpha\n# {self.KEY_B}=this_should_not_load\n"
        )
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "alpha"
        assert self.KEY_B not in os.environ

    def test_skips_lines_without_equals_sign(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "malformed.env"
        env_file.write_text(f"this_line_has_no_equals\n{self.KEY_A}=alpha\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "alpha"

    def test_skips_lines_with_empty_keys(self, monkeypatch, tmp_path):
        # A `=value` line has an empty key after partition + strip; the
        # `if not key` guard must drop it before os.environ[""] = value.
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "empty_key.env"
        env_file.write_text(f"=orphaned_value\n{self.KEY_A}=alpha\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: None)

        langgraph_bridge._load_dotenv_into_environ()

        assert os.environ[self.KEY_A] == "alpha"
        # Empty key never written to os.environ.
        assert "" not in os.environ

    def test_silent_on_missing_file(self, monkeypatch, tmp_path):
        # Truly hermetic now (PR #148 review finding #2): all four loader
        # candidates resolve under tmp_path via _isolate_loader_candidates,
        # so the real repo-root .env can't leak in. The load-bearing
        # assertion is that emit_event fires ZERO times — that positively
        # proves no file was loaded, rather than relying on the
        # namespaced-key absence which would pass even if a real .env
        # happened to lack our test key.
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        nonexistent = tmp_path / "does-not-exist.env"
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(nonexistent))
        captured: list[dict] = []
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: captured.append(payload))

        langgraph_bridge._load_dotenv_into_environ()

        # All four candidates miss → emit_event must NOT fire.
        assert captured == [], f"loader fired on a missing-file run, captured={captured!r}"
        assert self.KEY_A not in os.environ

    def test_emit_event_fires_with_loaded_path(self, monkeypatch, tmp_path):
        self._clean_keys(monkeypatch)
        self._isolate_loader_candidates(monkeypatch, tmp_path)
        env_file = tmp_path / "explicit.env"
        env_file.write_text(f"{self.KEY_A}=alpha\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        captured: list[dict] = []
        monkeypatch.setattr(langgraph_bridge, "emit_event", lambda payload: captured.append(payload))

        langgraph_bridge._load_dotenv_into_environ()

        assert len(captured) == 1
        event = captured[0]
        assert event["kind"] == "activity"
        assert "loaded .env from" in event["activity"]
        assert str(env_file) in event["activity"]

    def test_reload_bridge_helper_loads_explicit_env_file(self, monkeypatch, tmp_path):
        # Coverage for _reload_bridge(): the test helper at the top of the
        # original test_langgraph_bridge_tools.py module existed but had no
        # caller before this PR (#112 names this gap). The helper reloads
        # the bridge module, which triggers the module-level
        # _load_dotenv_into_environ() side effect. This pins the helper's
        # behavior so a future refactor doesn't silently break it.
        self._clean_keys(monkeypatch)
        # No _isolate_loader_candidates here because the reload re-executes
        # the module body and resets __file__ from sys.path; the explicit
        # AX_BRIDGE_ENV_FILE wins at candidate 1 regardless.
        env_file = tmp_path / "reload.env"
        env_file.write_text(f"{self.KEY_A}=reloaded\n")
        monkeypatch.setenv("AX_BRIDGE_ENV_FILE", str(env_file))
        monkeypatch.chdir(tmp_path)

        _reload_bridge()

        assert os.environ[self.KEY_A] == "reloaded"
