"""ax gateway — runtime install/types/templates commands and validators.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import typer

from ..gateway import (
    hermes_setup_status,
    infer_asset_descriptor,
    load_gateway_session,
    ollama_setup_status,
)
from ..gateway_runtime_types import (
    agent_template_list,
    runtime_type_definition,
    runtime_type_list,
)
from ..output import JSON_OPTION, err_console, print_json, print_table
from .gateway_app import app, runtime_app


def _normalize_runtime_type(runtime_type: str) -> str:
    try:
        return str(runtime_type_definition(runtime_type)["id"])
    except KeyError as exc:
        raise ValueError(
            "Unsupported runtime type. Use echo, exec, hermes_plugin, sentinel_inference_sdk, sentinel_cli, claude_code_channel, or inbox."
        ) from exc


def _validate_runtime_registration(runtime_type: str, exec_cmd: str | None) -> None:
    definition = runtime_type_definition(runtime_type)
    required = set(definition.get("requires") or [])
    if "exec_command" in required and not exec_cmd:
        raise ValueError("Exec runtimes require --exec.")
    if "exec_command" not in required and exec_cmd:
        raise ValueError("This runtime does not accept --exec.")


def _normalize_timeout_seconds(timeout_seconds: int | None) -> int | None:
    if timeout_seconds is None:
        return None
    try:
        normalized = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("Timeout must be a whole number of seconds.") from exc
    if normalized < 1:
        raise ValueError("Timeout must be at least 1 second.")
    return normalized


def _resolve_hermes_model(workdir: str | None) -> str | None:
    """Read the actual model from the hermes config so the platform shows the truth."""
    candidates = []
    if workdir:
        candidates.append(Path(workdir).expanduser().resolve() / ".hermes" / "config.yaml")
    candidates.append(Path.home() / ".hermes" / "config.yaml")
    for cfg_path in candidates:
        if not cfg_path.exists():
            continue
        try:
            import yaml as _yaml

            loaded = _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("model"):
                provider = loaded.get("provider", "")
                model_name = str(loaded["model"])
                if provider:
                    return f"{provider}:{model_name}"
                return model_name
        except Exception:
            continue
    return None


def _validate_hermes_provider(provider: str) -> None:
    """Check that ~/.hermes/auth.json has a credential pool entry for the provider."""
    auth_path = Path.home() / ".hermes" / "auth.json"
    if not auth_path.exists():
        raise ValueError(
            f"~/.hermes/auth.json not found. Cannot validate provider '{provider}'. "
            "Create auth.json with a credential pool entry for this provider."
        )
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Cannot read ~/.hermes/auth.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("~/.hermes/auth.json is not a JSON object.")
    pool = data.get("credential_pool") or {}
    if not isinstance(pool, dict):
        raise ValueError("~/.hermes/auth.json credential_pool is not a JSON object.")
    if provider not in pool:
        available = ", ".join(sorted(pool.keys())) or "(empty)"
        raise ValueError(
            f"Provider '{provider}' not found in ~/.hermes/auth.json credential pool. "
            f"Available providers: {available}. "
            f"Add a credential entry for '{provider}' before registering."
        )
    creds = pool[provider]
    if not isinstance(creds, list) or not creds:
        raise ValueError(
            f"Provider '{provider}' in ~/.hermes/auth.json has no credential entries. "
            "Add at least one credential with auth_type and access_token."
        )


def _runtime_types_payload() -> dict:
    return {"runtime_types": runtime_type_list(), "count": len(runtime_type_list())}


def _annotate_template_taxonomy(definition: dict) -> dict:
    enriched = dict(definition)
    descriptor = infer_asset_descriptor(
        {
            "template_id": definition.get("id"),
            "template_label": definition.get("label"),
            "runtime_type": definition.get("runtime_type"),
            "telemetry_shape": definition.get("telemetry_shape"),
            "asset_class": definition.get("asset_class"),
            "intake_model": definition.get("intake_model"),
            "worker_model": definition.get("worker_model"),
            "trigger_sources": definition.get("trigger_sources"),
            "return_paths": definition.get("return_paths"),
            "tags": definition.get("tags"),
            "capabilities": definition.get("capabilities"),
            "constraints": definition.get("constraints"),
            "addressable": definition.get("addressable"),
            "messageable": definition.get("messageable"),
            "schedulable": definition.get("schedulable"),
            "externally_triggered": definition.get("externally_triggered"),
        }
    )
    enriched.update(
        {
            "asset_class": descriptor["asset_class"],
            "intake_model": descriptor["intake_model"],
            "worker_model": descriptor.get("worker_model"),
            "trigger_sources": descriptor["trigger_sources"],
            "return_paths": descriptor["return_paths"],
            "telemetry_shape": descriptor["telemetry_shape"],
            "asset_type_label": descriptor["type_label"],
            "output_label": descriptor["output_label"],
            "asset_descriptor": descriptor,
        }
    )
    return enriched


# ── Runtime install (GATEWAY-RUNTIME-AUTOSETUP-001) ────────────────────────
#
# Hardcoded allowlist of runtimes the gateway can install on the operator's
# behalf. Per the spec security section: clone URL is NEVER taken from the
# request body — it comes from this dict by template_id. Adding a new runtime
# requires a code-reviewable PR. Targets must resolve under Path.home() (with
# realpath() so symlinks can't escape the home tree). pip install runs inside
# a venv at <target>/.venv, never against the system Python.

_RUNTIME_INSTALL_RECIPES: dict[str, dict] = {
    "hermes": {
        "clone_url": "https://github.com/NousResearch/hermes-agent",
        "target_relative": "hermes-agent",
        "verify_template_id": "hermes",
        "install_steps": ("clone", "venv", "pip_install", "verify"),
    },
    "sentinel_inference_sdk": {
        # No clone — creates a dedicated venv and installs the openai package.
        # Target matches the default python path resolved by
        # _sentinel_inference_sdk_python() so agents pick it up automatically.
        "target_relative": "hermes-agent",
        "packages": ["openai"],
        "install_steps": ("venv", "pip_install_packages", "pip_verify_packages"),
    },
}


def _resolve_install_target(template_id: str, override: str | None = None) -> Path:
    recipe = _RUNTIME_INSTALL_RECIPES.get(template_id)
    if recipe is None:
        raise ValueError(f"unknown runtime template: {template_id!r}")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        candidate = (Path.home() / recipe["target_relative"]).resolve()
    home_resolved = Path.home().resolve()
    try:
        candidate.relative_to(home_resolved)
    except ValueError as exc:
        raise ValueError(f"refusing to install outside home tree: {candidate} (home={home_resolved})") from exc
    return candidate


def _proc_error_msg(exc: subprocess.CalledProcessError) -> str:
    """Best-effort error string from a subprocess failure.

    `python -m venv` writes its "ensurepip not available, apt install python3-venv"
    hint to stdout, not stderr. Reading only `exc.stderr` swallowed the actionable
    error in the AUTOSETUP demo dry-run. Use both streams; fall back to exit code.
    """
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    parts: list[str] = []
    if stderr:
        parts.append(stderr)
    if stdout and stdout != stderr:
        parts.append(stdout)
    if not parts:
        parts.append(f"exit {exc.returncode}")
    return " | ".join(parts)[:500]


def _venv_module_unavailable_reason() -> str | None:
    """Return an actionable error string if stdlib venv can't create environments.

    On Debian/Ubuntu, `python3 -m venv` fails when the `python3-venv` package
    is missing — but the failure mode is "exits 1, prints hint to stdout" which
    is easy to miss. Probe `ensurepip` directly so we can fail fast with a clean
    message before running git clone.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import ensurepip"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"could not probe Python venv module: {exc}"
    if result.returncode != 0:
        return (
            "stdlib venv unavailable (ensurepip missing). "
            "On Debian/Ubuntu: `apt install python3.12-venv` (or matching python3-venv for your interpreter)."
        )
    return None


