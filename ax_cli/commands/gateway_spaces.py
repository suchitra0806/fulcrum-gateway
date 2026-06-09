"""ax gateway — space resolution/cache helpers and the `spaces` sub-app.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import typer

from ..client import AxClient
from ..commands import auth as auth_cmd
from ..config import resolve_space_id
from ..gateway import (
    apply_entry_current_space,
    apply_space_to_gateway_session,
    load_gateway_session,
    load_space_cache,
    looks_like_space_uuid,
    lookup_space_in_cache,
    save_gateway_registry,
    save_space_cache,
    space_name_from_cache,
)
from ..output import JSON_OPTION, err_console, print_json, print_table
from .gateway_app import spaces_app


def _resolve_space_via_cache(value: str | None) -> str | None:
    """Cache-only space resolver for the pass-through (`local_*`) commands.

    Pass-through agents must not need the user PAT, so we cannot fall back
    to a fresh `client.list_spaces()` here — that would defeat the trust
    boundary. The on-disk space cache (populated by any prior user-side
    Gateway command) is the authoritative source on the agent side.

    Returns the canonical UUID for a slug or name when found, the original
    UUID-like input verbatim, or ``None`` if neither (caller decides whether
    to error or pass through).

    This intentionally diverges from `config.resolve_space_id()`, which
    requires an authoring client and falls back to upstream `list_spaces`.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # UUID-like passes through unchanged.
    try:
        from uuid import UUID

        UUID(raw)
        return raw
    except ValueError:
        pass
    cached = lookup_space_in_cache(raw)
    if cached:
        sid = str(cached.get("id") or cached.get("space_id") or "").strip()
        if sid:
            return sid
    return None


def _agent_row_space_ids(registry: dict) -> set[str]:
    return {
        str(item.get("space_id") or "").strip()
        for item in registry.get("agents", [])
        if isinstance(item, dict) and str(item.get("space_id") or "").strip()
    }


def _space_list_from_response(raw: object) -> list[dict]:
    items = raw.get("spaces", raw) if isinstance(raw, dict) else raw
    return [item for item in (items or []) if isinstance(item, dict)]


def _space_name_for_id(client: AxClient, space_id: str) -> str | None:
    """Friendly-name lookup with persistent-cache short-circuit.

    Hits the local space cache first so we don't pay an upstream `list_spaces`
    call (and risk a 429) for a name we already know. Only falls through to
    upstream when the cache has no entry for this id, and refreshes the cache
    on a successful fetch so future calls stay in-process.
    """
    cached = space_name_from_cache(space_id)
    if cached:
        return cached
    try:
        rows = _space_list_from_response(client.list_spaces())
    except Exception:
        return None
    refreshed: list[dict] = []
    match: str | None = None
    for item in rows:
        sid = auth_cmd._candidate_space_id(item)
        if not sid:
            continue
        name = str(item.get("name") or item.get("slug") or sid)
        slug = str(item.get("slug") or "").strip() or None
        refreshed.append({"id": sid, "name": name, "slug": slug})
        if sid == space_id:
            match = name
    if refreshed:
        save_space_cache(refreshed)
    return match


def _resolve_gateway_agent_home_space(
    *,
    client: AxClient,
    session: dict,
    registry: dict,
    explicit_space_id: str | None = None,
) -> str:
    explicit = str(explicit_space_id or "").strip()
    if explicit:
        if looks_like_space_uuid(explicit):
            return explicit
        # Caller passed a name/slug — resolve through the backend so we never
        # store a non-UUID in the registry's space_id field.
        return resolve_space_id(client, explicit=explicit)
    session_space = str(session.get("space_id") or "").strip()
    if session_space:
        return session_space

    row_spaces = _agent_row_space_ids(registry)
    if len(row_spaces) == 1:
        return next(iter(row_spaces))

    try:
        selected = auth_cmd._select_login_space(_space_list_from_response(client.list_spaces()))
        selected_id = auth_cmd._candidate_space_id(selected or {})
        if selected_id:
            return selected_id
    except Exception:
        pass

    if len(row_spaces) > 1:
        raise ValueError(
            "Multiple agent spaces are present. Pick a home space once with --space-id, "
            "or move an existing agent row to the intended space."
        )
    raise ValueError(
        "No agent home space could be inferred. Pick a home space once with --space-id; "
        "after the agent row exists, Gateway will use the row's space_id."
    )


