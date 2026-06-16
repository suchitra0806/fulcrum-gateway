"""Discoverable agent manifest template library (GH #259).

Bundled templates ship as ``*.agent.toml`` (apply-ready defaults) plus
``*.meta.toml`` (operator-facing metadata). User-local overrides live in
``~/.ax/templates/`` with the same filename stem.

Replaces the hardcoded ``agent_template_catalog()`` dict in
``gateway_runtime_types.py``.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

from .gateway_runtime_types import (
    _bridge_python,
    _gateway_composio_connectors_skill_path,
    _gateway_setup_skill_path,
    _repo_root,
    runtime_type_definition,
)

_BUNDLED_DIR = Path(__file__).resolve().parent / "manifest_templates"
_USER_DIR = Path.home() / ".ax" / "templates"

_TEMPLATE_LIST_ORDER = [
    "hermes",
    "ollama",
    "langgraph",
    "langgraph_composio",
    "autogen",
    "pydantic_ai",
    "strands",
    "echo_test",
    "service_account",
    "pass_through",
    "sentinel_cli",
    "claude_code_channel",
    "inbox",
]

_SETUP_SKILL_PATHS = {
    "gateway-agent-setup": _gateway_setup_skill_path,
    "gateway-composio-connectors": _gateway_composio_connectors_skill_path,
}


def _template_vars() -> dict[str, str]:
    repo = _repo_root()
    return {
        "repo_root": str(repo),
        "bridge_python": _bridge_python(),
    }


def _resolve_template_string(value: str) -> str:
    resolved = value
    for key, repl in _template_vars().items():
        resolved = resolved.replace(f"{{{{{key}}}}}", repl)
    return resolved


def _resolve_value(value: Any) -> Any:
    if isinstance(value, str):
        return _resolve_template_string(value)
    if isinstance(value, list):
        return [_resolve_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item) for key, item in value.items()}
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _discover_template_ids() -> list[str]:
    seen: dict[str, Path] = {}
    for directory in (_BUNDLED_DIR, _USER_DIR):
        if not directory.is_dir():
            continue
        for meta_path in sorted(directory.glob("*.meta.toml")):
            template_id = meta_path.name.removesuffix(".meta.toml").strip().lower()
            if not template_id:
                continue
            seen[template_id] = meta_path.parent
    ordered = [template_id for template_id in _TEMPLATE_LIST_ORDER if template_id in seen]
    extras = sorted(template_id for template_id in seen if template_id not in ordered)
    return ordered + extras


def _template_paths(template_id: str) -> tuple[Path, Path]:
    normalized = template_id.lower().strip()
    for directory in (_USER_DIR, _BUNDLED_DIR):
        meta_path = directory / f"{normalized}.meta.toml"
        agent_path = directory / f"{normalized}.agent.toml"
        if meta_path.is_file() and agent_path.is_file():
            return meta_path, agent_path
    raise KeyError(template_id)


def _manifest_defaults(agent_path: Path) -> dict[str, Any]:
    raw = _read_toml(agent_path)
    defaults: dict[str, Any] = {}
    if runtime_type := str(raw.get("type") or "").strip():
        defaults["runtime_type"] = runtime_type
    for manifest_key, default_key in (
        ("exec_command", "exec_command"),
        ("workdir", "workdir"),
        ("model", "model"),
        ("client", "agent_client"),
        ("connector_ref", "connector_ref"),
        ("provider", "provider"),
    ):
        if manifest_key in raw:
            value = str(raw.get(manifest_key) or "").strip()
            if value:
                defaults[default_key] = value
    return _resolve_value(defaults)


def _build_signals(meta: dict[str, Any], runtime_type: str) -> dict[str, str]:
    base = copy.deepcopy(runtime_type_definition(runtime_type).get("signals") or {})
    override = meta.get("signals")
    if isinstance(override, dict):
        for key, value in override.items():
            base[str(key)] = str(value)
    return base


def _build_template_entry(template_id: str) -> dict[str, Any]:
    meta_path, agent_path = _template_paths(template_id)
    meta = _read_toml(meta_path)
    entry_id = str(meta.get("id") or template_id).strip().lower()
    runtime_type = str(meta.get("runtime_type") or "").strip()
    if not runtime_type:
        manifest_defaults = _manifest_defaults(agent_path)
        runtime_type = str(manifest_defaults.get("runtime_type") or "").strip()
    if not runtime_type:
        raise ValueError(f"Template {template_id!r} missing runtime_type in meta or manifest")

    setup_skill = str(meta.get("setup_skill") or "gateway-agent-setup").strip()
    setup_skill_fn = _SETUP_SKILL_PATHS.get(setup_skill, _gateway_setup_skill_path)

    defaults = _manifest_defaults(agent_path)
    defaults.setdefault("runtime_type", runtime_type)
    defaults_extra = meta.get("defaults_extra")
    if isinstance(defaults_extra, dict):
        for key, value in defaults_extra.items():
            defaults[str(key)] = _resolve_value(value)

    entry: dict[str, Any] = {
        "id": entry_id,
        "label": str(meta.get("label") or entry_id),
        "description": str(meta.get("description") or ""),
        "availability": str(meta.get("availability") or "ready"),
        "launchable": bool(meta.get("launchable", True)),
        "runtime_type": runtime_type,
        "asset_class": str(meta.get("asset_class") or "interactive_agent"),
        "intake_model": str(meta.get("intake_model") or ""),
        "trigger_sources": list(meta.get("trigger_sources") or []),
        "return_paths": list(meta.get("return_paths") or []),
        "telemetry_shape": str(meta.get("telemetry_shape") or "basic"),
        "suggested_name": str(meta.get("suggested_name") or entry_id),
        "operator_summary": str(meta.get("operator_summary") or ""),
        "recommended_test_message": str(meta.get("recommended_test_message") or ""),
        "what_you_need": list(meta.get("what_you_need") or []),
        "setup_skill": setup_skill,
        "setup_skill_path": str(setup_skill_fn()),
        "defaults": defaults,
        "signals": _build_signals(meta, runtime_type),
        "advanced": dict(meta.get("advanced") or {}),
    }
    if worker_model := str(meta.get("worker_model") or "").strip():
        entry["worker_model"] = worker_model
    if meta.get("requires_approval"):
        entry["requires_approval"] = True
    return entry


def agent_template_catalog() -> dict[str, dict[str, Any]]:
    return {template_id: _build_template_entry(template_id) for template_id in _discover_template_ids()}


def agent_template_definition(template_id: str) -> dict[str, Any]:
    normalized = template_id.lower().strip()
    if normalized == "echo":
        normalized = "echo_test"
    catalog = agent_template_catalog()
    if normalized not in catalog:
        raise KeyError(template_id)
    return catalog[normalized]


def agent_template_list(*, include_advanced: bool = False) -> list[dict[str, Any]]:
    catalog = agent_template_catalog()
    templates = [catalog[template_id] for template_id in _discover_template_ids() if template_id in catalog]
    if include_advanced:
        return templates
    return [item for item in templates if str(item.get("availability") or "") != "advanced"]


def template_manifest_path(template_id: str) -> Path:
    """Return the resolved ``*.agent.toml`` path for *template_id*."""
    _, agent_path = _template_paths(template_id.lower().strip())
    return agent_path


def list_template_ids(*, include_advanced: bool = False) -> list[str]:
    if include_advanced:
        return _discover_template_ids()
    return [item["id"] for item in agent_template_list(include_advanced=False)]


def copy_template_manifest(template_id: str, *, suggested_name: str | None = None) -> str:
    """Return manifest text for *template_id*, optionally overriding ``name``."""
    path = template_manifest_path(template_id)
    text = _resolve_template_string(path.read_text(encoding="utf-8"))
    if suggested_name:
        lines = []
        for line in text.splitlines():
            if line.startswith("name = "):
                lines.append(f'name = "{suggested_name}"')
            else:
                lines.append(line)
        text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text
