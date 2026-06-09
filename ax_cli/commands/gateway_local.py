"""ax gateway — local pass-through agent commands (`local` sub-app).

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

import httpx
import typer

from ..output import JSON_OPTION, console, print_json
from .gateway_app import local_app


@local_app.command("connect")
def local_connect(
    agent_name: str | None = typer.Argument(None, help="Local pass-through agent name"),
    registry_ref: str = typer.Option(
        None,
        "--registry",
        "--ref",
        help="Existing Gateway registry row, name, install id, or id prefix to reconnect",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to fingerprint"),
    space_id: str = typer.Option(None, "--space-id", help="Initial home space if Gateway cannot infer one"),
    as_json: bool = JSON_OPTION,
):
    """Request Gateway access for a local polling/pass-through agent."""
    try:
        payload = _request_local_connect(
            agent_name=agent_name,
            registry_ref=registry_ref,
            gateway_url=gateway_url,
            workdir=workdir,
            space_id=space_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        print_json(payload)
        return
    status = str(payload.get("status") or "pending")
    connected_name = str(payload.get("agent", {}).get("name") or agent_name or registry_ref or "")
    console.print(f"[bold]local connect[/bold] @{connected_name}: {status}")
    if payload.get("registry_ref"):
        console.print(f"  registry = {payload['registry_ref']}")
    if payload.get("approval_id"):
        console.print(f"  approval = {payload['approval_id']}")
    if payload.get("session_token"):
        console.print(f"  session  = {payload['session_token']}")
        console.print(f"  expires  = {payload.get('expires_at')}")


def _ensure_workdir(path: Path, *, create: bool, raw_input: str | None = None) -> None:
    """Validate or provision a workdir for a folder-bound Gateway identity.

    The workdir is the durable binding for a Gateway agent — one folder maps
    to one registry row. Silently creating a directory the operator did not
    intend is exactly the surprise this guard exists to prevent: a typo in
    ``--workdir`` should not mint a fresh empty folder somewhere unexpected
    and then attach an agent identity to it.

    * If the path exists and is a directory, return.
    * If the path exists but is a file, error.
    * If the path does not exist and ``create`` is True, create it (with any
      missing parent directories).
    * If the path does not exist and ``create`` is False, error with an
      actionable hint pointing at ``--create-workdir``.
    """
    label = raw_input if raw_input and raw_input != str(path) else str(path)
    if path.exists():
        if not path.is_dir():
            raise typer.BadParameter(f"--workdir {label!r} exists but is not a directory: {path}")
        return
    if not create:
        raise typer.BadParameter(
            f"--workdir {label!r} does not exist: {path}\n"
            "Pass --create-workdir to create it, or pick an existing folder. "
            "One folder maps to one Gateway identity, so the workdir should be a "
            "real workspace you intend the agent to operate in."
        )
    try:
        path.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise typer.BadParameter(f"Could not create --workdir {path}: {exc}") from exc


def _gateway_local_config_text(*, agent_name: str, gateway_url: str, workdir: str | None = None) -> str:
    lines = [
        "[gateway]",
        'mode = "local"',
        f'url = "{gateway_url}"',
        "",
        "[agent]",
        f'agent_name = "{agent_name}"',
    ]
    if workdir:
        lines.append(f'workdir = "{workdir}"')
    return "\n".join(lines) + "\n"


def _gateway_local_config_from_workdir(workdir: str | None) -> dict:
    if not workdir:
        return {}
    config_path = Path(workdir).expanduser().resolve() / ".ax" / "config.toml"
    if not config_path.exists():
        return {}
    try:
        cfg = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    gateway = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    mode = str(gateway.get("mode") or cfg.get("gateway_mode") or "").strip().lower()
    url = str(gateway.get("url") or gateway.get("base_url") or cfg.get("gateway_url") or "").strip()
    agent_name = str(
        agent.get("agent_name") or agent.get("name") or cfg.get("gateway_agent_name") or cfg.get("agent_name") or ""
    ).strip()
    registry_ref = str(
        agent.get("registry_ref") or agent.get("registry") or cfg.get("gateway_registry_ref") or ""
    ).strip()
    if mode not in {"local", "pass_through", "gateway"} and not url:
        return {}
    return {
        "agent_name": agent_name or None,
        "registry_ref": registry_ref or None,
        "gateway_url": url or None,
        "config_path": str(config_path),
    }


def _resolve_local_gateway_identity(
    *,
    agent_name: str | None,
    registry_ref: str | None,
    workdir: str | None,
) -> tuple[str | None, str | None]:
    workdir_cfg = _gateway_local_config_from_workdir(workdir)
    configured_agent = str(workdir_cfg.get("agent_name") or "").strip()
    configured_ref = str(workdir_cfg.get("registry_ref") or "").strip()
    requested_agent = str(agent_name or "").strip()
    requested_ref = str(registry_ref or "").strip()

    if configured_agent and requested_agent and configured_agent != requested_agent:
        raise ValueError(
            "Gateway identity mismatch: "
            f"{workdir_cfg.get('config_path')} is configured for @{configured_agent}, "
            f"but this command requested @{requested_agent}. "
            "Run from that agent's directory, omit --agent, or update the repo-local Gateway config."
        )
    if configured_ref and requested_ref and configured_ref != requested_ref:
        raise ValueError(
            "Gateway registry mismatch: "
            f"{workdir_cfg.get('config_path')} is configured for {configured_ref}, "
            f"but this command requested {requested_ref}."
        )
    if not requested_agent and not requested_ref:
        requested_agent = configured_agent
        requested_ref = configured_ref
    return requested_agent or None, requested_ref or None


def _local_route_failure_guidance(
    *,
    detail: str,
    status_code: int | None,
    gateway_url: str,
    agent_name: str | None,
    workdir: str | None,
    action: str,
) -> str:
    """Build an actionable error message for /local/connect or /local/proxy failures.

    The bare ``Gateway local connect failed: not found`` text from a 404 leaves
    the operator with no idea what to try next — it doesn't even hint that the
    workspace might be bound to a Live Listener (claude_code_channel, hermes)
    that uses direct identity instead of the local-connect protocol.

    For 404s we surface that and suggest the obvious recovery commands. For
    other statuses we keep the message terse but still point at the Gateway UI.
    """
    name = (agent_name or "").strip()
    subject = f"@{name}" if name else "this workspace"
    base_url = gateway_url.rstrip("/")
    detail_text = (detail or "").strip() or "no detail returned"
    parts = [f"Gateway {action} failed for {subject}: {detail_text}."]
    if status_code == 404:
        parts.append(
            "Either no Gateway binding is registered for this workspace, "
            "or the workspace is bound to a Live Listener "
            "(claude_code_channel, hermes, etc.) which uses direct identity, "
            "not local-connect/proxy."
        )
        suggestions = ["ax gateway agents list --json"]
        if name and workdir:
            suggestions.append(f"ax gateway local connect {name} --workdir {workdir}")
        elif name:
            suggestions.append(f"ax gateway local connect {name}")
        parts.append("Try: " + "; ".join(suggestions) + ".")
    parts.append(f"Or open {base_url} to inspect Gateway agents.")
    return " ".join(parts)


def _approval_required_guidance(
    *,
    connect_payload: dict,
    gateway_url: str,
    agent_name: str | None = None,
    workdir: str | None = None,
    action: str = "continue",
) -> str:
    agent = connect_payload.get("agent") if isinstance(connect_payload.get("agent"), dict) else {}
    approval = connect_payload.get("approval") if isinstance(connect_payload.get("approval"), dict) else {}
    fingerprint = connect_payload.get("fingerprint") if isinstance(connect_payload.get("fingerprint"), dict) else {}
    name = str(agent.get("name") or agent_name or connect_payload.get("agent_name") or "").strip()
    approval_id = str(
        connect_payload.get("approval_id") or approval.get("approval_id") or agent.get("approval_id") or ""
    ).strip()
    resolved_workdir = str(
        workdir or agent.get("workdir") or fingerprint.get("cwd") or approval.get("resource") or ""
    ).strip()
    space_label = str(
        agent.get("active_space_name")
        or agent.get("active_space_id")
        or agent.get("space_name")
        or agent.get("space_id")
        or ""
    ).strip()
    binding_type = str(approval.get("approval_kind") or approval.get("action") or "runtime binding").strip()
    risk = str(approval.get("risk") or "").strip()

    subject = f"@{name}" if name else "this local agent"
    lines = [
        f"Gateway approval required for {subject}.",
        f"Ask the user to open {gateway_url.rstrip('/')} and approve the pending binding before I can {action}.",
    ]
    details = []
    if approval_id:
        details.append(f"approval_id={approval_id}")
    if resolved_workdir:
        details.append(f"workdir={resolved_workdir}")
    if space_label:
        details.append(f"space={space_label}")
    if binding_type:
        details.append(f"binding={binding_type}")
    if risk:
        details.append(f"risk={risk}")
    if details:
        lines.append("Details: " + " ".join(details))
    lines.append("Do not fall back to a direct PAT; this agent is waiting on Gateway approval.")
    return " ".join(lines)


@local_app.command("init")
def local_init(
    agent_name: str = typer.Argument(..., help="Local Gateway agent name"),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    workdir: str = typer.Option(
        None,
        "--workdir",
        help=(
            "Workspace folder to configure; defaults to CWD. One folder maps to one durable "
            "Gateway identity. The folder must already exist; pass --create-workdir to create it."
        ),
    ),
    create_workdir: bool = typer.Option(
        False,
        "--create-workdir",
        help=(
            "Create the workdir (and any missing parent directories) instead of failing when "
            "it doesn't exist. Use when you are intentionally provisioning a new workspace."
        ),
    ),
    connect: bool = typer.Option(True, "--connect/--no-connect", help="Immediately request Gateway approval/session"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing .ax/config.toml"),
    as_json: bool = JSON_OPTION,
):
    """Write a Gateway-native local config that contains no PAT or token file.

    The workdir is the durable binding for this Gateway identity: one folder
    or container maps to exactly one registry row. By default the workdir
    must already exist — bind to a real workspace, do not let the CLI silently
    fabricate one. Pass ``--create-workdir`` when you are intentionally
    provisioning a new folder for the agent.
    """
    raw_workdir = workdir or str(Path.cwd())
    root = Path(raw_workdir).expanduser().resolve()
    _ensure_workdir(root, create=create_workdir, raw_input=raw_workdir)
    ax_dir = root / ".ax"
    config_path = ax_dir / "config.toml"
    if config_path.exists() and not force:
        raise typer.BadParameter(f"{config_path} already exists; pass --force to replace it.")
    ax_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _gateway_local_config_text(agent_name=agent_name, gateway_url=gateway_url, workdir=str(root)),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    payload: dict = {
        "config_path": str(config_path),
        "workdir": str(root),
        "agent_name": agent_name,
        "gateway_url": gateway_url,
        "token_stored": False,
    }
    if connect:
        try:
            payload["connect"] = _request_local_connect(
                agent_name=agent_name,
                gateway_url=gateway_url,
                workdir=str(root),
                space_id=None,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    if as_json:
        print_json(payload)
        return
    console.print(f"[green]Gateway local config written:[/green] {config_path}")
    console.print(f"  agent  = {agent_name}")
    console.print("  token  = not stored")
    if payload.get("connect"):
        status = str(payload["connect"].get("status") or "pending")
        console.print(f"  status = {status}")
        if payload["connect"].get("approval_id"):
            console.print(f"  approval = {payload['connect']['approval_id']}")


def _request_local_connect(
    *,
    agent_name: str | None = None,
    registry_ref: str | None = None,
    gateway_url: str = "http://127.0.0.1:8765",
    workdir: str | None = None,
    space_id: str | None = None,
) -> dict:
    resolved_workdir = str(Path(workdir or Path.cwd()).expanduser().resolve())
    agent_name, registry_ref = _resolve_local_gateway_identity(
        agent_name=agent_name,
        registry_ref=registry_ref,
        workdir=resolved_workdir,
    )
    display_name = str(agent_name or registry_ref or "").strip()
    if not display_name:
        raise ValueError("Provide a local agent name or --registry/--ref.")
    fingerprint = _local_process_fingerprint(agent_name=display_name, cwd=resolved_workdir)
    body = {"fingerprint": fingerprint}
    if agent_name:
        body["agent_name"] = agent_name
    if registry_ref:
        body["registry_ref"] = registry_ref
    if space_id:
        body["space_id"] = space_id
    try:
        response = httpx.post(
            f"{gateway_url.rstrip('/')}/local/connect",
            json=body,
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise ValueError(
            _local_route_failure_guidance(
                detail=detail,
                status_code=exc.response.status_code,
                gateway_url=gateway_url,
                agent_name=display_name,
                workdir=resolved_workdir,
                action="local connect",
            )
        ) from exc
    except Exception as exc:
        raise ValueError(
            _local_route_failure_guidance(
                detail=str(exc),
                status_code=None,
                gateway_url=gateway_url,
                agent_name=display_name,
                workdir=resolved_workdir,
                action="local connect",
            )
        ) from exc
    return payload


def _resolve_local_gateway_session(
    *,
    session_token: str | None,
    agent_name: str | None = None,
    registry_ref: str | None = None,
    gateway_url: str = "http://127.0.0.1:8765",
    workdir: str | None = None,
    space_id: str | None = None,
) -> tuple[str, dict | None]:
    token = str(session_token or "").strip()
    if token:
        return token, None
    payload = _request_local_connect(
        agent_name=agent_name,
        registry_ref=registry_ref,
        gateway_url=gateway_url,
        workdir=workdir,
        space_id=space_id,
    )
    token = str(payload.get("session_token") or "").strip()
    if not token:
        status = str(payload.get("status") or "pending")
        if status == "pending":
            raise ValueError(
                _approval_required_guidance(
                    connect_payload=payload,
                    gateway_url=gateway_url,
                    agent_name=agent_name,
                    workdir=workdir,
                    action="send or poll",
                )
            )
        raise ValueError(f"Gateway local session is {status}; approve the agent before sending.")
    return token, payload


def _print_pending_reply_warning_local(
    pending: dict,
    *,
    target_inbox_cmd: str = "ax gateway local inbox",
) -> None:
    """Surface a non-blocking warning if pending unread messages exist for the local sender."""
    if not isinstance(pending, dict):
        return
    count = pending.get("count", 0) or 0
    if not count:
        return
    senders = pending.get("newest_senders", []) or []
    sender_blurb = ""
    if senders:
        if len(senders) == 1:
            sender_blurb = f", newest from @{senders[0]}"
        else:
            extras = len(senders) - 1
            sender_blurb = f", newest from @{senders[0]} (+{extras} other{'s' if extras != 1 else ''})"
    plural = "y" if count == 1 else "ies"
    console.print(
        f"[yellow]\u26a0 {count} pending repl{plural} addressed to you{sender_blurb}. "
        f"Review with: {target_inbox_cmd}[/yellow]"
    )


def _check_local_pending_replies(
    *,
    gateway_url: str,
    session_token: str,
    space_id: str | None = None,
    limit: int = 5,
) -> dict:
    """Non-blocking pre-send check via /local/inbox; mirrors messages.check_pending_replies shape.

    Always returns the empty/zero shape on any error — never raises. The local
    inbox check uses ``mark_read=False`` so the user's unread state is unchanged
    by the warning surface.
    """
    empty: dict = {"count": 0, "message_ids": [], "newest_senders": []}
    try:
        payload = _poll_local_inbox_over_http(
            gateway_url=gateway_url,
            session_token=session_token,
            limit=limit,
            space_id=space_id,
            mark_read=False,
            wait_seconds=0,
        )
    except Exception:
        return empty
    if not isinstance(payload, dict):
        return empty
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return empty
    ids: list[str] = []
    senders: list[str] = []
    seen: set[str] = set()
    for m in messages[:limit]:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if mid:
            ids.append(mid)
        sender = m.get("display_name") or m.get("agent_name") or m.get("sender") or m.get("sender_name") or ""
        sender = str(sender).strip()
        if sender and sender not in seen:
            senders.append(sender)
            seen.add(sender)
    raw_count = payload.get("unread_count")
    count = raw_count if isinstance(raw_count, int) else len(messages)
    return {"count": count, "message_ids": ids, "newest_senders": senders}


def _poll_local_inbox_over_http(
    *,
    gateway_url: str,
    session_token: str,
    limit: int = 10,
    channel: str = "main",
    space_id: str | None = None,
    mark_read: bool = True,
    wait_seconds: int = 0,
    poll_interval: float = 1.0,
) -> dict:
    params = {
        "limit": limit,
        "channel": channel,
        "unread_only": "true",
        "mark_read": "true" if mark_read else "false",
    }
    if space_id:
        params["space_id"] = space_id
    deadline = time.monotonic() + wait_seconds
    payload = None
    while True:
        response = httpx.get(
            f"{gateway_url.rstrip('/')}/local/inbox",
            params=params,
            headers={"X-Gateway-Session": session_token},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("messages") or wait_seconds <= 0 or time.monotonic() >= deadline:
            return payload
        time.sleep(poll_interval)


@local_app.command("send")
def local_send(
    session_token: str = typer.Option(
        None, "--session-token", envvar="AX_GATEWAY_SESSION", help="Gateway session token"
    ),
    content: str = typer.Argument(..., help="Message content"),
    space_id: str = typer.Option(
        None,
        "--space",
        "--space-id",
        "-s",
        help="Space to send into. Accepts a slug, name, or UUID; slug/name resolves through the local space cache.",
    ),
    agent_name: str = typer.Option(
        None, "--agent", "--name", help="Approved local pass-through agent to connect as if no session token is set"
    ),
    registry_ref: str = typer.Option(
        None, "--registry", "--ref", help="Existing Gateway registry row to connect as if no session token is set"
    ),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to fingerprint when auto-connecting"),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    parent_id: str = typer.Option(None, "--parent-id", help="Optional parent message id"),
    include_inbox: bool = typer.Option(
        True,
        "--inbox/--no-inbox",
        help="After sending, include unread messages waiting for this pass-through agent.",
    ),
    inbox_wait: int = typer.Option(
        2,
        "--inbox-wait",
        min=0,
        help="Seconds to wait for inbound messages after sending. Use 0 to only check immediately.",
    ),
    inbox_limit: int = typer.Option(10, "--inbox-limit", min=1, max=100, help="Max inbound messages to return."),
    session_proof: str = typer.Option(
        None,
        "--session-proof",
        help=(
            "Echo back the challenge code Gateway issued on the previous send. "
            "Only required when AX_GATEWAY_SESSION_CHALLENGE is enabled on the Gateway "
            "(opt-in session-continuity test). On a successful send under the flag, the "
            "response includes next_session_proof for the following call."
        ),
    ),
    as_json: bool = JSON_OPTION,
):
    """Send through an approved local pass-through Gateway session.

    The ``--space`` option accepts a slug, name, or UUID. Slugs and names
    resolve through the local space cache so pass-through agents do not
    need a user PAT just to translate a friendly name into a UUID.
    """
    if space_id:
        resolved = _resolve_space_via_cache(space_id)
        if resolved is None:
            raise typer.BadParameter(
                f"Could not resolve space '{space_id}' from the local space cache. "
                "Pass a UUID, or run `ax spaces list` once from the user side to populate the cache."
            )
        space_id = resolved
    try:
        resolved_session_token, connect_payload = _resolve_local_gateway_session(
            session_token=session_token,
            agent_name=agent_name,
            registry_ref=registry_ref,
            gateway_url=gateway_url,
            workdir=workdir,
            space_id=space_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    pending = _check_local_pending_replies(
        gateway_url=gateway_url,
        session_token=resolved_session_token,
        space_id=space_id,
    )

    body = {"content": content, "space_id": space_id, "parent_id": parent_id}
    if session_proof:
        body["session_proof"] = session_proof.strip()
    try:
        response = httpx.post(
            f"{gateway_url.rstrip('/')}/local/send",
            json=body,
            headers={"X-Gateway-Session": resolved_session_token},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        # Surface session-challenge errors so the operator can see the code
        # and the next step without sifting through generic "send failed" text.
        if isinstance(detail, str) and (
            detail.startswith("session_challenge_required:") or detail.startswith("invalid_session_proof:")
        ):
            raise typer.BadParameter(detail) from exc
        raise typer.BadParameter(f"Gateway local send failed: {detail}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Gateway local send failed: {exc}") from exc
    if include_inbox:
        try:
            payload["inbox"] = _poll_local_inbox_over_http(
                gateway_url=gateway_url,
                session_token=resolved_session_token,
                limit=inbox_limit,
                space_id=space_id,
                mark_read=True,
                wait_seconds=inbox_wait,
            )
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            try:
                detail = exc.response.json().get("error", detail)
            except Exception:
                pass
            payload["inbox_error"] = detail
        except Exception as exc:
            payload["inbox_error"] = str(exc)
    payload["pending_reply_count"] = pending.get("count", 0)
    payload["pending_reply_message_ids"] = list(pending.get("message_ids", []))
    payload["pending_reply_newest_senders"] = list(pending.get("newest_senders", []))
    if as_json:
        if connect_payload:
            payload["connect"] = {
                "status": connect_payload.get("status"),
                "registry_ref": connect_payload.get("registry_ref"),
                "agent": (connect_payload.get("agent") or {}).get("name")
                if isinstance(connect_payload.get("agent"), dict)
                else None,
            }
        print_json(payload)
        return
    console.print(f"[green]Sent through Gateway[/green] as @{payload.get('agent')}")
    if payload.get("next_session_proof"):
        console.print(
            f"[cyan]Next session-proof:[/cyan] {payload['next_session_proof']} "
            "(echo this with --session-proof on the next send)"
        )
    _print_pending_reply_warning_local(pending)
    inbox_payload = payload.get("inbox") if isinstance(payload.get("inbox"), dict) else {}
    messages = inbox_payload.get("messages") if isinstance(inbox_payload, dict) else []
    if messages:
        console.print(
            f"[bold]inbox[/bold] @{inbox_payload.get('agent') or payload.get('agent')}: {len(messages)} unread"
        )
        for message in messages:
            created = str(message.get("created_at") or "")
            author = str(message.get("display_name") or message.get("agent_name") or message.get("sender") or "-")
            body_text = str(message.get("content") or "").replace("\n", " ")
            console.print(f"  {created} {author}: {body_text[:160]}")
    elif payload.get("inbox_error"):
        console.print(f"[yellow]Inbox check failed:[/yellow] {payload['inbox_error']}")


@local_app.command("inbox")
def local_inbox(
    session_token: str = typer.Option(
        None, "--session-token", envvar="AX_GATEWAY_SESSION", help="Gateway session token"
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Max messages to return"),
    channel: str = typer.Option("main", "--channel", help="Message channel"),
    space_id: str = typer.Option(
        None,
        "--space",
        "--space-id",
        "-s",
        help="Space to poll. Accepts a slug, name, or UUID; slug/name resolves through the local space cache.",
    ),
    agent_name: str = typer.Option(
        None, "--agent", "--name", help="Approved local pass-through agent to connect as if no session token is set"
    ),
    registry_ref: str = typer.Option(
        None, "--registry", "--ref", help="Existing Gateway registry row to connect as if no session token is set"
    ),
    workdir: str = typer.Option(None, "--workdir", help="Workspace folder to fingerprint when auto-connecting"),
    mark_read: bool = typer.Option(
        True,
        "--mark-read/--no-mark-read",
        help="Mark returned messages as read. Use --no-mark-read to peek without clearing.",
    ),
    wait_seconds: int = typer.Option(
        0,
        "--wait",
        min=0,
        help="Wait up to this many seconds for an inbox message before returning.",
    ),
    poll_interval: float = typer.Option(
        2.0,
        "--poll-interval",
        min=0.5,
        max=30.0,
        help="Seconds between inbox checks when --wait is used.",
    ),
    gateway_url: str = typer.Option("http://127.0.0.1:8765", "--url", help="Local Gateway UI/API URL"),
    as_json: bool = JSON_OPTION,
):
    """Poll an approved local pass-through Gateway inbox.

    The ``--space`` option accepts a slug, name, or UUID. Slugs and names
    resolve through the local space cache; pass-through agents do not need
    a user PAT for the lookup.
    """
    if space_id:
        resolved = _resolve_space_via_cache(space_id)
        if resolved is None:
            raise typer.BadParameter(
                f"Could not resolve space '{space_id}' from the local space cache. "
                "Pass a UUID, or run `ax spaces list` once from the user side to populate the cache."
            )
        space_id = resolved
    try:
        resolved_session_token, connect_payload = _resolve_local_gateway_session(
            session_token=session_token,
            agent_name=agent_name,
            registry_ref=registry_ref,
            gateway_url=gateway_url,
            workdir=workdir,
            space_id=space_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        payload = _poll_local_inbox_over_http(
            gateway_url=gateway_url,
            session_token=resolved_session_token,
            limit=limit,
            channel=channel,
            space_id=space_id,
            mark_read=mark_read,
            wait_seconds=wait_seconds,
            poll_interval=poll_interval,
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        raise typer.BadParameter(f"Gateway local inbox failed: {detail}") from exc
    except Exception as exc:
        raise typer.BadParameter(f"Gateway local inbox failed: {exc}") from exc
    if as_json:
        if connect_payload:
            payload["connect"] = {
                "status": connect_payload.get("status"),
                "registry_ref": connect_payload.get("registry_ref"),
                "agent": (connect_payload.get("agent") or {}).get("name")
                if isinstance(connect_payload.get("agent"), dict)
                else None,
            }
        if wait_seconds > 0:
            payload["waited_seconds"] = wait_seconds
        print_json(payload)
        return
    messages = payload.get("messages") or []
    console.print(f"[bold]local inbox[/bold] @{payload.get('agent')}: {len(messages)} unread")
    for message in messages:
        created = str(message.get("created_at") or "")
        author = str(message.get("display_name") or message.get("agent_name") or message.get("sender") or "-")
        content = str(message.get("content") or "").replace("\n", " ")
        console.print(f"  {created} {author}: {content[:160]}")


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from .gateway_session import _local_process_fingerprint  # noqa: E402
from .gateway_spaces import _resolve_space_via_cache  # noqa: E402
