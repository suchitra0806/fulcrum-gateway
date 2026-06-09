"""ax gateway — outbound tool connector commands (`connectors` sub-app).

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import typer

from ..output import JSON_OPTION, err_console, print_json, print_table
from .gateway_app import connectors_app, connectors_auth_app, connectors_tools_app

# ── Connector commands ────────────────────────────────────────────────────────


@connectors_app.command("list")
def connectors_list(as_json: bool = JSON_OPTION):
    """List registered outbound tool connectors."""
    from ..connectors import list_connectors

    rows = list_connectors()
    if as_json:
        print_json([r.to_dict() for r in rows])
        return
    if not rows:
        err_console.print(
            "No connectors registered. Run: ax gateway connectors add <name> --provider composio --managed-auth"
        )
        return
    print_table(
        ["Name", "Provider", "Enabled", "Auth", "ID"],
        [r.to_dict() for r in rows],
        keys=["name", "provider", "enabled", "auth_ref", "id"],
    )


@connectors_app.command("show")
def connectors_show(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """Show connector details (auth key names only, never values)."""
    from ..connectors import ConnectorNotFoundError, auth_status, find_connector

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    payload = row.to_dict()
    if row.auth_ref:
        payload["auth_status"] = auth_status(row.id, row.name)
    if as_json:
        print_json(payload)
        return
    err_console.print(f"[bold]{row.name}[/bold]  ({row.provider})")
    err_console.print(f"  id       = {row.id}")
    err_console.print(f"  enabled  = {row.enabled}")
    err_console.print(f"  auth_ref = {row.auth_ref or '(none)'}")
    if row.config:
        err_console.print("  config:")
        for k, v in sorted(row.config.items()):
            err_console.print(f"    {k} = {v}")
    if "auth_status" in payload:
        a = payload["auth_status"]
        if a.get("exists"):
            err_console.print(f"  auth keys = {', '.join(a['keys']) or '(empty)'}")
            err_console.print(f"  auth permissions = {a.get('permissions', '?')}")
        else:
            err_console.print("  auth = [yellow]not configured[/yellow]")


@connectors_app.command("add")
def connectors_add(
    name: str = typer.Argument(..., help="Connector name (human-readable, unique)"),
    provider: str = typer.Option(..., "--provider", "-p", help="Provider type (e.g. composio)"),
    managed_auth: bool = typer.Option(False, "--managed-auth", help="Create managed auth env file"),
    as_json: bool = JSON_OPTION,
):
    """Register a new outbound tool connector."""
    from ..connectors import ConnectorRow, add_connector, validate_new_connector
    from ..connectors.errors import ConnectorError
    from ..connectors.providers.registry import get_provider

    provider_info = get_provider(provider)
    config = dict(provider_info["default_config"]) if provider_info else {}
    row = ConnectorRow.create(name, provider, managed_auth=managed_auth, config=config)
    try:
        validate_new_connector(row)
    except ConnectorError as e:
        err_console.print(f"[red]Validation error:[/red] {e}")
        raise typer.Exit(1)
    add_connector(row)
    if as_json:
        print_json(row.to_dict())
        return
    err_console.print(f"[green]Added connector:[/green] {row.name} (provider={row.provider}, id={row.id})")
    if managed_auth:
        err_console.print(f"  Next: ax gateway connectors auth write {name} COMPOSIO_API_KEY=<key>")


@connectors_app.command("remove")
def connectors_remove(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """Remove a connector and clean up its auth file."""
    from ..connectors import ConnectorNotFoundError, cleanup_auth, remove_connector

    try:
        removed = remove_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if removed.auth_ref:
        cleanup_auth(removed.id)
    if as_json:
        print_json({"removed": removed.to_dict()})
        return
    err_console.print(f"[green]Removed connector:[/green] {removed.name}")


@connectors_app.command("set")
def connectors_set(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    key: str = typer.Argument(..., help="Configuration key (e.g. entity_id, composio_base_url)"),
    value: str = typer.Argument(..., help="Configuration value"),
    as_json: bool = JSON_OPTION,
):
    """Set a connector configuration value."""
    from ..connectors import ConnectorNotFoundError, find_connector, update_connector
    from ..connectors.constants import KEY_TOOLS_LIMIT, MAX_TOOLS_LIMIT

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    config = dict(row.config)
    _POLICY_LIST_KEYS = {"allowed_tools", "denied_tools", "allowed_toolkits", "denied_toolkits"}
    if key in _POLICY_LIST_KEYS:
        import json as _json

        from ..connectors.filtering import validate_policy_patterns

        try:
            parsed = _json.loads(value)
            if isinstance(parsed, list):
                value = parsed
            else:
                value = [str(parsed)]
        except _json.JSONDecodeError:
            value = [v.strip() for v in value.split(",") if v.strip()]
        try:
            validate_policy_patterns({key: value})
        except ValueError as exc:
            err_console.print(f"[red]Invalid policy pattern:[/red] {exc}")
            raise typer.Exit(1)
    elif key == KEY_TOOLS_LIMIT:
        # from_config clamps a too-high limit to MAX_TOOLS_LIMIT at evaluation
        # time, but silently — so `set tools_limit 500` would look accepted yet
        # enforce 200. Surface the cap here (closest to the action) and persist
        # the clamped value so stored config matches what's actually enforced.
        # The ceiling is read from the constant so the message can't drift.
        try:
            requested = int(value)
        except (TypeError, ValueError):
            requested = None
        if requested is not None and requested > MAX_TOOLS_LIMIT:
            err_console.print(
                f"[yellow]tools_limit {requested} exceeds the maximum of "
                f"{MAX_TOOLS_LIMIT}; capping to {MAX_TOOLS_LIMIT}.[/yellow]"
            )
            value = MAX_TOOLS_LIMIT
    config[key] = value
    updated = update_connector(ref, {"config": config})
    if as_json:
        print_json(updated.to_dict())
        return
    err_console.print(f"[green]Updated:[/green] {updated.name} config.{key} = {value}")


@connectors_app.command("enable")
def connectors_enable(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """Enable a connector so agents can use it."""
    from ..connectors import ConnectorNotFoundError, find_connector, update_connector

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if row.enabled:
        err_console.print(f"Connector {row.name!r} is already enabled.")
        return
    updated = update_connector(ref, {"enabled": True})
    if as_json:
        print_json(updated.to_dict())
        return
    err_console.print(f"[green]Enabled:[/green] {updated.name}")


@connectors_app.command("disable")
def connectors_disable(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """Disable a connector (agents cannot use it until re-enabled)."""
    from ..connectors import ConnectorNotFoundError, find_connector, update_connector

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if not row.enabled:
        err_console.print(f"Connector {row.name!r} is already disabled.")
        return
    updated = update_connector(ref, {"enabled": False})
    if as_json:
        print_json(updated.to_dict())
        return
    err_console.print(f"[yellow]Disabled:[/yellow] {updated.name}")


@connectors_app.command("call")
def connectors_call(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    tool: str = typer.Option(..., "--tool", "-t", help="Tool slug (e.g. GITHUB_LIST_PRS)"),
    args_json: str = typer.Option("{}", "--args-json", "-a", help="Tool arguments as JSON string"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show request payload without executing"),
    as_json: bool = JSON_OPTION,
):
    """Execute a tool via a connector's provider."""
    import json as _json

    from ..connectors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        ConnectorPolicyError,
        ConnectorProviderError,
        execute_tool,
        find_connector,
        read_auth,
    )

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if not row.enabled:
        err_console.print(f"[red]Connector {row.name!r} is disabled[/red]")
        raise typer.Exit(1)
    try:
        args = _json.loads(args_json)
    except _json.JSONDecodeError as e:
        err_console.print(f"[red]Invalid JSON in --args-json:[/red] {e}")
        raise typer.Exit(1)
    try:
        auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth error:[/red] {e}")
        raise typer.Exit(1)
    if dry_run:
        payload = {
            "connector": row.name,
            "provider": row.provider,
            "tool": tool,
            "args": args,
            "auth_keys": sorted(auth_env.keys()),
        }
        if as_json:
            print_json(payload)
        else:
            err_console.print("[bold]Dry run — would send:[/bold]")
            print_json(payload)
        return
    try:
        result = execute_tool(row, tool, args, auth_env)
    except ConnectorPolicyError as e:
        err_console.print(f"[red]Blocked by policy:[/red] {e}")
        raise typer.Exit(1)
    except ConnectorProviderError as e:
        err_console.print(f"[red]Provider error:[/red] {e}")
        raise typer.Exit(1)
    if as_json:
        print_json(result)
        return
    err_console.print(f"[bold]Result from {row.provider}/{tool}:[/bold]")
    print_json(result)


