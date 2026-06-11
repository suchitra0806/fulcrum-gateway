"""ax gateway — managed-agent lifecycle (start/stop/attach/desired-state).

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import typer

from .. import gateway as gateway_core
from ..gateway import (
    active_gateway_pid,
    agent_dir,
    annotate_runtime_health,
    find_agent_entry,
    load_gateway_registry,
    record_gateway_activity,
    save_gateway_registry,
)
from ..output import JSON_OPTION, err_console, print_json
from .gateway_app import _ATTACHED_SESSION_PROCESSES, agents_app


def _set_managed_agent_desired_state(name: str, desired_state: str) -> dict:
    desired_state = desired_state.lower().strip()
    if desired_state not in {"running", "stopped"}:
        raise ValueError("Desired state must be running or stopped.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    entry["desired_state"] = desired_state
    if desired_state == "running":
        entry["last_runtime_error_at"] = None
        entry["consecutive_setup_errors"] = 0
        entry["last_setup_error_signature"] = None
        entry["setup_disabled"] = False
        entry["setup_disabled_at"] = None
        entry["setup_disabled_reason"] = None
    if desired_state == "stopped":
        entry.pop("manual_attach_state", None)
        entry.pop("manual_attached_at", None)
        entry.pop("manual_attach_note", None)
        entry.pop("manual_attach_source", None)
        if str(entry.get("local_attach_state") or "").lower() == "manual_attached":
            entry["local_attach_state"] = "stopped"
            entry["local_attach_detail"] = "Claude Code is not running locally."
    save_gateway_registry(registry)
    event = "managed_agent_desired_running" if desired_state == "running" else "managed_agent_desired_stopped"
    record_gateway_activity(event, entry=entry)
    return annotate_runtime_health(entry, registry=registry)


def _mark_attached_agent_session(name: str, *, note: str | None = None) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    profile = gateway_core.infer_operator_profile(entry)
    if profile["placement"] != "attached" or profile["activation"] != "attach_only":
        raise ValueError(f"@{name} is not an attached-session agent.")
    now = datetime.now(timezone.utc).isoformat()
    entry["desired_state"] = "running"
    entry["effective_state"] = "running"
    entry["manual_attach_state"] = "attached"
    entry["manual_attached_at"] = now
    entry["manual_attach_source"] = "operator"
    if note is not None:
        entry["manual_attach_note"] = str(note).strip()
    entry["current_status"] = "idle"
    entry["current_activity"] = "Manually attached"
    entry["local_attach_state"] = "manual_attached"
    entry["local_attach_detail"] = "Operator marked this Claude Code session as manually attached."
    entry["last_connected_at"] = now
    entry["last_seen_at"] = now
    save_gateway_registry(registry)
    record_gateway_activity(
        "manual_attach_confirmed",
        entry=entry,
        activity_message=str(note or "Operator marked attached session as active."),
    )
    return annotate_runtime_health(entry, registry=registry)


_EXTERNAL_RUNTIME_RUNNING_STATUSES = {
    "accepted",
    "active",
    "connected",
    "heartbeat",
    "processing",
    "running",
    "started",
    "thinking",
    "tool",
    "working",
}
_EXTERNAL_RUNTIME_COMPLETE_STATUSES = {"completed", "done", "idle", "ready"}
_EXTERNAL_RUNTIME_STOPPED_STATUSES = {"disconnected", "offline", "stopped"}


def _announce_external_agent_runtime(name: str, body: dict) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")

    now = datetime.now(timezone.utc).isoformat()
    status = str(body.get("status") or "connected").strip().lower()
    runtime_kind = str(body.get("runtime_kind") or "external").strip() or "external"
    message_id = str(body.get("message_id") or "").strip()
    activity = str(body.get("activity") or body.get("status_text") or "").strip()
    current_tool = str(body.get("current_tool") or body.get("tool") or "").strip()
    pid = str(body.get("pid") or "").strip()
    workdir = str(body.get("workdir") or "").strip()
    runtime_instance_id = str(body.get("runtime_instance_id") or "").strip()
    if not runtime_instance_id:
        runtime_instance_id = f"external:{runtime_kind}:{name}:{pid or 'unknown'}"

    desired_stopped = str(entry.get("desired_state") or "stopped").strip().lower() == "stopped"
    if status in _EXTERNAL_RUNTIME_STOPPED_STATUSES:
        entry["desired_state"] = "stopped"
    elif not desired_stopped:
        entry["desired_state"] = "running"
    entry["runtime_instance_id"] = runtime_instance_id
    entry["external_runtime_instance_id"] = runtime_instance_id
    entry["external_runtime_kind"] = runtime_kind
    entry["external_runtime_managed"] = True
    entry["external_runtime_seen_at"] = now
    entry["external_runtime_status"] = status
    entry["external_runtime_state"] = "offline" if status in _EXTERNAL_RUNTIME_STOPPED_STATUSES else "connected"
    if pid:
        entry["external_runtime_pid"] = pid
    if workdir:
        entry["external_runtime_workdir"] = workdir

    if status in _EXTERNAL_RUNTIME_STOPPED_STATUSES:
        entry["effective_state"] = "stopped"
        entry["last_disconnected_at"] = now
        entry["current_status"] = None
        entry["current_tool"] = None
        entry["current_tool_call_id"] = None
        if activity:
            entry["current_activity"] = activity[:240]
    elif desired_stopped:
        entry["effective_state"] = "stopped"
        entry["runtime_instance_id"] = None
        entry["last_seen_at"] = now
        entry["current_status"] = None
        entry["current_tool"] = None
        entry["current_tool_call_id"] = None
        entry["local_attach_state"] = "external_stopped"
        entry["local_attach_detail"] = (
            "Operator requested stop; external runtime heartbeats will not mark this agent live."
        )
        if activity:
            entry["current_activity"] = activity[:240]
    else:
        entry["effective_state"] = "running"
        entry["last_seen_at"] = now
        entry["last_connected_at"] = entry.get("last_connected_at") or now
        entry["backlog_depth"] = 0
        if status in _EXTERNAL_RUNTIME_RUNNING_STATUSES:
            entry["current_status"] = "processing" if status in {"tool", "working"} else status
            if activity:
                entry["current_activity"] = activity[:240]
            if current_tool:
                entry["current_tool"] = current_tool[:120]
        elif status in _EXTERNAL_RUNTIME_COMPLETE_STATUSES:
            entry["current_status"] = None
            entry["current_tool"] = None
            entry["current_tool_call_id"] = None
            if activity:
                entry["current_activity"] = activity[:240]
        if message_id:
            if status in _EXTERNAL_RUNTIME_COMPLETE_STATUSES:
                entry["last_work_completed_at"] = now
                entry["last_reply_message_id"] = message_id
            else:
                entry["last_work_received_at"] = now
                entry["last_received_message_id"] = message_id

    save_gateway_registry(registry)
    record_gateway_activity(
        "external_runtime_announced",
        entry=entry,
        runtime_kind=runtime_kind,
        runtime_status=status,
        message_id=message_id or None,
        activity_message=activity or None,
    )
    return annotate_runtime_health(entry, registry=registry)


@agents_app.command("start")
def start_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Set a managed agent's desired state to running."""
    if active_gateway_pid() is None:
        err_console.print(
            f"[red]Gateway daemon is stopped — `agents start {name}` would only "
            "set desired_state, no supervisor would bring the agent up.[/red]"
        )
        err_console.print("[yellow]Start it with `ax gateway start`, then retry.[/yellow]")
        raise typer.Exit(1)
    try:
        _set_managed_agent_desired_state(name, "running")
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Desired state set to running:[/green] @{name}")


