"""Business logic for ax agents profiles — runtime settings management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PROFILES_DIR = Path(__file__).parent / "agent_profiles"
_AX_PROFILES_KEY = "_axProfiles"

# Per-client config. Add a new entry here when a client gains profile support.
#
# `client` here is the MCP/coding-agent client whose settings format the
# profile fragments target (per ADR-010 §3 / ADR-011 — the profile directory
# maps to the `--client` parameter), NOT the Gateway `runtime_type` (how
# Gateway supervises the agent process — see `_gateway_runtime_to_client`
# below for the mapping between the two axes).
#
# settings_path: relative path within the agent workdir for the settings file.
_CLIENT_CONFIG: dict[str, dict[str, Any]] = {
    "claude": {
        "settings_path": ".claude/settings.local.json",
    },
}

SUPPORTED_CLIENTS: frozenset[str] = frozenset(_CLIENT_CONFIG)

# Maps Gateway `runtime_type` (how Gateway supervises the agent process) to the
# profile client (which MCP/coding-agent tool the agent runs, and therefore
# which settings file and profile fragments apply). These are different axes —
# `claude_code_channel` and `sentinel_cli` both run the Claude Code CLI and
# share its `.claude/settings.local.json` surface, so both resolve to `claude`.
# `hermes_plugin` and the sentinel SDK runtimes run Hermes's own AIAgent (no
# `.claude/settings.local.json`; their capability surface is connector policy
# instead — see docs/agent-permission-model.md), and runtimes with no
# agent-owned settings surface (echo, exec, inbox, ollama) aren't covered yet.
# Unmapped runtimes resolve to None so callers can surface a clear message
# rather than guessing.
_GATEWAY_RUNTIME_TO_CLIENT: dict[str, str] = {
    "claude_code_channel": "claude",
    "sentinel_cli": "claude",
}


def _gateway_runtime_to_client(runtime_type: str | None) -> str | None:
    """Derive the profile client implied by a Gateway ``runtime_type``.

    Returns None when the gateway runtime has no profile support yet, so
    callers can surface a clear message instead of guessing or crashing.
    """
    return _GATEWAY_RUNTIME_TO_CLIENT.get(str(runtime_type or "").strip().lower())


def _require_supported_client(client: str) -> None:
    if client not in SUPPORTED_CLIENTS:
        supported = ", ".join(sorted(SUPPORTED_CLIENTS))
        raise ValueError(f"Client '{client}' is not supported for profiles apply/diff (supported: {supported})")


def _settings_path(workdir: str | Path, client: str) -> Path:
    return Path(workdir) / _CLIENT_CONFIG[client]["settings_path"]


def _read_settings(workdir: str | Path, client: str) -> dict[str, Any]:
    path = _settings_path(workdir, client)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Profile fragment discovery
# ---------------------------------------------------------------------------


def _profiles_dir(client: str) -> Path:
    return _PROFILES_DIR / client


def list_available(client: str) -> list[str]:
    """Return profile names available for *client* (filename stems, no extension)."""
    d = _profiles_dir(client)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def list_all() -> dict[str, list[str]]:
    """Return all profiles grouped by client: {client: [profile, ...]}."""
    if not _PROFILES_DIR.is_dir():
        return {}
    return {d.name: list_available(d.name) for d in sorted(_PROFILES_DIR.iterdir()) if d.is_dir()}


# ---------------------------------------------------------------------------
# Fragment loading and merging
# ---------------------------------------------------------------------------


def _load_profile_fragment(client: str, profile_name: str) -> dict[str, Any]:
    path = _profiles_dir(client) / f"{profile_name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Profile '{profile_name}' not found for client '{client}' (looked in {path})")
    return json.loads(path.read_text())


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Merge *overlay* into *base* in-place: lists union, scalars last-wins."""
    for key, val in overlay.items():
        if key in base:
            if isinstance(base[key], dict) and isinstance(val, dict):
                _deep_merge(base[key], val)
            elif isinstance(base[key], list) and isinstance(val, list):
                existing = set(base[key])
                base[key] = base[key] + [item for item in val if item not in existing]
            else:
                base[key] = val
        else:
            base[key] = val