@connectors_app.command("providers")
def connectors_providers(as_json: bool = JSON_OPTION):
    """List available connector provider types."""
    from ..connectors.providers.registry import list_providers

    providers = list_providers()
    if as_json:
        print_json(providers)
        return
    for p in providers:
        caps = ", ".join(p.get("capabilities", []))
        err_console.print(f"[bold]{p['name']}[/bold] — {p['description']}")
        if caps:
            err_console.print(f"  Capabilities: {caps}")
        err_console.print(f"  Required auth: {', '.join(p['required_auth_keys']) or '(none)'}")
        if p.get("optional_auth_keys"):
            err_console.print(f"  Optional auth: {', '.join(p['optional_auth_keys'])}")


@connectors_app.command("apps")
def connectors_apps(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """List apps with active OAuth connections in the provider."""
    from ..connectors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        find_connector,
        list_apps,
        read_auth,
    )

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    try:
        auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth error:[/red] {e}")
        raise typer.Exit(1)
    from ..connectors.errors import ConnectorProviderError

    try:
        items = list_apps(row, auth_env)
    except ConnectorProviderError as e:
        err_console.print(f"[red]Provider error:[/red] {e}")
        raise typer.Exit(1)
    if as_json:
        print_json(
            [
                {"app": a.get("appName"), "status": a.get("status"), "entity_id": a.get("clientUniqueUserId")}
                for a in items
            ]
        )
        return
    if not items:
        err_console.print("No connected apps. Run: ax gateway connectors connect <ref> --app <app_name>")
        return
    for a in items:
        err_console.print(
            f"  [bold]{a.get('appName', '?')}[/bold]  status={a.get('status', '?')}  entity={a.get('clientUniqueUserId', '?')}"
        )


