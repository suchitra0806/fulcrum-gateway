#!/usr/bin/env python3
"""Run the canonical axctl operator QA sequence in CI.

The workflow supplies secrets/vars as AX_QA_<ENV>_* values. This script turns
those into temporary named user-login configs, then runs:

1. axctl auth doctor
2. axctl qa preflight
3. axctl qa matrix

It intentionally does not print tokens or persist credentials outside the
temporary AX_CONFIG_DIR used for the job.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

AX_RUNTIME_ENV_KEYS = {
    "AX_TOKEN",
    "AX_CODEX_TOKEN",
    "AX_BASE_URL",
    "AX_AGENT_NAME",
    "AX_AGENT_ID",
    "AX_SPACE_ID",
    "AX_SWARM_TOKEN",
    "AX_ENV",
    "AX_USER_ENV",
}

EXIT_OK = 0
EXIT_NOT_OK = 2
EXIT_SKIPPED = 3


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _split_envs(value: str | None) -> list[str]:
    raw = value or "dev,next"
    return [part.strip() for part in raw.split(",") if part.strip()]


def _normalize_env_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.strip().lower()).strip(".-")
    if not normalized:
        raise ValueError("Environment name cannot be empty")
    return normalized


def _env_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.strip().upper()).strip("_")


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _artifact_name(value: str, suffix: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip(".-") or "env"
    return f"{safe}-{suffix}.json"


def _read_env_target(env_name: str) -> dict[str, Any]:
    normalized = _normalize_env_name(env_name)
    key = _env_key(normalized)
    token = os.environ.get(f"AX_QA_{key}_TOKEN") or os.environ.get(f"AX_QA_{key}_PAT")
    base_url = os.environ.get(f"AX_QA_{key}_BASE_URL") or os.environ.get(f"AX_QA_{key}_URL")
    space_id = os.environ.get(f"AX_QA_{key}_SPACE_ID")
    missing = []
    if not token:
        missing.append(f"AX_QA_{key}_TOKEN")
    if not base_url:
        missing.append(f"AX_QA_{key}_BASE_URL")
    if not space_id:
        missing.append(f"AX_QA_{key}_SPACE_ID")

    return {
        "env": normalized,
        "key": key,
        "token": token,
        "base_url": base_url,
        "space_id": space_id,
        "configured": not missing,
        "missing": missing,
    }


def _write_user_login(config_dir: Path, target: dict[str, Any]) -> None:
    path = config_dir / "users" / target["env"] / "user.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"token = {_toml_string(str(target['token']))}",
                f"base_url = {_toml_string(str(target['base_url']))}",
                f"space_id = {_toml_string(str(target['space_id']))}",
                'principal_type = "user"',
                f"environment = {_toml_string(str(target['env']))}",
                "",
            ]
        )
    )
    path.chmod(0o600)


def _command_env(config_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in AX_RUNTIME_ENV_KEYS:
        env.pop(key, None)
    env["AX_CONFIG_DIR"] = str(config_dir)
    return env


def _run_json_command(
    command: list[str],
    *,
    artifact_path: Path,
    config_dir: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        env=_command_env(config_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    duration_ms = round((time.monotonic() - started) * 1000)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(completed.stdout)

    parsed: Any = None
    parse_error: str | None = None
    if completed.stdout.strip():
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            parse_error = str(exc)

    return {
        "command": command,
        "returncode": completed.returncode,
        "ok": completed.returncode == 0 and parse_error is None,
        "artifact_path": str(artifact_path),
        "duration_ms": duration_ms,
        "stderr_tail": completed.stderr[-4000:] if completed.stderr else "",
        "parse_error": parse_error,
        "payload": parsed,
    }


def _run_preflight(target: dict[str, Any], *, qa_target: str, artifact_dir: Path, config_dir: Path) -> dict[str, Any]:
    artifact_path = artifact_dir / _artifact_name(target["env"], "preflight")
    command = [
        "axctl",
        "qa",
        "preflight",
        "--env",
        target["env"],
        "--space-id",
        str(target["space_id"]),
        "--for",
        qa_target,
        "--artifact",
        str(artifact_path),
        "--json",
    ]
    return _run_json_command(command, artifact_path=artifact_path, config_dir=config_dir)


def _run_doctor(target: dict[str, Any], *, artifact_dir: Path, config_dir: Path) -> dict[str, Any]:
    artifact_path = artifact_dir / _artifact_name(target["env"], "doctor")
    command = [
        "axctl",
        "auth",
        "doctor",
        "--env",
        target["env"],
        "--space-id",
        str(target["space_id"]),
        "--json",
    ]
    return _run_json_command(command, artifact_path=artifact_path, config_dir=config_dir)


def _run_matrix(
    targets: list[dict[str, Any]], *, qa_target: str, artifact_dir: Path, config_dir: Path
) -> dict[str, Any]:
    matrix_dir = artifact_dir / "matrix"
    stdout_path = artifact_dir / "matrix-stdout.json"
    command = ["axctl", "qa", "matrix", "--for", qa_target, "--artifact-dir", str(matrix_dir), "--json"]
    for target in targets:
        command.extend(["--env", target["env"]])
    for target in targets:
        command.extend(["--space", f"{target['env']}={target['space_id']}"])
    return _run_json_command(command, artifact_path=stdout_path, config_dir=config_dir)


def _write_step_summary(summary: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    lines = [
        "## axctl Operator QA",
        "",
        f"- ok: `{summary['ok']}`",
        f"- skipped: `{summary['skipped']}`",
        f"- target: `{summary['target']}`",
        f"- configured envs: `{', '.join(summary['configured_envs']) or 'none'}`",
        "",
    ]
    if summary["skipped"]:
        lines.append(f"Skipped reason: {summary['skip_reason']}")
    else:
        lines.extend(["| Env | Doctor | Preflight |", "| --- | --- | --- |"])
        for row in summary["envs"]:
            lines.append(f"| {row['env']} | {row['doctor_ok']} | {row['preflight_ok']} |")
        lines.extend(["", f"- matrix_ok: `{summary.get('matrix_ok')}`"])
    Path(path).write_text("\n".join(lines) + "\n")


def main() -> int:
    artifact_dir = Path(os.environ.get("AX_QA_ARTIFACT_DIR", "operator-qa-artifacts")).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    qa_target = os.environ.get("AX_QA_TARGET", "release")
    requested = _split_envs(os.environ.get("AX_QA_ENVS"))
    require_matrix = _bool_env("AX_QA_REQUIRE_MATRIX", default=False)
    config_dir = Path(os.environ.get("AX_QA_CONFIG_DIR", tempfile.mkdtemp(prefix="ax-qa-config-"))).resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)

    discovered = [_read_env_target(env_name) for env_name in requested]
    configured = [target for target in discovered if target["configured"]]
    skipped = [
        {
            "env": target["env"],
            "missing": target["missing"],
        }
        for target in discovered
        if not target["configured"]
    ]

    summary: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "skip_reason": None,
        "target": qa_target,
        "requested_envs": requested,
        "configured_envs": [target["env"] for target in configured],
        "skipped_envs": skipped,
        "artifact_dir": str(artifact_dir),
        "config_dir": str(config_dir),
        "envs": [],
    }

    if not configured:
        summary["skipped"] = True
        summary["skip_reason"] = "no configured AX_QA_<ENV>_TOKEN/BASE_URL/SPACE_ID triples found"
        summary["ok"] = False
        summary["require_matrix"] = require_matrix
        summary_path = artifact_dir / "operator-qa-summary.json"
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        _write_step_summary(summary)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return EXIT_SKIPPED

    for target in configured:
        _write_user_login(config_dir, target)

    for target in configured:
        doctor = _run_doctor(target, artifact_dir=artifact_dir, config_dir=config_dir)
        preflight = _run_preflight(target, qa_target=qa_target, artifact_dir=artifact_dir, config_dir=config_dir)
        summary["envs"].append(
            {
                "env": target["env"],
                "base_url": target["base_url"],
                "space_id": target["space_id"],
                "doctor_ok": doctor["returncode"] == 0
                and bool((doctor.get("payload") or {}).get("ok"))
                and doctor["parse_error"] is None,
                "doctor_artifact": doctor["artifact_path"],
                "doctor_returncode": doctor["returncode"],
                "doctor_stderr_tail": doctor["stderr_tail"],
                "preflight_ok": preflight["returncode"] == 0
                and bool((preflight.get("payload") or {}).get("ok"))
                and preflight["parse_error"] is None,
                "preflight_artifact": preflight["artifact_path"],
                "preflight_returncode": preflight["returncode"],
                "preflight_stderr_tail": preflight["stderr_tail"],
            }
        )

    matrix = _run_matrix(configured, qa_target=qa_target, artifact_dir=artifact_dir, config_dir=config_dir)
    matrix_payload = matrix.get("payload") if isinstance(matrix.get("payload"), dict) else {}
    matrix_ok = matrix["returncode"] == 0 and bool(matrix_payload.get("ok")) and matrix["parse_error"] is None
    summary["matrix_ok"] = matrix_ok
    summary["matrix_artifact"] = str(artifact_dir / "matrix" / "matrix.json")
    summary["matrix_stdout_artifact"] = matrix["artifact_path"]
    summary["matrix_returncode"] = matrix["returncode"]
    summary["matrix_stderr_tail"] = matrix["stderr_tail"]
    summary["ok"] = matrix_ok and all(row["doctor_ok"] and row["preflight_ok"] for row in summary["envs"])

    summary_path = artifact_dir / "operator-qa-summary.json"
    summary["summary_artifact"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    _write_step_summary(summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK if summary["ok"] else EXIT_NOT_OK


if __name__ == "__main__":
    raise SystemExit(main())
