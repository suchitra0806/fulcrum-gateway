"""ax gateway — login, session loading/guards, and upstream 429 retry.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import httpx
import typer

from .. import gateway as gateway_core
from ..client import AxClient
from ..commands import auth as auth_cmd
from ..config import resolve_space_id, resolve_user_token
from ..gateway import (
    gateway_dir,
    load_gateway_registry,
    load_gateway_session,
    record_gateway_activity,
    save_gateway_registry,
    save_gateway_session,
)
from ..output import JSON_OPTION, err_console, print_json
from .gateway_app import app, log


def _resolve_gateway_login_token(explicit_token: str | None) -> str:
    if explicit_token and explicit_token.strip():
        return auth_cmd._resolve_login_token(explicit_token)
    existing = resolve_user_token()
    if existing:
        err_console.print("[cyan]Using existing axctl user login for Gateway bootstrap.[/cyan]")
        return existing
    return auth_cmd._resolve_login_token(None)


def _warn_if_gateway_session_stale() -> None:
    """Warn when the gateway session PAT predates the user-login PAT.

    `ax login` writes `~/.ax/user.toml`; `ax gateway login` writes
    `~/.ax/gateway/session.json`. They're independent stores, so a PAT
    rotation refreshed via `ax login` leaves the gateway session pointing
    at a revoked token — failures show up only when a gateway command later
    hits `/auth/exchange` and gets 401 (see #74, and #73 for the
    raw-traceback UX of that failure).

    File mtime is a coarse signal but a reliable one here: there's no
    in-process reason for user.toml to be newer than session.json other
    than the user re-logging-in / rotating the user PAT.

    The session and the user login resolve through *different* environment
    scoping (see #80): `session_path()` scopes via `gateway_environment()`
    (`AX_GATEWAY_ENV`, ignores the active-env marker), while `_user_config_path()`
    scopes via `_resolve_user_env()` (consults `AX_USER_ENV`/`AX_ENV` and the
    active marker). When those disagree the two paths point at *different*
    environments' files, so an mtime comparison would pair the session against
    an unrelated `user.toml` and false-positive. In that case we can't make a
    trustworthy comparison, so skip silently rather than cry wolf.

    Fails closed silently — never raises, never blocks the command — so a
    `stat()` error, missing user.toml (different env), or an unexpected
    filesystem edge case can't break gateway commands themselves.
    """
    try:
        from ..config import _user_config_path

        session_p = gateway_core.session_path()
        user_p = _user_config_path()
        # Only compare when both stores resolve to the same environment. The
        # user.toml the gateway env *would* use must match the one the user-env
        # scoping picked; otherwise the two paths are unrelated (see #80).
        gateway_user_p = _user_config_path(gateway_core.gateway_environment() or "default")
        if gateway_user_p != user_p:
            return
        if not session_p.exists() or not user_p.exists():
            return
        if session_p.stat().st_mtime < user_p.stat().st_mtime:
            err_console.print(
                "[yellow]Warning:[/yellow] gateway session is older than your user login "
                "— run `ax gateway login` to refresh."
            )
    except Exception:
        return


def _divergence_marker_path() -> Path:
    return gateway_dir() / "divergence_warned"


def _warn_if_gateway_space_divergent() -> None:
    """Warn when the Gateway session's space differs from the CLI's space.

    `ax spaces use` now syncs both stores (issue #82), but divergence can still
    exist from a CLI that predates that fix, a hand-edited config, or a session
    written from a different working directory. A mismatch makes
    `ax gateway agents add` target a different space than the operator set,
    surfacing as a cryptic 400 from /api/v1/keys ("Agent IDs not found in this
    space").

    Warn once per distinct divergence state, not on every command (issue #159):
    operators who intentionally keep a project-local CLI space differing from the
    single global Gateway session would otherwise see this on every gateway
    command. We record the warned `session|cli` pair in a marker file and stay
    quiet until that pair changes; re-alignment clears the marker so a later
    divergence warns again. The early signal is preserved; the repetition is not.

    Reads only local config — `_load_config()` merges TOML files with no network
    call. Best-effort and fails closed silently: never raises, never blocks.
    """
    try:
        # Intentional private-symbol import: no public accessor exposes the
        # merged config's space_id, and this read is deliberately the same
        # local TOML merge the runtime client uses. Kept private on purpose
        # rather than widening config.py's public surface for one caller (#162).
        from ..config import _load_config

        session_space = str(load_gateway_session().get("space_id") or "").strip()
        cli_space = str(_load_config().get("space_id") or "").strip()
        marker = _divergence_marker_path()

        if not (session_space and cli_space and session_space != cli_space):
            # Aligned (or not enough info to judge): drop any prior marker so a
            # future divergence is surfaced again.
            marker.unlink(missing_ok=True)
            return

        state_key = f"{session_space}|{cli_space}"
        try:
            already_warned = marker.read_text(encoding="utf-8").strip() == state_key
        except OSError:
            already_warned = False
        if already_warned:
            return

        err_console.print(
            f"[yellow]Warning:[/yellow] Gateway space ({session_space}) differs from your "
            f"CLI space ({cli_space}) — run `ax spaces use <space>` to sync both."
        )
        try:
            marker.write_text(state_key, encoding="utf-8")
        except OSError:
            pass
    except Exception:
        # Fail-soft: a divergence-check bug must never break the command. Log at
        # debug so a swallowed programming error (NameError/AttributeError from a
        # future refactor) is still visible to maintainers under -v (issue #160).
        log.debug("gateway space-divergence check failed", exc_info=True)
        return


class GatewaySessionRejectedError(RuntimeError):
    """The gateway session PAT was rejected during token exchange.

    The gateway session token (``~/.ax/gateway/session.json``) is exchanged
    for a JWT lazily, on the first authenticated upstream call. When that PAT
    has been rotated or revoked, ``/auth/exchange`` returns 401/403 from deep
    inside httpx — past every ``_load_gateway_user_client`` caller's local
    error handling. Surfacing a typed error instead of the raw
    ``httpx.HTTPStatusError`` lets ``main()`` print an actionable
    "run ``ax gateway login``" message rather than a Rich traceback (#73).
    """


def _guard_gateway_exchange(client: AxClient) -> None:
    """Convert an exchange-boundary 401/403 into GatewaySessionRejectedError.

    The gateway session PAT is exchanged for a JWT inside AxClient's single
    auth boundary (``_get_jwt``). Wrapping that one method catches a rejected
    session PAT regardless of which command triggered the exchange. Wrapping
    the constructed instance (rather than subclassing ``AxClient``) keeps the
    ``AxClient`` construction seam intact for callers that swap it for a
    double; a double without ``_get_jwt`` is left untouched.
    """
    original = getattr(client, "_get_jwt", None)
    if not callable(original):
        return

    def guarded(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            url = str(exc.request.url) if exc.request is not None else ""
            if response is not None and response.status_code in (401, 403) and "/auth/exchange" in url:
                raise GatewaySessionRejectedError() from exc
            raise

    client._get_jwt = guarded


def _load_gateway_user_client() -> AxClient:
    if os.environ.get("AX_OFFLINE"):
        from ax_cli.offline_client import OfflineAxClient

        return OfflineAxClient()

    session = load_gateway_session()
    if not session:
        err_console.print("[red]Gateway is not logged in.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    token = str(session.get("token") or "")
    if not token:
        err_console.print("[red]Gateway session is missing its bootstrap token.[/red]")
        raise typer.Exit(1)
    if not token.startswith("axp_u_"):
        err_console.print("[red]Gateway bootstrap currently requires a user PAT (axp_u_).[/red]")
        raise typer.Exit(1)
    _warn_if_gateway_session_stale()
    _warn_if_gateway_space_divergent()
    client = AxClient(base_url=str(session.get("base_url") or auth_cmd.DEFAULT_LOGIN_BASE_URL), token=token)
    _guard_gateway_exchange(client)
    return client


def _load_gateway_session_or_exit() -> dict:
    if os.environ.get("AX_OFFLINE"):
        _gw_url = os.environ.get("AX_LOCAL_GATEWAY_URL") or "http://localhost:8765"
        return {"token": "offline", "base_url": _gw_url, "space_id": "00000000-0000-0000-0000-000000000001"}

    session = load_gateway_session()
    if not session:
        err_console.print("[red]Gateway is not logged in.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    return session


def _wipe_ephemeral_session_if_marked() -> Path | None:
    # Called after a successful agent mint. If the operator ran
    # `ax gateway login --no-persist`, the user PAT was meant to live on disk
    # only long enough to mint one agent — delete session.json now so the raw
    # axp_u_* token doesn't linger in the (often bind-mounted) state dir.
    # Best-effort: a failure here must not mask the successful mint.
    try:
        session = load_gateway_session()
    except Exception:
        return None
    if not session or not bool(session.get("ephemeral")):
        return None
    path = gateway_core.session_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    record_gateway_activity("gateway_session_wiped_ephemeral", session_path=str(path))
    return path


# ---------------------------------------------------------------------------
# Upstream rate-limit handling: retry with exponential backoff + structured
# error so operator-visible flows (Connect agent modal, CLI commands) degrade
# cleanly when paxai.app rate-limits us. Two retry budgets:
#   - Interactive (Connect agent modal, CLI invocations): 2 retries × 1s/2s
#     base_wait → ~3s ceiling so the operator's UI doesn't hang.
#   - Background (reconcile loop, cache refresh): 5 retries × exponential.
# ---------------------------------------------------------------------------

INTERACTIVE_429_MAX_RETRIES = 2
INTERACTIVE_429_BASE_WAIT = 1.0
BACKGROUND_429_MAX_RETRIES = 5
BACKGROUND_429_BASE_WAIT = 1.0


class UpstreamRateLimitedError(RuntimeError):
    """Raised when an upstream call returned 429 even after retries.

    Carries the original ``httpx.HTTPStatusError`` plus a parsed
    ``retry_after_seconds`` (from the Retry-After header, when present)
    so callers can surface operator-actionable guidance without having
    to re-parse the upstream response.
    """

    def __init__(self, last_exc: httpx.HTTPStatusError, retries_attempted: int) -> None:
        self.last_exc = last_exc
        self.retries_attempted = retries_attempted
        retry_after: int | None = None
        try:
            response = last_exc.response
            header_value = response.headers.get("retry-after") if response is not None else None
            if header_value:
                retry_after = int(float(header_value))
        except (ValueError, AttributeError, TypeError):
            retry_after = None
        self.retry_after_seconds = retry_after
        super().__init__(f"Upstream rate-limited after {retries_attempted} retries")


def _with_upstream_429_retry(
    call,
    *,
    max_retries: int,
    base_wait: float = 1.0,
    max_wait: float = 120.0,
):
    """Run ``call`` and retry on httpx 429, honoring ``Retry-After`` when present.

    Per-attempt wait = ``max(base_wait * 2**attempt, retry_after_seconds)``,
    capped at ``max_wait``. paxai.app sends ``Retry-After: <seconds>`` on its
    per-user rate-limit responses; ignoring it and falling back to a 1s/2s
    exponential backoff exhausts the retry budget far below the server's
    cooldown and surfaces as a spurious ``UpstreamRateLimitedError``.

    Other httpx exceptions (4xx/5xx that aren't 429, network errors) propagate
    immediately. After the configured retry budget is exhausted on a
    persistent 429, raises ``UpstreamRateLimitedError`` carrying the
    final exception.
    """
    attempts = 0
    while True:
        try:
            return call()
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 429:
                raise
            if attempts >= max_retries:
                raise UpstreamRateLimitedError(exc, attempts) from exc
            retry_after_raw = exc.response.headers.get("retry-after")
            try:
                hint = float(retry_after_raw) if retry_after_raw is not None else 0.0
            except (TypeError, ValueError):
                hint = 0.0
            exp = base_wait * (2**attempts)
            wait = min(max(exp, hint), max_wait)
            time.sleep(wait)
            attempts += 1


def _resolve_gateway_login_base_url(explicit: str | None) -> str:
    """Resolve the base URL for `ax gateway login`.

    Explicit `--url` wins. Otherwise prefer the user's existing axctl
    session (`AX_USER_BASE_URL` env or the `base_url` field from the
    axctl user config). Fall back to the documented default
    `https://paxai.app` rather than the local-dev `http://localhost:8001`
    that the broader `resolve_user_base_url()` would surface, matching
    the `--url` help text. Closes #129.
    """
    if explicit:
        return explicit
    from ..config import _load_user_config

    user_cfg = _load_user_config()
    env_url = os.environ.get("AX_USER_BASE_URL", "").strip()
    cfg_url = str(user_cfg.get("base_url") or "").strip()
    return env_url or cfg_url or auth_cmd.DEFAULT_LOGIN_BASE_URL


@app.command("login")
def login(
    token: str = typer.Option(
        None, "--token", "-t", help="User PAT (prompted or reused from axctl login when omitted)"
    ),
    base_url: str = typer.Option(
        None, "--url", "-u", help="API base URL (defaults to existing axctl login or paxai.app)"
    ),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Optional default space for managed agents"),
    no_persist: bool = typer.Option(
        False,
        "--no-persist",
        help=(
            "Mark the session ephemeral: session.json is deleted automatically after "
            "the first successful `agents add`. Use this for containerized setups "
            "where the user PAT should not linger on disk beyond the one mint it's "
            "needed for. See issue #87."
        ),
    ),
    as_json: bool = JSON_OPTION,
):
    """Store the Gateway bootstrap session.

    The Gateway keeps the user PAT centrally and uses it to mint agent PATs for
    managed runtimes. Managed runtimes themselves never receive the PAT or JWT.
    """
    resolved_token = _resolve_gateway_login_token(token)
    if not resolved_token.startswith("axp_u_"):
        err_console.print("[red]Gateway bootstrap requires a user PAT (axp_u_).[/red]")
        raise typer.Exit(1)
    resolved_base_url = _resolve_gateway_login_base_url(base_url)

    err_console.print(f"[cyan]Verifying Gateway login against {resolved_base_url}...[/cyan]")
    from ..token_cache import TokenExchanger

    try:
        exchanger = TokenExchanger(resolved_base_url, resolved_token)
        exchanger.get_token(
            "user_access",
            scope="messages tasks context agents spaces search",
            force_refresh=True,
        )
        client = AxClient(base_url=resolved_base_url, token=resolved_token)
        me = client.whoami()
    except Exception as exc:
        err_console.print(f"[red]Gateway login failed:[/red] {exc}")
        raise typer.Exit(1)

    selected_space = space_id
    selected_space_name = None
    if not selected_space:
        try:
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            selected = auth_cmd._select_login_space([s for s in space_list if isinstance(s, dict)])
            if selected:
                selected_space = auth_cmd._candidate_space_id(selected)
                selected_space_name = str(selected.get("name") or selected_space)
        except Exception:
            selected_space = None
    elif selected_space:
        try:
            selected_space = resolve_space_id(client, explicit=selected_space)
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            selected_space_name = next(
                (
                    str(item.get("name") or selected_space)
                    for item in space_list
                    if isinstance(item, dict) and auth_cmd._candidate_space_id(item) == selected_space
                ),
                None,
            )
        except Exception:
            selected_space_name = None

    from .. import __version__ as _gw_version

    gateway_id = None
    gateway_name = f"gateway-{socket.gethostname()}"
    try:
        gw_result = client.register_gateway(gateway_name, version=_gw_version)
        gateway_id = gw_result.get("id")
        err_console.print(f"[green]Registered gateway {gateway_name}[/green] (id={gateway_id})")
    except Exception as exc:
        err_console.print(f"[yellow]Gateway registration skipped:[/yellow] {exc}")

    payload = {
        "token": resolved_token,
        "base_url": resolved_base_url,
        "principal_type": "user",
        "space_id": selected_space,
        "space_name": selected_space_name,
        "username": me.get("username"),
        "email": me.get("email"),
        "gateway_id": gateway_id,
        "gateway_name": gateway_name,
        "saved_at": None,
    }
    if no_persist:
        payload["ephemeral"] = True
    path = save_gateway_session(payload)
    registry = load_gateway_registry()
    registry.setdefault("gateway", {})
    registry["gateway"]["session_connected"] = True
    registry["gateway"]["gateway_id"] = gateway_id
    save_gateway_registry(registry)
    record_gateway_activity(
        "gateway_login",
        username=me.get("username"),
        base_url=resolved_base_url,
        space_id=selected_space,
        gateway_id=gateway_id,
    )

    result = {
        "session_path": str(path),
        "base_url": resolved_base_url,
        "space_id": selected_space,
        "space_name": selected_space_name,
        "username": me.get("username"),
        "email": me.get("email"),
        "gateway_id": gateway_id,
    }
    if as_json:
        print_json(result)
    else:
        err_console.print(f"[green]Gateway login saved:[/green] {path}")
        for key, value in result.items():
            err_console.print(f"  {key} = {value}")