@connectors_app.command("connect")
def connectors_connect(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    app: str = typer.Option(..., "--app", "-a", help="App to connect (e.g. github, gmail, slack)"),
    as_json: bool = JSON_OPTION,
):
    """Initiate an OAuth connection for an app via the provider. Prints a URL to complete auth in a browser."""
    from ..connectors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        find_connector,
        initiate_connection,
        read_auth,
    )

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    try:
        auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth error:[/red] {e}")
        raise typer.Exit(1)
    entity_id = row.config.get("entity_id") or "default"
    from ..connectors.errors import ConnectorProviderError

    try:
        result = initiate_connection(row, app, entity_id, auth_env)
    except ConnectorProviderError as e:
        err_console.print(f"[red]Provider error:[/red] {e}")
        raise typer.Exit(1)
    if as_json:
        print_json(result)
        return
    status = result.get("connectionStatus") or result.get("status", "?")
    url = result.get("redirectUrl") or result.get("redirect_url", "")
    err_console.print(f"[bold]Connection status:[/bold] {status}")
    if url:
        err_console.print(f"[bold]Open this URL to authorize:[/bold] {url}")
    else:
        err_console.print("[green]App connected (no OAuth redirect needed).[/green]")


@connectors_app.command("search")
def connectors_search(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    query: str = typer.Option(..., "--query", "-q", help="Natural-language use case (e.g. 'send email')"),
    app: str = typer.Option(None, "--app", help="Filter by app name (e.g. github, gmail, slack)"),
    limit: int = typer.Option(5, "--limit", "-n", help="Max results"),
    as_json: bool = JSON_OPTION,
):
    """Search for available tools by use case."""
    from ..connectors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        find_connector,
        read_auth,
        search_tools,
    )

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if not row.enabled:
        err_console.print(f"[red]Connector {row.name!r} is disabled[/red]")
        raise typer.Exit(1)
    try:
        auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth error:[/red] {e}")
        raise typer.Exit(1)
    from ..connectors.errors import ConnectorProviderError

    try:
        result = search_tools(row, query, auth_env, apps=app, limit=limit)
    except ConnectorProviderError as e:
        err_console.print(f"[red]Provider error:[/red] {e}")
        raise typer.Exit(1)
    items = result.get("items", [])
    if as_json:
        print_json(items)
        return
    if not items:
        err_console.print(f"No tools found for query: {query!r}")
        return
    for item in items:
        slug = item.get("enum", item.get("name", "?"))
        display = item.get("displayName") or item.get("display_name") or ""
        app_id = item.get("appId", "")
        tags = item.get("tags", [])
        read_only = "readOnlyHint" in tags
        err_console.print(f"  [bold]{slug}[/bold]")
        err_console.print(f"    {display}")
        err_console.print(f"    app={app_id}  read_only={read_only}")
        err_console.print()


