"""Gateway local sessions, fingerprinting, identity binding, and space authorization.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import re
import shlex
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import AxClient
from .gateway_constants import LOCAL_SESSION_TTL_SECONDS, _normalized_base_url


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _gateway_id_from_registry(registry: dict[str, Any]) -> str:
    gateway = registry.setdefault("gateway", {})
    gateway_id = str(gateway.get("gateway_id") or "").strip()
    if gateway_id:
        return gateway_id
    gateway_id = str(uuid.uuid4())
    gateway["gateway_id"] = gateway_id
    return gateway_id


def local_secret_path() -> Path:
    return gateway_dir() / "local_secret.bin"


def load_local_secret() -> bytes:
    path = local_secret_path()
    if path.exists():
        return path.read_bytes()
    secret = os.urandom(32)
    path.write_bytes(secret)
    path.chmod(0o600)
    return secret


def _local_session_signature(payload: str, secret: bytes) -> str:
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def issue_local_session(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    fingerprint: dict[str, Any] | None = None,
    ttl_seconds: int = LOCAL_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc).isoformat()
    session = {
        "session_id": str(uuid.uuid4()),
        "agent_name": str(entry.get("name") or ""),
        "agent_id": str(entry.get("agent_id") or ""),
        "asset_id": _asset_id_for_entry(entry),
        "gateway_id": _gateway_id_from_registry(registry),
        "fingerprint_signature": _payload_hash(fingerprint or entry.get("local_fingerprint") or {}),
        "issued_at": now.isoformat(),
        "expires_at": expires_at,
    }
    payload = _b64url_encode(json.dumps(session, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _local_session_signature(payload, load_local_secret())
    token = f"axgw_s_{payload}.{signature}"
    registry.setdefault("local_sessions", [])
    registry["local_sessions"].append({**session, "status": "active"})
    return {"session_token": token, "session": session}


def verify_local_session_token(registry: dict[str, Any], token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    if not raw.startswith("axgw_s_") or "." not in raw:
        raise ValueError("Invalid Gateway local session token.")
    payload, signature = raw.removeprefix("axgw_s_").split(".", 1)
    expected = _local_session_signature(payload, load_local_secret())
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid Gateway local session token.")
    try:
        session = json.loads(_b64url_decode(payload).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid Gateway local session payload.") from exc
    expires_at = _parse_iso8601(session.get("expires_at"))
    if expires_at is None or expires_at < datetime.now(timezone.utc):
        raise ValueError("Gateway local session expired.")
    session_id = str(session.get("session_id") or "")
    stored = next(
        (item for item in registry.get("local_sessions", []) if str(item.get("session_id") or "") == session_id),
        None,
    )
    if stored and str(stored.get("status") or "active") != "active":
        raise ValueError("Gateway local session is no longer active.")
    return session


def _asset_id_for_entry(entry: dict[str, Any]) -> str:
    return str(entry.get("agent_id") or entry.get("asset_id") or entry.get("name") or "").strip()


def _binding_type_for_entry(entry: dict[str, Any]) -> str:
    activation = str(entry.get("activation") or "").strip()
    if activation == "attach_only":
        return "attached_session"
    if activation == "queue_worker" or str(entry.get("runtime_type") or "").strip().lower() == "inbox":
        return "queue_worker"
    return "local_runtime"


def _launch_spec_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    launch_spec = {
        "runtime_type": str(entry.get("runtime_type") or "").strip() or None,
        "template_id": str(entry.get("template_id") or "").strip() or None,
        "command": str(entry.get("exec_command") or "").strip() or None,
        "workdir": str(entry.get("workdir") or "").strip() or None,
        "model": str(entry.get("model") or "").strip() or None,
        "transport": str(entry.get("transport") or "").strip() or None,
    }
    model = str(
        entry.get("hermes_model")
        or entry.get("sentinel_model")
        or entry.get("runtime_model")
        or entry.get("model")
        or ""
    ).strip()
    if model:
        launch_spec["model"] = model
    return launch_spec


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _host_fingerprint() -> str:
    host = platform.node() or "unknown-host"
    return f"host:{hashlib.sha256(host.encode('utf-8')).hexdigest()[:16]}"


def _safe_file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if path.exists() and path.is_file():
            return _file_sha256(path)
    except OSError:
        return None
    return None


def _without_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None and value != ""}


def _command_executable_path(command: str | None) -> str | None:
    raw = str(command or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    idx = 0
    if parts and parts[0] == "env":
        idx = 1
        while idx < len(parts) and "=" in parts[idx] and not parts[idx].startswith("-"):
            idx += 1
    if idx >= len(parts):
        return None
    executable = parts[idx]
    resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
    return str(Path(resolved).expanduser().resolve()) if resolved else executable


def _runtime_origin_fingerprint(entry: dict[str, Any]) -> dict[str, Any]:
    command = str(entry.get("exec_command") or "").strip() or None
    executable_path = _command_executable_path(command)
    workdir = str(entry.get("workdir") or "").strip() or None
    runtime_type = str(entry.get("runtime_type") or "").strip() or None
    template_id = str(entry.get("template_id") or "").strip() or None
    hermes_tools_shim = Path(__file__).resolve().parent / "runtimes" / "hermes" / "tools" / "__init__.py"
    payload = _without_none(
        {
            "schema": "gateway.runtime_fingerprint.v1",
            "agent_name": str(entry.get("name") or "").strip() or None,
            "runtime_type": runtime_type,
            "template_id": template_id,
            "host_fingerprint": _host_fingerprint(),
            "platform": platform.platform(),
            "user": os.environ.get("USER") or os.environ.get("LOGNAME"),
            "workdir": str(Path(workdir).expanduser()) if workdir else None,
            "command": command,
            "executable_path": executable_path,
            "executable_sha256": _safe_file_sha256(Path(executable_path)) if executable_path else None,
            "hermes_repo_path": str(entry.get("hermes_repo_path") or "").strip() or None,
            "hermes_python": _sentinel_inference_sdk_python(entry)
            if runtime_type == "sentinel_inference_sdk"
            else None,
            "gateway_repo_root": str(_gateway_repo_root()) if runtime_type == "sentinel_inference_sdk" else None,
            "hermes_tools_shim": str(hermes_tools_shim) if runtime_type == "sentinel_inference_sdk" else None,
            "hermes_tools_shim_sha256": _safe_file_sha256(hermes_tools_shim)
            if runtime_type == "sentinel_inference_sdk"
            else None,
        }
    )
    payload["runtime_fingerprint_hash"] = _payload_hash(payload)
    return payload


def _environment_label_for_base_url(base_url: object) -> str:
    normalized = _normalized_base_url(base_url)
    if not normalized:
        return "unknown"
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = str(parsed.netloc or parsed.path or "").lower()
    if host == "paxai.app":
        return "prod"
    if host == "dev.paxai.app":
        return "dev"
    if host in {"localhost", "127.0.0.1"} or host.startswith("localhost:") or host.startswith("127.0.0.1:"):
        return "local"
    return host or "custom"


def _redacted_path(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser().resolve()
        home = Path.home().resolve()
        try:
            rel = path.relative_to(home)
            return str(Path("~") / rel)
        except ValueError:
            return str(path)
    except Exception:
        return raw


def _space_cache_rows(value: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    items = value if isinstance(value, list) else []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        space_id = str(item.get("space_id") or item.get("id") or "").strip()
        if not space_id or space_id in seen:
            continue
        seen.add(space_id)
        rows.append(
            {
                "space_id": space_id,
                "name": str(item.get("name") or item.get("space_name") or space_id),
                "is_default": bool(item.get("is_default", False)),
            }
        )
    return rows


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _space_name_from_cache(allowed_spaces: list[dict[str, Any]], space_id: str | None) -> str | None:
    if not space_id:
        return None
    for item in allowed_spaces:
        if str(item.get("space_id") or "") == str(space_id):
            name = str(item.get("name") or "").strip()
            if name and not _UUID_RE.match(name):
                return name
            return None
    return None


def apply_entry_current_space(
    entry: dict[str, Any],
    space_id: str,
    *,
    space_name: str | None = None,
    make_default: bool = True,
) -> dict[str, Any]:
    """Update an agent registry row's placement fields as one coherent bundle."""
    normalized_space_id = str(space_id or "").strip()
    if not normalized_space_id:
        return entry
    current_allowed = _space_cache_rows(entry.get("allowed_spaces"))
    # Resolve the friendly name in this order:
    #   1. caller-supplied (most authoritative)
    #   2. agent's own allowed_spaces cache (covers same-space writes)
    #   3. global on-disk space cache (covers space moves where the new
    #      space hasn't been added to the agent's allowed_spaces yet)
    #   4. legacy entry.space_name fallback — deliberately last because it
    #      can be stale right after a move (the previous space's name).
    normalized_name = (
        str(space_name or "").strip()
        or _space_name_from_cache(current_allowed, normalized_space_id)
        or space_name_from_cache(normalized_space_id)
        or str(entry.get("space_name") or normalized_space_id)
    )
    rows: list[dict[str, Any]] = [
        {
            "space_id": normalized_space_id,
            "name": normalized_name,
            "is_default": bool(make_default),
        }
    ]
    seen = {normalized_space_id}
    for item in current_allowed:
        item_space_id = str(item.get("space_id") or "").strip()
        if not item_space_id or item_space_id in seen:
            continue
        seen.add(item_space_id)
        rows.append(
            {
                "space_id": item_space_id,
                "name": str(item.get("name") or item_space_id),
                "is_default": False if make_default else bool(item.get("is_default", False)),
            }
        )

    entry["space_id"] = normalized_space_id
    entry["active_space_id"] = normalized_space_id
    entry["active_space_name"] = normalized_name
    if make_default:
        entry["default_space_id"] = normalized_space_id
        entry["default_space_name"] = normalized_name
    elif not str(entry.get("default_space_id") or "").strip():
        entry["default_space_id"] = normalized_space_id
        entry["default_space_name"] = normalized_name
    entry["allowed_spaces"] = rows
    return entry