def _agent_space_id_from_backend_record(agent: dict) -> str | None:
    """Return the backend-owned current/default space for an agent row.

    Prefer the current row placement (`space_id`) over defaults so a Gateway
    local client that omits --space-id follows the database after a user moves
    the agent between spaces.
    """
    raw_current = agent.get("current_space")
    current_space_id = ""
    if isinstance(raw_current, dict):
        current_space_id = str(raw_current.get("space_id") or raw_current.get("id") or "").strip()
    elif raw_current:
        current_space_id = str(raw_current).strip()
    return (
        current_space_id
        or str(agent.get("active_space_id") or "").strip()
        or str(agent.get("space_id") or "").strip()
        or str(agent.get("default_space_id") or "").strip()
        or None
    )


def _agent_space_name_from_backend_record(agent: dict, space_id: str | None) -> str | None:
    raw_current = agent.get("current_space")
    if isinstance(raw_current, dict):
        current_id = str(raw_current.get("space_id") or raw_current.get("id") or "").strip()
        if not space_id or current_id == space_id:
            return str(raw_current.get("name") or raw_current.get("space_name") or "").strip() or None
    return (
        str(agent.get("space_name") or agent.get("active_space_name") or agent.get("default_space_name") or "").strip()
        or None
    )


def _backend_agent_record(client: AxClient, name: str) -> dict | None:
    """Look up an agent by name on the upstream backend.

    Falls back to the local agents cache when upstream is unavailable
    (e.g. paxai.app rate-limits us). Successful upstream responses
    seed/refresh the cache so the next failure has stale-but-usable
    data to serve.
    """
    agents: list[dict] = []
    try:
        agents_data = client.list_agents()
        agents = agents_data if isinstance(agents_data, list) else (agents_data or {}).get("agents", []) or []
        if agents:
            _save_agents_cache([a for a in agents if isinstance(a, dict)])
    except Exception:
        # Upstream unavailable — fall back to last-good cache.
        agents = _load_agents_cache()
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if str(agent.get("name") or "") != name:
            continue
        return agent
    return None


def _existing_agent_home_space(client: AxClient, name: str) -> str | None:
    agent = _backend_agent_record(client, name)
    if not agent:
        return None
    return _agent_space_id_from_backend_record(agent)


def _hydrate_entry_space_from_database(registry: dict, entry: dict) -> str | None:
    """Refresh an existing registry entry's space from the backend agent row."""
    name = str(entry.get("name") or "").strip()
    if not name:
        return None
    try:
        agent = _backend_agent_record(_load_gateway_user_client(), name)
    except Exception:
        return None
    if not agent:
        return None
    space_id = _agent_space_id_from_backend_record(agent)
    if not space_id:
        return None
    space_name = _agent_space_name_from_backend_record(agent, space_id)
    apply_entry_current_space(entry, space_id, space_name=space_name, make_default=False)
    if str(agent.get("default_space_id") or "").strip():
        entry["default_space_id"] = str(agent.get("default_space_id") or "").strip()
    if str(agent.get("id") or agent.get("agent_id") or "").strip():
        entry["agent_id"] = str(agent.get("id") or agent.get("agent_id") or "").strip()
    save_gateway_registry(registry)
    return space_id


def _normalize_spaces_response(items: list) -> list[dict]:
    """Normalize an upstream `list_spaces` response into [{id, name, slug}].

    If a row arrives with an empty/missing name (we've seen this happen for
    brand-new spaces), fall back to the local cache before defaulting to the
    UUID — avoids the "raw UUID rendered in picker" symptom for any space the
    operator has seen at least once.
    """
    spaces: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        space_id = str(item.get("id") or item.get("space_id") or "").strip()
        if not space_id:
            continue
        upstream_name = str(item.get("name") or item.get("space_name") or "").strip()
        cached_name = space_name_from_cache(space_id) if not upstream_name else None
        spaces.append(
            {
                "id": space_id,
                "name": upstream_name or cached_name or space_id,
                "slug": str(item.get("slug") or "").strip() or None,
            }
        )
    return spaces


def _spaces_payload() -> dict:
    """Return the spaces visible to the Gateway bootstrap session.

    Always surfaces ``active_space_id`` / ``active_space_name`` from session
    state, even when the upstream ``list_spaces`` call fails (e.g. paxai.app
    rate-limits). Successful upstream responses are cached on disk so the UI
    keeps a usable picker through transient outages.
    """
    session = load_gateway_session() or {}
    active_space_id = str(session.get("space_id") or "").strip() or None
    active_space_name = str(session.get("space_name") or "").strip() or None

    error: str | None = None
    cached = False
    try:
        client = _load_gateway_user_client()
        raw = client.list_spaces()
        items = raw.get("spaces", raw) if isinstance(raw, dict) else raw
        spaces = _normalize_spaces_response(items or [])
        if spaces:
            save_space_cache(spaces)
    except Exception as exc:  # noqa: BLE001 — upstream errors are routine here
        error = str(exc)
        spaces = load_space_cache()
        cached = bool(spaces)

    if active_space_id and not any(s["id"] == active_space_id for s in spaces):
        spaces = [
            {"id": active_space_id, "name": active_space_name or active_space_id, "slug": None},
            *spaces,
        ]

    payload: dict = {
        "spaces": spaces,
        "active_space_id": active_space_id,
        "active_space_name": active_space_name,
    }
    if error:
        payload["error"] = error
        payload["cached"] = cached
    return payload