def _install_runtime_payload(
    template_id,
    *,
    target_override=None,
    operator_session=None,
):
    """Run the install recipe for ``template_id`` and return a structured result.

    Per AUTOSETUP-001 §"Security model":
    - Operator-only auth: caller MUST pass an ``operator_session`` (truthy).
      The HTTP route checks via ``load_gateway_session()`` before calling.
    - Hardcoded allowlist: ``template_id`` must be in ``_RUNTIME_INSTALL_RECIPES``.
    - User-writable target only: enforced via ``_resolve_install_target``
      (uses ``realpath`` to close the symlink trap).
    - No system Python: pip runs inside ``<target>/.venv``.
    - Cleanup on failure: any partial directory we created is removed.

    Returns a dict of shape ``{ready, summary, target, steps}`` where ``steps``
    is a chronological list of ``{step, status, detail}`` records (synchronous
    today; SSE streaming variant is a follow-up).
    """
    if not operator_session:
        raise PermissionError("install requires an active gateway operator session")
    template_id = str(template_id or "").strip().lower()
    recipe = _RUNTIME_INSTALL_RECIPES.get(template_id)
    if recipe is None:
        raise ValueError(f"unknown runtime template: {template_id!r}")

    target = _resolve_install_target(template_id, override=target_override)
    steps: list[dict[str, str]] = []
    we_created_target = False

    def _log(step: str, status: str, detail: str = "") -> None:
        steps.append({"step": step, "status": status, "detail": detail})

    def _cleanup() -> None:
        if we_created_target and target.exists():
            try:
                import shutil

                shutil.rmtree(target)
                _log("cleanup", "ok", f"removed partial install at {target}")
            except Exception as exc:  # noqa: BLE001
                _log("cleanup", "warn", f"could not remove {target}: {exc}")

    # Step: clone
    if "clone" in recipe["install_steps"]:
        clone_url = recipe["clone_url"]
        if target.exists():
            _log("clone", "skipped", f"target already exists at {target}")
        else:
            _log("clone", "running", f"cloning {clone_url} → {target}")
            we_created_target = True
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", clone_url, str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                _log("clone", "ok", f"cloned to {target}")
            except subprocess.CalledProcessError as exc:
                _cleanup()
                _log("clone", "error", f"git clone failed: {_proc_error_msg(exc)}")
                return {"ready": False, "summary": "clone failed", "target": str(target), "steps": steps}
            except subprocess.TimeoutExpired:
                _cleanup()
                _log("clone", "error", "git clone timed out after 600s")
                return {"ready": False, "summary": "clone timed out", "target": str(target), "steps": steps}

    # Step: venv
    venv_dir = target / ".venv"
    if "venv" in recipe["install_steps"]:
        if venv_dir.exists():
            _log("venv", "skipped", f"venv already at {venv_dir}")
        else:
            preflight = _venv_module_unavailable_reason()
            if preflight:
                _cleanup()
                _log("venv", "error", preflight)
                return {"ready": False, "summary": "venv prerequisite missing", "target": str(target), "steps": steps}
            _log("venv", "running", f"creating venv at {venv_dir}")
            try:
                subprocess.run(
                    [sys.executable, "-m", "venv", str(venv_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                _log("venv", "ok", str(venv_dir))
            except subprocess.CalledProcessError as exc:
                _cleanup()
                _log("venv", "error", f"venv create failed: {_proc_error_msg(exc)}")
                return {"ready": False, "summary": "venv create failed", "target": str(target), "steps": steps}

    # Step: pip install
    if "pip_install" in recipe["install_steps"]:
        venv_pip = venv_dir / "bin" / "pip"
        if not venv_pip.exists():
            _log("pip_install", "skipped", f"no pip at {venv_pip}")
        elif not (target / "pyproject.toml").exists() and not (target / "setup.py").exists():
            _log("pip_install", "skipped", "no pyproject.toml or setup.py at target")
        else:
            _log("pip_install", "running", f"installing {target} into venv")
            try:
                subprocess.run(
                    [str(venv_pip), "install", "-e", str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                _log("pip_install", "ok", "")
            except subprocess.CalledProcessError as exc:
                # Don't cleanup — the clone is valuable even if pip failed
                _log("pip_install", "warn", f"pip install -e failed (non-fatal): {_proc_error_msg(exc)}")

    # Step: pip install named packages (no local clone needed — e.g. openai)
    if "pip_install_packages" in recipe["install_steps"]:
        packages = list(recipe.get("packages") or [])
        venv_pip = venv_dir / "bin" / "pip"
        if not venv_pip.exists():
            _log("pip_install_packages", "skipped", f"no pip at {venv_pip}")
        elif not packages:
            _log("pip_install_packages", "skipped", "no packages specified in recipe")
        else:
            pkg_str = " ".join(packages)
            _log("pip_install_packages", "running", f"installing {pkg_str} into venv")
            try:
                subprocess.run(
                    [str(venv_pip), "install", *packages],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                _log("pip_install_packages", "ok", f"installed {pkg_str}")
            except subprocess.CalledProcessError as exc:
                _log("pip_install_packages", "error", f"pip install {pkg_str} failed: {_proc_error_msg(exc)}")
                return {
                    "ready": False,
                    "summary": f"pip install {pkg_str} failed",
                    "target": str(target),
                    "steps": steps,
                }

    # Step: verify named packages are importable from the venv python
    if "pip_verify_packages" in recipe["install_steps"]:
        packages = list(recipe.get("packages") or [])
        venv_python = venv_dir / "bin" / "python3"
        for pkg in packages:
            try:
                result = subprocess.run(
                    [str(venv_python), "-c", f"import importlib; importlib.import_module('{pkg}')"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    _log("verify", "ok", f"{pkg} importable from {venv_python}")
                else:
                    _log("verify", "error", f"{pkg} not importable: {result.stderr.strip()[:200]}")
                    return {
                        "ready": False,
                        "summary": f"{pkg} not importable after install",
                        "target": str(target),
                        "steps": steps,
                    }
            except Exception as exc:  # noqa: BLE001
                _log("verify", "error", f"verify failed: {exc}")
                return {"ready": False, "summary": "verify failed", "target": str(target), "steps": steps}

    # Step: verify (re-run setup_status check)
    if "verify" in recipe["install_steps"]:
        verify_template = recipe.get("verify_template_id", template_id)
        try:
            from ..gateway import hermes_setup_status

            status = hermes_setup_status({"template_id": verify_template})
            ready = bool(status.get("ready"))
            _log("verify", "ok" if ready else "error", str(status.get("summary") or ""))
        except Exception as exc:  # noqa: BLE001
            _log("verify", "error", f"verify failed: {exc}")
            return {"ready": False, "summary": "verify failed", "target": str(target), "steps": steps}

    return {
        "ready": True,
        "summary": f"{template_id} installed at {target}",
        "target": str(target),
        "python_path": str(venv_dir / "bin" / "python3"),
        "steps": steps,
    }


def _sentinel_inference_sdk_venv_status() -> dict:
    """Check whether the sentinel_inference_sdk venv exists and openai is importable."""
    recipe = _RUNTIME_INSTALL_RECIPES["sentinel_inference_sdk"]
    target = Path.home() / recipe["target_relative"]
    venv_python = target / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        return {
            "ready": False,
            "template_id": "sentinel_inference_sdk",
            "resolved_path": None,
            "expected_path": str(target / ".venv"),
            "summary": f"venv not found at {target / '.venv'}. Run `ax gateway runtime install sentinel_inference_sdk`.",
        }
    packages = list(recipe.get("packages") or [])
    for pkg in packages:
        try:
            result = subprocess.run(
                [str(venv_python), "-c", f"import importlib; importlib.import_module('{pkg}')"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return {
                    "ready": False,
                    "template_id": "sentinel_inference_sdk",
                    "resolved_path": str(venv_python),
                    "summary": f"{pkg} not importable from {venv_python}. Run `ax gateway runtime install sentinel_inference_sdk`.",
                }
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "template_id": "sentinel_inference_sdk", "summary": f"verify failed: {exc}"}
    return {
        "ready": True,
        "template_id": "sentinel_inference_sdk",
        "resolved_path": str(venv_python),
        "summary": f"openai importable from {venv_python}.",
        "python_path": str(venv_python),
    }


def _agent_templates_payload() -> dict:
    templates = [_annotate_template_taxonomy(item) for item in agent_template_list()]
    ollama_status = ollama_setup_status()
    for item in templates:
        template_id = str(item.get("id") or "").strip().lower()
        if template_id == "ollama":
            defaults = dict(item.get("defaults") or {})
            recommended_model = str(ollama_status.get("recommended_model") or "").strip() or None
            if recommended_model and not str(defaults.get("model") or "").strip():
                defaults["model"] = recommended_model
            item["defaults"] = defaults
            item["ollama_server_reachable"] = bool(ollama_status.get("server_reachable"))
            item["ollama_available_models"] = list(ollama_status.get("available_models") or [])
            item["ollama_local_models"] = list(ollama_status.get("local_models") or [])
            item["ollama_recommended_model"] = recommended_model
            item["ollama_summary"] = str(ollama_status.get("summary") or "")
        elif template_id == "hermes":
            hermes_status = hermes_setup_status({"template_id": "hermes"})
            item["hermes_ready"] = bool(hermes_status.get("ready"))
            item["hermes_resolved_path"] = hermes_status.get("resolved_path")
            item["hermes_expected_path"] = hermes_status.get("expected_path")
            item["hermes_summary"] = str(hermes_status.get("summary") or "")
            item["hermes_detail"] = str(hermes_status.get("detail") or hermes_status.get("summary") or "")
            # We don't ship a canonical clone URL — operators may use a private
            # fork. Surface the env var the gateway honors instead.
            item["hermes_fix_command"] = "export HERMES_REPO_PATH=/path/to/your/hermes-agent"
    return {"templates": templates, "count": len(templates)}


_SENTINEL_INFERENCE_SDK_SUPPORTED_CLIENTS = {"openai_sdk"}


@runtime_app.command("install")
def runtime_install(
    template_id: str = typer.Argument(..., help="Runtime template id (e.g. 'hermes', 'sentinel_inference_sdk')"),
    target: str = typer.Option(None, "--target", help="Override install target (must resolve under your home tree)"),
    client: str = typer.Option(
        None,
        "--client",
        help="Client library to install. Required for sentinel_inference_sdk. Supported: openai_sdk.",
    ),
    as_json: bool = JSON_OPTION,
):
    """Install a runtime template's prerequisites (clone + venv + pip install + verify).

    Supported templates:

    - ``hermes`` — clones https://github.com/NousResearch/hermes-agent into
      ~/hermes-agent and installs into a venv at ~/hermes-agent/.venv.
    - ``sentinel_inference_sdk`` — creates a venv at ~/hermes-agent/.venv
      (or reuses an existing one) and installs the specified client package.
      Requires ``--client``. Only ``openai_sdk`` is supported today; other clients
      are unsupported and must be added via a separate PR.
      Prints the resolved ``python_path`` so you can wire it to an agent with
      ``ax gateway agents update <name> --python <path>``.

    Other templates require a code-reviewable PR to extend the allowlist per
    AUTOSETUP-001 §Security.

    Requires an active gateway operator session — run ``ax gateway login`` first.

        ax gateway runtime install hermes
        ax gateway runtime install sentinel_inference_sdk --client openai_sdk
        ax gateway runtime install hermes --target /opt/work/hermes-agent
    """
    operator_session = load_gateway_session()
    if not operator_session:
        err_console.print("[red]No active gateway session.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    tid = str(template_id or "").strip().lower()
    if tid == "sentinel_inference_sdk":
        if not client:
            err_console.print(
                "[red]--client is required for sentinel_inference_sdk.[/red] "
                "Supported clients: openai_sdk. "
                "Example: ax gateway runtime install sentinel_inference_sdk --client openai_sdk"
            )
            raise typer.Exit(1)
        if client not in _SENTINEL_INFERENCE_SDK_SUPPORTED_CLIENTS:
            err_console.print(
                f"[red]Unsupported client: {client!r}.[/red] "
                f"Only {sorted(_SENTINEL_INFERENCE_SDK_SUPPORTED_CLIENTS)} is supported for sentinel_inference_sdk today."
            )
            raise typer.Exit(1)
    try:
        payload = _install_runtime_payload(template_id, target_override=target, operator_session=operator_session)
    except (ValueError, PermissionError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    err_console.print(f"[bold]ax gateway runtime install {template_id}[/bold]")
    err_console.print(f"  target = {payload.get('target')}")
    for step in payload.get("steps", []):
        marker = {
            "ok": "[green]✓[/green]",
            "skipped": "[dim]·[/dim]",
            "running": "[cyan]…[/cyan]",
            "warn": "[yellow]![/yellow]",
            "error": "[red]✗[/red]",
        }.get(step.get("status", ""), "?")
        detail = (step.get("detail") or "")[:160]
        err_console.print(f"  {marker} {step.get('step')}: {detail}")
    state = "[green]ready[/green]" if payload.get("ready") else "[red]not ready[/red]"
    err_console.print(f"  state = {state}")
    if payload.get("python_path"):
        err_console.print(f"  python_path = {payload['python_path']}")
        err_console.print(
            f"  [dim]Wire it up with: ax gateway agents update <name> --python {payload['python_path']}[/dim]"
        )
    if not payload.get("ready"):
        raise typer.Exit(1)


@runtime_app.command("status")
def runtime_status(
    template_id: str = typer.Argument(..., help="Runtime template id (e.g. 'hermes', 'sentinel_inference_sdk')"),
    as_json: bool = JSON_OPTION,
):
    """Report whether a runtime template is ready (preflight check).

    Useful as an automation gate: exits non-zero when not ready.

        ax gateway runtime status hermes
        ax gateway runtime status sentinel_inference_sdk
    """
    tid = template_id.strip().lower()
    if tid not in _RUNTIME_INSTALL_RECIPES:
        err_console.print(f"[red]unknown runtime template:[/red] {template_id!r}")
        raise typer.Exit(1)
    if tid == "sentinel_inference_sdk":
        status = _sentinel_inference_sdk_venv_status()
    else:
        from ..gateway import hermes_setup_status

        status = hermes_setup_status({"template_id": tid})
    if as_json:
        print_json(status)
        return
    state = "[green]ready[/green]" if status.get("ready") else "[red]not ready[/red]"
    err_console.print(f"[bold]{template_id}[/bold] {state}")
    if status.get("resolved_path"):
        err_console.print(f"  resolved_path = {status['resolved_path']}")
    if status.get("expected_path"):
        err_console.print(f"  expected_path = {status['expected_path']}")
    if status.get("python_path"):
        err_console.print(f"  python_path = {status['python_path']}")
    if status.get("summary"):
        err_console.print(f"  summary = {status['summary']}")
    if not status.get("ready"):
        raise typer.Exit(1)


@app.command("runtime-types")
def runtime_types(as_json: bool = JSON_OPTION):
    """List advanced/internal Gateway runtime backends."""
    payload = _runtime_types_payload()
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in payload["runtime_types"]:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "kind": item.get("kind"),
                "activity": item.get("signals", {}).get("activity"),
                "tools": item.get("signals", {}).get("tools"),
            }
        )
    print_table(
        ["Type", "Label", "Kind", "Activity Signal", "Tool Signal"],
        rows,
        keys=["id", "label", "kind", "activity", "tools"],
    )


@app.command("templates")
def templates(as_json: bool = JSON_OPTION):
    """List Gateway agent templates and what signals they provide."""
    payload = _agent_templates_payload()
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in payload["templates"]:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "type": item.get("asset_type_label"),
                "output": item.get("output_label"),
                "availability": item.get("availability"),
                "summary": item.get("operator_summary"),
                "activity": item.get("signals", {}).get("activity"),
            }
        )
    print_table(
        ["Template", "Label", "Type", "Output", "Status", "Why Pick It", "Activity Signal"],
        rows,
        keys=["id", "label", "type", "output", "availability", "summary", "activity"],
    )


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