def _fallback_allowed_spaces(entry: dict[str, Any], session: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    session = session or {}
    default_id = str(entry.get("default_space_id") or entry.get("space_id") or session.get("space_id") or "").strip()
    active_id = str(entry.get("active_space_id") or entry.get("space_id") or default_id).strip()
    rows: list[dict[str, Any]] = []
    if default_id:
        rows.append(
            {
                "space_id": default_id,
                "name": str(
                    entry.get("default_space_name")
                    or entry.get("space_name")
                    or session.get("space_name")
                    or default_id
                ),
                "is_default": True,
            }
        )
    if active_id and active_id != default_id:
        rows.append(
            {
                "space_id": active_id,
                "name": str(entry.get("active_space_name") or entry.get("space_name") or active_id),
                "is_default": False,
            }
        )
    return _space_cache_rows(rows)


def _space_id_allowed(allowed_spaces: list[dict[str, Any]], space_id: str | None) -> bool:
    if not space_id:
        return False
    return any(str(item.get("space_id") or "") == str(space_id) for item in allowed_spaces)


def _binding_candidate_for_entry(entry: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip() or str(uuid.uuid4())
    launch_spec = _launch_spec_for_entry(entry)
    runtime_fingerprint = _runtime_origin_fingerprint(entry)
    workdir = str(entry.get("workdir") or "").strip() or None
    path = str(Path(workdir).expanduser()) if workdir else None
    candidate = {
        "asset_id": asset_id,
        "gateway_id": _gateway_id_from_registry(registry),
        "install_id": install_id,
        "binding_type": _binding_type_for_entry(entry),
        "path": path,
        "launch_spec": launch_spec,
        "launch_spec_hash": _payload_hash(launch_spec),
        "runtime_fingerprint": runtime_fingerprint,
        "runtime_fingerprint_hash": runtime_fingerprint.get("runtime_fingerprint_hash"),
        "created_from": str(
            entry.get("created_from") or ("ax_template" if entry.get("template_id") else "custom_bridge")
        ),
        "created_via": str(entry.get("created_via") or "cli"),
        "approved_state": str(entry.get("approved_state") or "approved"),
        "first_seen_at": str(entry.get("first_seen_at") or _now_iso()),
        "last_verified_at": str(entry.get("last_verified_at") or _now_iso()),
    }
    candidate["candidate_signature"] = _payload_hash(
        {
            "asset_id": candidate["asset_id"],
            "gateway_id": candidate["gateway_id"],
            "install_id": candidate["install_id"],
            "path": candidate["path"],
            "launch_spec_hash": candidate["launch_spec_hash"],
        }
    )
    return candidate


def _ensure_registry_lists(registry: dict[str, Any]) -> None:
    registry.setdefault("bindings", [])
    registry.setdefault("identity_bindings", [])
    registry.setdefault("approvals", [])


def find_binding(
    registry: dict[str, Any],
    *,
    asset_id: str | None = None,
    install_id: str | None = None,
    gateway_id: str | None = None,
) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    for binding in registry.get("bindings", []):
        if asset_id and str(binding.get("asset_id") or "") != asset_id:
            continue
        if install_id and str(binding.get("install_id") or "") != install_id:
            continue
        if gateway_id and str(binding.get("gateway_id") or "") != gateway_id:
            continue
        return binding
    return None


def _bindings_for_asset(registry: dict[str, Any], asset_id: str) -> list[dict[str, Any]]:
    _ensure_registry_lists(registry)
    return [binding for binding in registry.get("bindings", []) if str(binding.get("asset_id") or "") == asset_id]


def upsert_binding(registry: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    bindings = registry["bindings"]
    target_install_id = str(binding.get("install_id") or "")
    for idx, existing in enumerate(bindings):
        if str(existing.get("install_id") or "") == target_install_id and target_install_id:
            merged = dict(existing)
            merged.update(binding)
            bindings[idx] = merged
            return merged
    bindings.append(binding)
    return binding


def find_identity_binding(
    registry: dict[str, Any],
    *,
    identity_binding_id: str | None = None,
    install_id: str | None = None,
    asset_id: str | None = None,
    gateway_id: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    normalized_base_url = _normalized_base_url(base_url)
    for binding in registry.get("identity_bindings", []):
        if identity_binding_id and str(binding.get("identity_binding_id") or "") != identity_binding_id:
            continue
        if install_id and str(binding.get("install_id") or "") != install_id:
            continue
        if asset_id and str(binding.get("asset_id") or "") != asset_id:
            continue
        if gateway_id and str(binding.get("gateway_id") or "") != gateway_id:
            continue
        if (
            normalized_base_url
            and _normalized_base_url(
                ((binding.get("environment") or {}) if isinstance(binding.get("environment"), dict) else {}).get(
                    "base_url"
                )
            )
            != normalized_base_url
        ):
            continue
        return binding
    return None


def _identity_bindings_for_asset(
    registry: dict[str, Any], asset_id: str, *, gateway_id: str | None = None
) -> list[dict[str, Any]]:
    _ensure_registry_lists(registry)
    rows = [
        binding for binding in registry.get("identity_bindings", []) if str(binding.get("asset_id") or "") == asset_id
    ]
    if gateway_id:
        rows = [binding for binding in rows if str(binding.get("gateway_id") or "") == gateway_id]
    return rows


def upsert_identity_binding(registry: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    bindings = registry["identity_bindings"]
    target_id = str(binding.get("identity_binding_id") or "")
    target_install_id = str(binding.get("install_id") or "")
    target_base_url = _normalized_base_url(
        ((binding.get("environment") or {}) if isinstance(binding.get("environment"), dict) else {}).get("base_url")
    )
    for idx, existing in enumerate(bindings):
        existing_base_url = _normalized_base_url(
            ((existing.get("environment") or {}) if isinstance(existing.get("environment"), dict) else {}).get(
                "base_url"
            )
        )
        if target_id and str(existing.get("identity_binding_id") or "") == target_id:
            merged = dict(existing)
            merged.update(binding)
            bindings[idx] = merged
            return merged
        if (
            target_install_id
            and str(existing.get("install_id") or "") == target_install_id
            and existing_base_url == target_base_url
        ):
            merged = dict(existing)
            merged.update(binding)
            bindings[idx] = merged
            return merged
    bindings.append(binding)
    return binding


def _normalize_allowed_spaces_payload(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("spaces"), list):
            return _space_cache_rows(payload.get("spaces"))
        if isinstance(payload.get("items"), list):
            return _space_cache_rows(payload.get("items"))
        if isinstance(payload.get("results"), list):
            return _space_cache_rows(payload.get("results"))
    return _space_cache_rows(payload)


def _fetch_allowed_spaces_for_entry(entry: dict[str, Any]) -> list[dict[str, Any]] | None:
    base_url = _normalized_base_url(entry.get("base_url"))
    if not base_url:
        return None
    try:
        token = load_gateway_managed_agent_token(entry)
    except ValueError:
        return None
    client = AxClient(
        base_url=base_url,
        token=token,
        agent_name=str(entry.get("name") or "") or None,
        agent_id=str(entry.get("agent_id") or "") or None,
    )
    try:
        return _normalize_allowed_spaces_payload(client.list_spaces())
    except Exception:
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def ensure_gateway_identity_binding(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    session: dict[str, Any] | None = None,
    created_via: str | None = None,
    verify_spaces: bool = False,
) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip()
    if not install_id:
        install_id = str(uuid.uuid4())
        entry["install_id"] = install_id
    base_url = _normalized_base_url(entry.get("base_url") or (session or {}).get("base_url"))
    existing = find_identity_binding(
        registry,
        identity_binding_id=str(entry.get("identity_binding_id") or "").strip() or None,
        install_id=install_id,
        base_url=base_url or None,
    )
    allowed_spaces = _space_cache_rows(entry.get("allowed_spaces"))
    if not allowed_spaces and existing:
        allowed_spaces = _space_cache_rows(existing.get("allowed_spaces_cache"))
    if verify_spaces:
        fetched = _fetch_allowed_spaces_for_entry(entry)
        if fetched:
            allowed_spaces = fetched
    if not allowed_spaces:
        allowed_spaces = _fallback_allowed_spaces(entry, session=session)
    default_space_id = (
        str(
            entry.get("default_space_id")
            or ((existing or {}).get("default_space_id") if isinstance(existing, dict) else "")
            or next((item.get("space_id") for item in allowed_spaces if bool(item.get("is_default"))), None)
            or entry.get("space_id")
            or (session or {}).get("space_id")
            or ""
        ).strip()
        or None
    )
    active_space_id = (
        str(
            entry.get("active_space_id")
            or ((existing or {}).get("active_space_id") if isinstance(existing, dict) else "")
            or entry.get("space_id")
            or default_space_id
            or ""
        ).strip()
        or None
    )
    default_space_name = (
        _space_name_from_cache(allowed_spaces, default_space_id)
        or space_name_from_cache(default_space_id)
        or str(entry.get("default_space_name") or entry.get("space_name") or default_space_id or "")
    )
    active_space_name = (
        _space_name_from_cache(allowed_spaces, active_space_id)
        or space_name_from_cache(active_space_id)
        or str(entry.get("active_space_name") or entry.get("space_name") or active_space_id or "")
    )
    binding = {
        "identity_binding_id": str((existing or {}).get("identity_binding_id") or f"idbind_{str(uuid.uuid4())}"),
        "asset_id": asset_id,
        "gateway_id": gateway_id,
        "install_id": install_id,
        "environment": {
            "base_url": base_url or None,
            "label": _environment_label_for_base_url(base_url),
            "host": urlparse(base_url).netloc if base_url else None,
        },
        "acting_identity": (
            dict(existing.get("acting_identity") or {})
            if isinstance(existing, dict) and isinstance(existing.get("acting_identity"), dict)
            else {
                "agent_id": str(entry.get("agent_id") or asset_id or "") or None,
                "agent_name": str(entry.get("name") or "") or None,
                "principal_type": "agent",
            }
        ),
        "credential_ref": {
            "kind": "token_file" if str(entry.get("token_file") or "").strip() else "unknown",
            "id": str(
                (existing or {}).get("credential_ref", {}).get("id")
                if isinstance((existing or {}).get("credential_ref"), dict)
                else ""
            )
            or f"cred_{str(entry.get('name') or asset_id or 'asset')}_{_environment_label_for_base_url(base_url)}",
            "display": "Gateway-managed agent token"
            if str(entry.get("credential_source") or "gateway") == "gateway"
            else "Non-gateway credential",
            "path_redacted": _redacted_path(entry.get("token_file")),
        },
        "active_space_id": active_space_id,
        "active_space_name": active_space_name or None,
        "default_space_id": default_space_id,
        "default_space_name": default_space_name or None,
        "allowed_spaces_cache": allowed_spaces,
        "binding_state": "verified" if base_url and str(entry.get("agent_id") or "") else "unbound",
        "created_via": str(created_via or entry.get("created_via") or "gateway_setup"),
        "last_verified_at": _now_iso(),
    }
    stored = upsert_identity_binding(registry, binding)
    entry["identity_binding_id"] = stored["identity_binding_id"]
    entry["default_space_id"] = stored.get("default_space_id")
    entry["default_space_name"] = stored.get("default_space_name")
    if stored.get("active_space_id"):
        entry["active_space_id"] = stored.get("active_space_id")
    if stored.get("active_space_name"):
        entry["active_space_name"] = stored.get("active_space_name")
    return stored


def evaluate_identity_space_binding(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    explicit_space_id: str | None = None,
    requested_base_url: str | None = None,
) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip() or None
    requested_url = _normalized_base_url(requested_base_url or entry.get("base_url"))
    binding = find_identity_binding(
        registry,
        identity_binding_id=str(entry.get("identity_binding_id") or "").strip() or None,
        install_id=install_id,
        base_url=requested_url or None,
    )
    asset_bindings = _identity_bindings_for_asset(registry, asset_id, gateway_id=gateway_id) if asset_id else []
    fallback_binding = asset_bindings[0] if asset_bindings else None
    acting_identity = (
        (
            binding.get("acting_identity")
            if isinstance(binding, dict) and isinstance(binding.get("acting_identity"), dict)
            else None
        )
        or (
            fallback_binding.get("acting_identity")
            if isinstance(fallback_binding, dict) and isinstance(fallback_binding.get("acting_identity"), dict)
            else None
        )
        or {}
    )
    bound_base_url = _normalized_base_url(
        (
            (binding.get("environment") or {})
            if isinstance(binding, dict) and isinstance(binding.get("environment"), dict)
            else {}
        ).get("base_url")
    )
    environment_status = "environment_unknown"
    if binding:
        environment_status = "environment_allowed"
        if requested_url and bound_base_url and requested_url != bound_base_url:
            environment_status = "environment_mismatch"
    elif requested_url and asset_bindings:
        environment_status = "environment_mismatch"

    identity_status = "verified"
    if not binding:
        identity_status = "verified" if asset_bindings else "unknown_identity"
    elif str(entry.get("credential_source") or "gateway").strip().lower() not in {"gateway", ""}:
        identity_status = "bootstrap_only"
    elif not str(entry.get("token_file") or "").strip():
        identity_status = "bootstrap_only"
    else:
        bound_agent_id = str(acting_identity.get("agent_id") or "").strip()
        bound_agent_name = str(acting_identity.get("agent_name") or "").strip().lower()
        entry_agent_id = str(entry.get("agent_id") or "").strip()
        entry_agent_name = str(entry.get("name") or "").strip().lower()
        if bound_agent_id and entry_agent_id and bound_agent_id != entry_agent_id:
            identity_status = "credential_mismatch"
        elif bound_agent_name and entry_agent_name and bound_agent_name != entry_agent_name:
            identity_status = "fallback_blocked"

    allowed_spaces = _space_cache_rows((binding or {}).get("allowed_spaces_cache"))
    if not allowed_spaces and binding:
        allowed_spaces = _fallback_allowed_spaces(entry)
    active_space_source = "none"
    active_space_id = str(explicit_space_id or "").strip() or None
    if active_space_id:
        active_space_source = "explicit_request"
    elif binding and str(binding.get("active_space_id") or "").strip():
        active_space_id = str(binding.get("active_space_id") or "").strip()
        active_space_source = "gateway_binding"
    elif binding and str(binding.get("default_space_id") or "").strip():
        active_space_id = str(binding.get("default_space_id") or "").strip()
        active_space_source = "visible_default"

    default_space_id = str((binding or {}).get("default_space_id") or "").strip() or None
    default_space_name = (
        str(
            (binding or {}).get("default_space_name")
            or _space_name_from_cache(allowed_spaces, default_space_id)
            or space_name_from_cache(default_space_id)
            or default_space_id
            or ""
        ).strip()
        or None
    )
    active_space_name = (
        _space_name_from_cache(allowed_spaces, active_space_id)
        or space_name_from_cache(active_space_id)
        or str((binding or {}).get("active_space_name") or active_space_id or "").strip()
        or None
    )

    if not active_space_id:
        space_status = "no_active_space"
    elif not allowed_spaces:
        space_status = "unknown"
    elif _space_id_allowed(allowed_spaces, active_space_id):
        space_status = "active_allowed"
    else:
        space_status = "active_not_allowed"

    return {
        "identity_binding_id": str((binding or {}).get("identity_binding_id") or entry.get("identity_binding_id") or "")
        or None,
        "asset_id": asset_id or None,
        "gateway_id": gateway_id,
        "install_id": install_id,
        "acting_agent_id": str(acting_identity.get("agent_id") or entry.get("agent_id") or "").strip() or None,
        "acting_agent_name": str(acting_identity.get("agent_name") or entry.get("name") or "").strip() or None,
        "principal_type": str(acting_identity.get("principal_type") or "agent"),
        "base_url": bound_base_url or requested_url or None,
        "environment_label": _environment_label_for_base_url(bound_base_url or requested_url),
        "environment_status": environment_status,
        "active_space_id": active_space_id,
        "active_space_name": active_space_name,
        "active_space_source": active_space_source,
        "default_space_id": default_space_id,
        "default_space_name": default_space_name,
        "allowed_spaces": allowed_spaces,
        "allowed_space_count": len(allowed_spaces),
        "identity_status": identity_status,
        "space_status": space_status,
        "last_space_verification_at": str((binding or {}).get("last_verified_at") or ""),
        "identity_binding_state": str((binding or {}).get("binding_state") or "unbound"),
        "credential_ref": dict((binding or {}).get("credential_ref") or {})
        if isinstance((binding or {}).get("credential_ref"), dict)
        else None,
    }


def _parse_iso8601(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_health import _now_iso  # noqa: E402
from .gateway_hermes import _gateway_repo_root, _sentinel_inference_sdk_python  # noqa: E402
from .gateway_storage import gateway_dir, load_gateway_managed_agent_token, space_name_from_cache  # noqa: E402