@spaces_app.command("use")
def use_gateway_space(
    space: str = typer.Argument(..., help="Space id, slug, or name to make current"),
    global_config: bool = typer.Option(
        False, "--global", help="Save the CLI space to global config instead of local .ax/config.toml"
    ),
    as_json: bool = JSON_OPTION,
):
    """Set the current space for both the Gateway session and the CLI.

    Alias of `ax spaces use` — both commands now write both stores so the
    Gateway session and CLI config can't silently diverge (issue #82).
    """
    from ..config import save_space_id

    _load_gateway_session_or_exit()
    client = _load_gateway_user_client()
    sid = resolve_space_id(client, explicit=space)
    space_name = _space_name_for_id(client, sid)
    gw_sync = apply_space_to_gateway_session(sid, space_name=space_name)
    # Sync the CLI config store too, so `ax send` / runtime resolution agree
    # with the Gateway session.
    save_space_id(sid, local=not global_config)
    session_path_str = gw_sync.get("session_path") if gw_sync else None
    result = {
        "session_path": session_path_str,
        "space_id": sid,
        "space_name": space_name,
        "cli_scope": "global" if global_config else "local",
        "gateway_session": gw_sync,
    }
    if as_json:
        print_json(result)
        return
    err_console.print(f"[green]Current space:[/green] {space_name or sid} ({sid})")
    if session_path_str:
        err_console.print(f"  session = {session_path_str}")
    err_console.print(f"  cli config = {'global' if global_config else 'local .ax/config.toml'}")
    if gw_sync and gw_sync.get("updated") and gw_sync.get("daemon_running"):
        err_console.print(
            "[yellow]Warning:[/yellow] Gateway daemon is running — restart it "
            "(`ax gateway stop && ax gateway start`) to apply the new space."
        )
    err_console.print("[dim]Tip: `ax spaces use` now sets both CLI and Gateway space.[/dim]")


@spaces_app.command("current")
def current_gateway_space(as_json: bool = JSON_OPTION):
    """Show the Gateway bootstrap session's current space."""
    session = _load_gateway_session_or_exit()
    result = {
        "space_id": session.get("space_id"),
        "space_name": session.get("space_name"),
        "base_url": session.get("base_url"),
        "username": session.get("username"),
    }
    if as_json:
        print_json(result)
        return
    err_console.print(f"Gateway current space: {result.get('space_name') or result.get('space_id') or '-'}")
    err_console.print(f"  space_id = {result.get('space_id') or '-'}")


@spaces_app.command("list")
def list_gateway_spaces(as_json: bool = JSON_OPTION):
    """List the spaces visible to the Gateway bootstrap session.

    Falls back to the locally cached list when the upstream API is
    unavailable (e.g. rate-limited), so the operator always sees something
    actionable.
    """
    payload = _spaces_payload()
    if as_json:
        print_json(payload)
        return

    spaces = payload.get("spaces") or []
    active_id = payload.get("active_space_id")
    if not spaces:
        err_console.print("[yellow]No spaces available.[/yellow]")
        if payload.get("error"):
            err_console.print(f"  error = {payload['error']}")
        return

    rows = []
    for space in spaces:
        sid = str(space.get("id") or "")
        rows.append(
            {
                "current": "*" if sid and sid == active_id else "",
                "name": str(space.get("name") or sid),
                "space_id": sid,
                "slug": str(space.get("slug") or "") or "-",
            }
        )
    print_table(
        ["", "Name", "Space ID", "Slug"],
        rows,
        keys=["current", "name", "space_id", "slug"],
    )
    if payload.get("error"):
        marker = "cached" if payload.get("cached") else "session-only"
        err_console.print(f"[dim]Upstream unavailable ({marker}): {payload['error']}[/dim]")


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from .gateway_agents import _load_agents_cache, _save_agents_cache  # noqa: E402
from .gateway_auth import _load_gateway_session_or_exit, _load_gateway_user_client  # noqa: E402