@agents_app.command("mark-attached")
def mark_attached_agent(
    name: str = typer.Argument(..., help="Attached-session agent name"),
    note: str = typer.Option(None, "--note", help="Optional operator note for the manual attach assertion"),
    as_json: bool = JSON_OPTION,
):
    """Mark an attached-session agent as manually attached and active."""
    try:
        payload = _mark_attached_agent_session(name, note=note)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    err_console.print(f"[green]Marked manually attached:[/green] @{name}")
    err_console.print("  state = active")


def _attach_command_for_payload(payload: dict) -> str:
    workdir = Path(str(payload["mcp_path"])).parent
    return f"cd {shlex.quote(str(workdir))} && {payload['launch_command']}"


def _prepare_attached_agent_payload(name: str) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    profile = gateway_core.infer_operator_profile(entry)
    if profile["placement"] != "attached" or profile["activation"] != "attach_only":
        raise ValueError(f"@{name} is not an attached-session agent.")
    workdir = str(entry.get("workdir") or "").strip()
    if not workdir:
        raise ValueError(f"Attached agent has no workdir: @{name}")

    from ..commands.channel import write_channel_setup

    payload = write_channel_setup(agent_name=name, workdir=Path(workdir))
    payload["attach_command"] = _attach_command_for_payload(payload)
    _set_managed_agent_desired_state(name, "running")
    return payload