def _merge_fragments(fragments: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for fragment in fragments:
        _deep_merge(merged, fragment)
    return merged


def resolve(profiles: list[str], client: str) -> dict[str, Any]:
    """Merge profile fragments for *profiles* into a single settings dict."""
    fragments = [_load_profile_fragment(client, p) for p in profiles]
    return _merge_fragments(fragments)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_profile_list(workdir: str | Path, client: str) -> list[str]:
    """Return the profiles currently applied for *client* in *workdir*."""
    _require_supported_client(client)
    return list(_read_settings(workdir, client).get(_AX_PROFILES_KEY, []))


def _list_diff(
    current: dict[str, Any], target: dict[str, Any], path: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Recursively compare list-valued entries between *current* and *target*.

    Profile fragments can touch any list in the settings tree — tool
    permissions (permissions.allow, permissions.deny) and MCP server
    enablement (enabledMcpjsonServers) alike — via the same generic
    `_deep_merge` that `apply()` uses, so `diff()` needs to walk the whole
    tree rather than special-case `permissions.allow`. Entries are labelled
    with their dotted path (e.g. "permissions.allow: mcp__ax-channel__*") so
    additions to different lists stay distinguishable in the +/- summary.

    `_AX_PROFILES_KEY` bookkeeping is skipped — `apply()` always overwrites it
    with the new profile list, so it's not "applied settings" content and
    diffing it would just produce noise when switching between profiles.
    """
    add: list[str] = []
    remove: list[str] = []
    for key in sorted(set(current) | set(target)):
        if key == _AX_PROFILES_KEY:
            continue
        sub_path = path + (key,)
        cur_val = current.get(key)
        tgt_val = target.get(key)
        if isinstance(cur_val, dict) or isinstance(tgt_val, dict):
            sub_add, sub_remove = _list_diff(
                cur_val if isinstance(cur_val, dict) else {},
                tgt_val if isinstance(tgt_val, dict) else {},
                sub_path,
            )
            add += sub_add
            remove += sub_remove
        elif isinstance(cur_val, list) or isinstance(tgt_val, list):
            cur_set = set(cur_val) if isinstance(cur_val, list) else set()
            tgt_set = set(tgt_val) if isinstance(tgt_val, list) else set()
            label = ".".join(sub_path)
            add += [f"{label}: {item}" for item in sorted(tgt_set - cur_set)]
            remove += [f"{label}: {item}" for item in sorted(cur_set - tgt_set)]
    return add, remove


def diff(profiles: list[str], client: str, workdir: str | Path) -> dict[str, Any]:
    """Return a dict describing the difference between current settings and *profiles*.

    Raises ValueError for unsupported clients.

    Walks every list-valued entry in the resolved settings tree — not just
    permissions.allow — so additions like enabledMcpjsonServers or
    permissions.deny show up alongside tool-permission changes.

    Keys: ``add``, ``remove`` (lists of "<dotted.path>: <item>" entries),
    ``profiles_before``, ``profiles_after``.
    """
    _require_supported_client(client)

    current = _read_settings(workdir, client)
    current_profiles: list[str] = current.get(_AX_PROFILES_KEY, [])
    target = resolve(profiles, client)
    add, remove = _list_diff(current, target)

    return {
        "profiles_before": current_profiles,
        "profiles_after": profiles,
        "add": add,
        "remove": remove,
    }


def apply(
    profiles: list[str],
    client: str,
    workdir: str | Path,
    *,
    reset: bool = False,
) -> Path:
    """Merge profiles into the client's settings file in *workdir*.

    If *reset* is True, start from a clean slate (only profile content).
    Otherwise, merge into existing settings (list union, scalar last-wins).

    Raises ValueError for unsupported clients.

    Returns the path written.
    """
    _require_supported_client(client)
    path = _settings_path(workdir, client)
    path.parent.mkdir(parents=True, exist_ok=True)

    base: dict[str, Any] = {} if reset else _read_settings(workdir, client)
    base.pop(_AX_PROFILES_KEY, None)

    _deep_merge(base, resolve(profiles, client))
    base[_AX_PROFILES_KEY] = profiles

    path.write_text(json.dumps(base, indent=2, sort_keys=True) + "\n")
    return path


def workdir_for_agent(agent_name: str) -> str | None:
    """Look up an agent's workdir from the local Gateway registry."""
    info = agent_info_from_registry(agent_name)
    return info["workdir"] if info else None


def agent_info_from_registry(agent_name: str) -> dict[str, str | None] | None:
    """Return registry-derived info for *agent_name*, or None if not found.

    Keys:
      ``workdir``     — the agent's workdir, or None if unset.
      ``runtime_type``— the raw Gateway runtime_type (e.g. ``claude_code_channel``),
                        or None if unset.
      ``client``      — the profile client derived from ``runtime_type`` via
                        `_gateway_runtime_to_client` (e.g. ``claude``), or None
                        when that gateway runtime has no profile support yet.
    """
    try:
        from . import gateway as gateway_core

        registry = gateway_core.load_gateway_registry()
        entry = gateway_core.find_agent_entry(registry, agent_name)
        if not entry:
            return None
        workdir = str(entry.get("workdir") or "").strip() or None
        runtime_type = str(entry.get("runtime_type") or "").strip() or None
        return {
            "workdir": workdir,
            "runtime_type": runtime_type,
            "client": _gateway_runtime_to_client(runtime_type),
        }
    except Exception:
        return None
