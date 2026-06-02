"""Doctor surfaces a warning when a user PAT exists in both the on-disk
user.toml and either of the user-PAT env vars (AX_TOKEN or AX_USER_TOKEN).

Without this, operators who adopt an encrypted-env workflow (dotenvx, sops,
pass) get a silent shadow copy of their PAT in ~/.ax/user.toml and don't know
it. The warning text is honest about the precedence split: user-PAT commands
read the file first; general runtime commands read the env var first. The
remediation guidance points at clearing only the `token` field so other
login defaults (base_url, space_id, environment) survive.

Per andrewprograde / #175, the resolvers (resolve_token vs resolve_user_token)
disagree on precedence; the warning text reflects that reality rather than
claiming a single winner.
"""

import pytest

from ax_cli.config import diagnose_auth_config


@pytest.fixture
def isolated_global(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_USER_TOKEN", raising=False)
    monkeypatch.delenv("AX_ENV", raising=False)
    monkeypatch.delenv("AX_USER_ENV", raising=False)
    return global_dir


def _write_default_user_toml(global_dir, token="axp_u_file.secret"):
    user_path = global_dir / "user.toml"
    user_path.write_text(f'token = "{token}"\nbase_url = "https://paxai.app"\nprincipal_type = "user"\n')
    return user_path


def _write_named_env_user_toml(global_dir, env_name="dev", token="axp_u_dev_file.secret"):
    user_dir = global_dir / "users" / env_name
    user_dir.mkdir(parents=True)
    (user_dir / "user.toml").write_text(
        f'token = "{token}"\nbase_url = "https://dev.paxai.app"\nprincipal_type = "user"\nenvironment = "{env_name}"\n'
    )
    return user_dir / "user.toml"


def _assert_honest_reason(warning, user_path):
    """The warning's reason must name the precedence split, point at the file,
    and steer the operator to clear the `token` field instead of `rm` (which
    would also wipe base_url / space_id / environment)."""
    reason = warning["reason"]
    assert str(user_path) in reason
    # Names the precedence split honestly — no single "X wins" claim.
    assert "precedence" in reason.lower()
    # Steers to clearing just the token field, not a blanket rm.
    assert "`token`" in reason
    assert "base_url" in reason or "login defaults" in reason


def test_warning_fires_when_user_toml_and_ax_token_both_set(isolated_global, monkeypatch):
    user_path = _write_default_user_toml(isolated_global)
    monkeypatch.setenv("AX_TOKEN", "axp_u_env.secret")

    diagnostic = diagnose_auth_config()

    warnings = {w["code"]: w for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" in warnings
    warning = warnings["user_pat_in_file_and_env"]
    assert warning["path"] == str(user_path)
    _assert_honest_reason(warning, user_path)


def test_warning_fires_when_user_toml_and_ax_user_token_both_set(isolated_global, monkeypatch):
    """AX_USER_TOKEN is the override resolve_user_token actually honors.
    Doctor must surface the shadow on either env var, not just AX_TOKEN."""
    user_path = _write_default_user_toml(isolated_global)
    monkeypatch.setenv("AX_USER_TOKEN", "axp_u_user_env.secret")

    diagnostic = diagnose_auth_config()

    warnings = {w["code"]: w for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" in warnings
    _assert_honest_reason(warnings["user_pat_in_file_and_env"], user_path)


def test_warning_silent_when_only_user_toml_has_token(isolated_global):
    _write_default_user_toml(isolated_global)
    # No env vars set (fixture clears them).

    diagnostic = diagnose_auth_config()

    codes = {w["code"] for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" not in codes


def test_warning_silent_when_only_ax_token_set(isolated_global, monkeypatch):
    # No user.toml on disk.
    monkeypatch.setenv("AX_TOKEN", "axp_u_env.secret")

    diagnostic = diagnose_auth_config()

    codes = {w["code"] for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" not in codes


def test_warning_silent_when_only_ax_user_token_set(isolated_global, monkeypatch):
    # No user.toml on disk.
    monkeypatch.setenv("AX_USER_TOKEN", "axp_u_env.secret")

    diagnostic = diagnose_auth_config()

    codes = {w["code"] for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" not in codes


def test_warning_silent_when_env_vars_are_whitespace(isolated_global, monkeypatch):
    _write_default_user_toml(isolated_global)
    monkeypatch.setenv("AX_TOKEN", "   ")
    monkeypatch.setenv("AX_USER_TOKEN", "   ")

    diagnostic = diagnose_auth_config()

    codes = {w["code"] for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" not in codes


def test_warning_names_named_env_path_when_env_selected(isolated_global, monkeypatch):
    named_path = _write_named_env_user_toml(isolated_global, env_name="dev")
    monkeypatch.setenv("AX_TOKEN", "axp_u_env.secret")

    diagnostic = diagnose_auth_config(env_name="dev")

    warnings = {w["code"]: w for w in diagnostic.get("warnings", [])}
    assert "user_pat_in_file_and_env" in warnings
    assert warnings["user_pat_in_file_and_env"]["path"] == str(named_path)
    _assert_honest_reason(warnings["user_pat_in_file_and_env"], named_path)