def _launch_attached_agent_session(payload: dict) -> dict:
    workdir = Path(str(payload["mcp_path"])).parent
    launch_command = str(payload.get("launch_command") or "").strip()
    server_name = str(payload.get("server_name") or "ax-channel").strip() or "ax-channel"
    agent_name = str(payload.get("agent") or "attached-session").strip() or "attached-session"
    registry = load_gateway_registry()
    existing_entry = find_agent_entry(registry, agent_name)
    old_pid = int(existing_entry.get("attached_session_pid") or 0) if existing_entry else 0
    if old_pid:
        try:
            os.killpg(old_pid, signal.SIGTERM)
        except OSError:
            pass
    command = [
        "claude",
        "--strict-mcp-config",
        "--mcp-config",
        str(payload["mcp_path"]),
        "--dangerously-load-development-channels",
        f"server:{server_name}",
    ]
    if not shutil.which("claude"):
        raise ValueError("Claude Code is not on PATH. Install or open Claude Code, then try attaching again.")

    log_path = agent_dir(agent_name) / "attached-session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        script_bin = shutil.which("script")
        if script_bin and sys.platform == "darwin":
            # Claude Code expects a TTY. macOS `script file command ...` gives
            # it a background pseudo-terminal without opening Terminal.app.
            # `script` writes the PTY transcript to log_path itself; routing
            # stdout to the same handle duplicates every byte on macOS.
            process_command = [script_bin, "-q", str(log_path), *command]
            stdin = subprocess.PIPE
            stdout = subprocess.DEVNULL
        elif script_bin:
            process_command = [script_bin, "-q", "-f", "-c", launch_command, str(log_path)]
            stdin = subprocess.PIPE
            stdout = subprocess.DEVNULL
        else:
            process_command = command
            stdin = subprocess.DEVNULL
            stdout = handle
        process = subprocess.Popen(
            process_command,
            cwd=str(workdir),
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        if process.stdin:
            # Claude Code may ask first-run terminal questions before the
            # channel starts. These answers select the default prompt choices
            # so Gateway can attach without opening an operator terminal.
            for _ in range(3):
                try:
                    process.stdin.write(b"1\n\n")
                    process.stdin.flush()
                except OSError:
                    break
                time.sleep(0.45)
            if process.poll() is not None:
                handle.write(
                    b"\n[ax-gateway] Claude Code exited immediately during background attach; "
                    b"open this workspace manually or rerun Start after resolving first-run prompts.\n"
                )
                handle.flush()
        _ATTACHED_SESSION_PROCESSES[:] = [managed for managed in _ATTACHED_SESSION_PROCESSES if managed.poll() is None]
        _ATTACHED_SESSION_PROCESSES.append(process)
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, agent_name)
    if entry:
        entry["desired_state"] = "running"
        entry["effective_state"] = "starting"
        entry["current_status"] = "attaching"
        entry["current_activity"] = "Starting Claude Code"
        entry["attached_session_pid"] = process.pid
        entry["attached_session_log_path"] = str(log_path)
        entry["last_started_at"] = datetime.now(timezone.utc).isoformat()
        save_gateway_registry(registry)
    return {
        **payload,
        "launched": True,
        "launch_mode": "background",
        "pid": process.pid,
        "log_path": str(log_path),
        "message": "Started Claude Code channel in the background.",
    }


@agents_app.command("attach")
def attach_agent(
    name: str = typer.Argument(..., help="Attached-session agent name"),
    run: bool = typer.Option(False, "--run", help="Run Claude Code in this terminal after writing config"),
    as_json: bool = JSON_OPTION,
):
    """Write channel config for an attached Claude Code agent and print the attach command."""
    try:
        payload = _prepare_attached_agent_payload(name)
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    except ValueError as exc:
        err_console.print(f"[red]Not an attached-session agent:[/red] @{name}")
        err_console.print(str(exc))
        raise typer.Exit(1)
    workdir = str(Path(str(payload["mcp_path"])).parent)
    if as_json:
        print_json(payload)
        return
    err_console.print(f"[green]Claude Code channel ready:[/green] @{name}")
    err_console.print(f"  workdir = {workdir}")
    err_console.print(f"  mcp     = {payload['mcp_path']}")
    err_console.print(f"  env     = {payload['env_path']}")
    err_console.print("")
    err_console.print("[bold]Attach from a terminal:[/bold]")
    err_console.print(payload["attach_command"])
    if run:
        os.chdir(workdir)
        os.execvp(
            "claude",
            [
                "claude",
                "--strict-mcp-config",
                "--mcp-config",
                payload["mcp_path"],
                "--dangerously-load-development-channels",
                f"server:{payload['server_name']}",
            ],
        )


@agents_app.command("stop")
def stop_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Set a managed agent's desired state to stopped."""
    try:
        _set_managed_agent_desired_state(name, "stopped")
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Desired state set to stopped:[/green] @{name}")


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