@connectors_auth_app.command("write")
def connectors_auth_write(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    kvs: list[str] = typer.Argument(..., help="KEY=VALUE pairs (e.g. COMPOSIO_API_KEY=ak_xxx)"),
    as_json: bool = JSON_OPTION,
):
    """Write managed auth credentials for a connector.

    Merges with existing keys — adding a new key does not remove others.

    Security note: KEY=VALUE args appear in shell history. For sensitive
    values, prefix with a space (most shells skip history) or use:
      export HISTCONTROL=ignorespace
    """
    from ..connectors import ConnectorNotFoundError, auth_status, find_connector, write_auth
    from ..connectors.errors import ConnectorAuthError

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if not row.auth_ref:
        err_console.print(
            f"[red]Connector {row.name!r} does not use managed auth.[/red] Re-create with --managed-auth."
        )
        raise typer.Exit(1)
    parsed: dict[str, str] = {}
    for kv in kvs:
        if "=" not in kv:
            err_console.print(f"[red]Invalid format:[/red] {kv!r} — expected KEY=VALUE")
            raise typer.Exit(1)
        k, _, v = kv.partition("=")
        k = k.strip()
        if not k:
            err_console.print(f"[red]Empty key in:[/red] {kv!r}")
            raise typer.Exit(1)
        parsed[k] = v
    try:
        write_auth(row.id, row.name, parsed)
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth write error:[/red] {e}")
        raise typer.Exit(1)
    status = auth_status(row.id, row.name)
    if as_json:
        print_json(status)
        return
    err_console.print(f"[green]Auth written for {row.name}:[/green] {', '.join(sorted(parsed.keys()))}")
    err_console.print(f"  Permissions: {status.get('permissions', '?')}")


@connectors_auth_app.command("status")
def connectors_auth_status(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """Show managed auth status (key names only, never values)."""
    from ..connectors import ConnectorNotFoundError, auth_status, find_connector

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    status = auth_status(row.id, row.name)
    if as_json:
        print_json(status)
        return
    if status.get("exists"):
        err_console.print(f"[bold]{row.name}[/bold] auth status:")
        err_console.print(f"  Keys: {', '.join(status['keys']) or '(empty)'}")
        err_console.print(f"  Permissions: {status.get('permissions', '?')}")
        err_console.print(f"  Last modified: {status.get('last_modified', '?')}")
        err_console.print(f"  Size: {status.get('size_bytes', '?')} bytes")
    else:
        err_console.print(f"[yellow]No auth configured for {row.name}.[/yellow]")
        err_console.print(f"  Run: ax gateway connectors auth write {row.name} COMPOSIO_API_KEY=<key>")


@connectors_auth_app.command("clear")
def connectors_auth_clear(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    as_json: bool = JSON_OPTION,
):
    """Remove managed auth credentials for a connector."""
    from ..connectors import ConnectorNotFoundError, cleanup_auth, find_connector

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    removed = cleanup_auth(row.id)
    if as_json:
        print_json({"connector": row.name, "auth_removed": removed})
        return
    if removed:
        err_console.print(f"[green]Auth removed for {row.name}[/green]")
    else:
        err_console.print(f"[yellow]No auth file found for {row.name}[/yellow]")


# ── connectors tools ─────────────────────────────────────────────────────────


@connectors_tools_app.command("list")
def connectors_tools_list(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    toolkit: str = typer.Option(None, "--toolkit", help="Filter by toolkit/app name"),
    limit: int = typer.Option(0, "--limit", help="Cap results (0 = use policy limit)"),
    as_json: bool = JSON_OPTION,
):
    """List tools available through a connector (filtered by policy)."""
    from ..connectors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        ConnectorProviderError,
        find_connector,
        list_tools,
        read_auth,
    )

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if not row.enabled:
        err_console.print(f"[red]Connector {row.name!r} is disabled[/red]")
        raise typer.Exit(1)
    try:
        auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth error:[/red] {e}")
        raise typer.Exit(1)
    try:
        result = list_tools(row, auth_env)
    except ConnectorProviderError as e:
        err_console.print(f"[red]Provider error:[/red] {e}")
        raise typer.Exit(1)

    items = result.get("items", [])
    if toolkit:
        toolkit_lower = toolkit.lower()
        items = [
            i
            for i in items
            if toolkit_lower in str(i.get("appName", "")).lower() or toolkit_lower in str(i.get("toolkit", "")).lower()
        ]
    if limit and limit > 0:
        items = items[:limit]

    matched = result.get("matched", len(items))
    clipped = bool(result.get("clipped"))
    policy_limit = result.get("limit")

    if as_json:
        print_json(
            {
                "connector": row.name,
                "provider": row.provider,
                "tools": items,
                "count": len(items),
                "matched": matched,
                "limit": policy_limit,
                "clipped": clipped,
            }
        )
        return
    if not items:
        err_console.print(f"No tools found for connector {row.name!r}.")
        return
    err_console.print(f"[bold]{row.name}[/bold] ({row.provider}) — {len(items)} tools:")
    if clipped:
        err_console.print(
            f"[yellow]Note:[/yellow] {matched} tools matched policy but only {policy_limit} are shown "
            f"(tools_limit={policy_limit}). Narrow with allowed_tools/allowed_toolkits, raise tools_limit, "
            f"or run `tools search` with a use case to find specific tools."
        )
    print_table(
        ["Name", "Display Name", "Description"],
        [
            {
                "name": str(i.get("name") or i.get("enum") or ""),
                "displayName": str(i.get("displayName") or ""),
                "description": str(i.get("description") or "")[:80],
            }
            for i in items
        ],
        keys=["name", "displayName", "description"],
    )


@connectors_tools_app.command("search")
def connectors_tools_search(
    ref: str = typer.Argument(..., help="Connector name or ID"),
    query: str = typer.Argument(
        None,
        help="Natural-language use case query (e.g. 'list github prs'). Required.",
    ),
    use_case: str = typer.Option(
        None,
        "--use-case",
        "-u",
        help="Deprecated alias for the positional QUERY argument; kept for backward compatibility.",
    ),
    mode: str = typer.Option("auto", "--mode", "-m", help="Search mode: auto, intent, or catalog"),
    limit: int = typer.Option(10, "--limit", help="Max results"),
    as_json: bool = JSON_OPTION,
):
    """Search for tools matching a use case (intent or catalog mode).

    The query goes as a positional argument:

        ax gateway connectors tools search <ref> "list github prs"

    ``--use-case`` is accepted as a deprecated alias and emits a hint when used
    alone. Passing both the positional QUERY and ``--use-case`` fails closed.
    """
    from ..connectors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        ConnectorProviderError,
        find_connector,
        read_auth,
        search_tools,
    )

    if query and use_case:
        err_console.print("[red]Pass the query as a positional argument or via --use-case, not both.[/red]")
        raise typer.Exit(1)
    if not query and not use_case:
        err_console.print(
            "[red]Missing query.[/red] Pass it as the second positional argument: "
            '[cyan]ax gateway connectors tools search <ref> "<query>"[/cyan]'
        )
        raise typer.Exit(1)
    if use_case and not query:
        err_console.print("[yellow]--use-case is deprecated;[/yellow] pass the query as a positional argument instead.")
        query = use_case
    use_case = query

    if mode not in ("auto", "intent", "catalog"):
        err_console.print(f"[red]Invalid mode:[/red] {mode!r}. Use auto, intent, or catalog.")
        raise typer.Exit(1)

    try:
        row = find_connector(ref)
    except ConnectorNotFoundError:
        err_console.print(f"[red]Connector not found:[/red] {ref}")
        raise typer.Exit(1)
    if not row.enabled:
        err_console.print(f"[red]Connector {row.name!r} is disabled[/red]")
        raise typer.Exit(1)
    try:
        auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
    except ConnectorAuthError as e:
        err_console.print(f"[red]Auth error:[/red] {e}")
        raise typer.Exit(1)
    try:
        result = search_tools(row, use_case, auth_env, limit=limit, mode=mode)
    except ConnectorProviderError as e:
        err_console.print(f"[red]Provider error:[/red] {e}")
        raise typer.Exit(1)

    items = result.get("items", [])
    if as_json:
        print_json({"connector": row.name, "query": use_case, "mode": mode, "tools": items, "count": len(items)})
        return
    if not items:
        err_console.print(f"No tools found for query {use_case!r}.")
        return
    err_console.print(f"[bold]{row.name}[/bold] search ({mode}) — {len(items)} results:")
    print_table(
        ["Name", "Display Name", "Description"],
        [
            {
                "name": str(i.get("name") or i.get("enum") or ""),
                "displayName": str(i.get("displayName") or ""),
                "description": str(i.get("description") or "")[:80],
            }
            for i in items
        ],
        keys=["name", "displayName", "description"],
    )


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
