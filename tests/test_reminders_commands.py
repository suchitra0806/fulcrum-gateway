"""Tests for the local reminder policy runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer as _typer
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        channel: str = "main",
        metadata: dict | None = None,
        message_type: str = "text",
        **_kwargs: Any,
    ) -> dict:
        message_id = f"msg-{len(self.sent) + 1}"
        self.sent.append(
            {
                "id": message_id,
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "metadata": metadata,
                "message_type": message_type,
            }
        )
        return {"id": message_id}


def _install_fake_runtime(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.reminders.get_client", lambda: client)
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_space_id",
        lambda _client, *, explicit=None: explicit or "space-abc",
    )
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_agent_name",
        lambda client=None: "chatgpt",
    )


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_add_creates_local_policy_file(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        [
            "reminders",
            "add",
            "task-1",
            "--reason",
            "check this task",
            "--target",
            "orion",
            "--first-in-minutes",
            "0",
            "--max-fires",
            "2",
            "--file",
            str(policy_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    store = _load(policy_file)
    assert store["version"] == 2
    assert store["drafts"] == []
    assert len(store["policies"]) == 1
    policy = store["policies"][0]
    assert policy["source_task_id"] == "task-1"
    assert policy["reason"] == "check this task"
    assert policy["target"] == "orion"
    assert policy["max_fires"] == 2
    assert policy["enabled"] is True
    # Defaults for new fields
    assert policy["mode"] == "auto"
    assert policy["priority"] == 50


def test_run_once_fires_due_policy_and_disables_at_max(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-test",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "review task state",
                        "target": "orion",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["message_type"] == "reminder"
    assert sent["content"].startswith("@orion Reminder:")
    metadata = sent["metadata"]
    assert metadata["alert"]["kind"] == "task_reminder"
    assert metadata["alert"]["source_task_id"] == "task-1"
    assert metadata["alert"]["target_agent"] == "orion"
    assert metadata["alert"]["response_required"] is True
    assert metadata["reminder_policy"]["policy_id"] == "rem-test"

    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "max_fires reached"
    assert stored["fired_count"] == 1
    assert stored["last_message_id"] == "msg-1"


def test_run_once_skips_future_policy(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-future",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "not yet",
                        "target": "orion",
                        "cadence_seconds": 300,
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert fake.sent == []
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is True
    assert stored["fired_count"] == 0


def test_run_once_enriches_alert_with_task_snapshot(monkeypatch, tmp_path):
    """Task e55be7c8: task reminder alerts should carry a task snapshot
    (title/priority/status/assignee) so the frontend renders task context
    without a second round-trip."""

    class _TaskAwareHttp:
        def get(self, path: str, *, headers: dict) -> Any:
            class _R:
                def __init__(self, data):
                    self._data = data

                def raise_for_status(self):
                    return None

                def json(self):
                    return self._data

            if path.endswith("/tasks/task-snap"):
                return _R(
                    {
                        "task": {
                            "id": "task-snap",
                            "title": "Ship delivery receipts",
                            "priority": "urgent",
                            "status": "in_progress",
                            "assignee_id": "agent-orion",
                            "creator_id": "agent-chatgpt",
                            "deadline": "2026-04-17T00:00:00Z",
                        }
                    }
                )
            if path.endswith("/agents/agent-orion"):
                return _R({"agent": {"id": "agent-orion", "name": "orion"}})
            return _R({})

    fake = _FakeClient()
    fake._http = _TaskAwareHttp()  # type: ignore[attr-defined]
    fake._with_agent = lambda _: {}  # type: ignore[attr-defined]
    fake._parse_json = lambda r: r.json()  # type: ignore[attr-defined]
    _install_fake_runtime(monkeypatch, fake)

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-snap",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-snap",
                        "reason": "review delivery receipts",
                        "target": "orion",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    metadata = fake.sent[0]["metadata"]

    task = metadata["alert"].get("task")
    assert task is not None, "alert.task should be embedded when source_task resolves"
    assert task["id"] == "task-snap"
    assert task["title"] == "Ship delivery receipts"
    assert task["priority"] == "urgent"
    assert task["status"] == "in_progress"
    assert task["assignee_id"] == "agent-orion"
    assert task["assignee_name"] == "orion"
    assert task["deadline"] == "2026-04-17T00:00:00Z"

    card_payload = metadata["ui"]["cards"][0]["payload"]
    assert card_payload.get("task") == task, "card_payload.task should mirror alert.task"
    assert card_payload.get("resource_uri") == "ui://tasks/task-snap"


def test_run_once_without_task_snapshot_still_fires(monkeypatch, tmp_path):
    """If the task fetch fails (404, network), the reminder still fires
    without a task snapshot — the existing source_task_id link is the fallback."""
    fake = _FakeClient()

    class _FailingHttp:
        def get(self, path: str, *, headers: dict) -> Any:
            raise RuntimeError("simulated network failure")

    fake._http = _FailingHttp()  # type: ignore[attr-defined]
    fake._with_agent = lambda _: {}  # type: ignore[attr-defined]
    fake._parse_json = lambda r: r.json()  # type: ignore[attr-defined]
    _install_fake_runtime(monkeypatch, fake)

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-fail",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-nope",
                        "reason": "fallback path",
                        "target": "orion",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    metadata = fake.sent[0]["metadata"]
    assert "task" not in metadata["alert"], "fallback: no task snapshot embedded on failure"
    assert metadata["alert"]["source_task_id"] == "task-nope", "source_task_id link still present"


def _http_stub(routes: dict[str, dict]):
    """Build a minimal _http stub that serves fixed responses per path suffix."""

    class _R:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _Stub:
        def get(self, path: str, *, headers: dict) -> Any:
            for suffix, payload in routes.items():
                if path.endswith(suffix):
                    return _R(payload)
            return _R({})

    return _Stub()


def _install_task_aware_client(monkeypatch, routes: dict[str, dict]) -> _FakeClient:
    fake = _FakeClient()
    fake._http = _http_stub(routes)  # type: ignore[attr-defined]
    fake._with_agent = lambda _: {}  # type: ignore[attr-defined]
    fake._parse_json = lambda r: r.json()  # type: ignore[attr-defined]
    _install_fake_runtime(monkeypatch, fake)
    return fake


def test_run_once_skips_and_disables_when_source_task_is_terminal(monkeypatch, tmp_path):
    """Task e032bc49: if source task is completed/closed/done/cancelled,
    reminder must not fire and the policy must be disabled so it stops
    flooding the Activity Stream."""
    fake = _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-done": {
                "task": {
                    "id": "task-done",
                    "title": "Already shipped",
                    "status": "completed",
                    "assignee_id": "agent-orion",
                    "creator_id": "agent-chatgpt",
                }
            },
            "/agents/agent-orion": {"agent": {"id": "agent-orion", "name": "orion"}},
        },
    )

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-done",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-done",
                        "reason": "old reminder for a finished task",
                        "target": "orion",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert fake.sent == [], "terminal task must not produce a reminder message"

    payload = json.loads(result.output)
    assert len(payload["fired"]) == 1
    skipped = payload["fired"][0]
    assert skipped.get("skipped") is True
    assert skipped.get("reason") == "source_task_terminal:completed"

    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "source task task-done is completed"
    assert stored["fired_count"] == 0, "disabled skip must NOT advance fired_count"


def test_run_once_reroutes_pending_review_to_review_owner(monkeypatch, tmp_path):
    """Task f00e36ac: if task is pending_review with a review_owner in
    requirements, reminder must route to the reviewer — not the worker/assignee."""
    fake = _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-review": {
                "task": {
                    "id": "task-review",
                    "title": "PR awaiting review",
                    "status": "pending_review",
                    "assignee_id": "agent-orion",
                    "creator_id": "agent-chatgpt",
                    "requirements": {"review_owner": "madtank"},
                }
            },
            "/agents/agent-orion": {"agent": {"id": "agent-orion", "name": "orion"}},
            "/agents/agent-chatgpt": {"agent": {"id": "agent-chatgpt", "name": "chatgpt"}},
        },
    )

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-review",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-review",
                        "reason": "merge this PR",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 2,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1, "reminder still fires — just reroutes to reviewer"
    sent = fake.sent[0]
    assert sent["content"].startswith("@madtank Reminder:")
    assert "[pending review]" in sent["content"], "reason should be prefixed with [pending review]"
    metadata = sent["metadata"]
    assert metadata["alert"]["target_agent"] == "madtank"
    assert metadata["reminder_policy"]["target_resolved_from"] == "review_owner"
    # Policy continues (not disabled) — the review owner can still be reminded
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is True
    assert stored["fired_count"] == 1


def test_run_once_pending_review_falls_back_to_creator_when_no_owner(monkeypatch, tmp_path):
    """Task f00e36ac: if pending_review is flagged but no review_owner is
    listed, fall back to the task creator (per spec escalation ladder)."""
    fake = _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-review2": {
                "task": {
                    "id": "task-review2",
                    "title": "PR awaiting review — no owner",
                    "status": "in_progress",
                    "assignee_id": "agent-orion",
                    "creator_id": "agent-chatgpt",
                    "requirements": {"pending_review": True},
                }
            },
            "/agents/agent-orion": {"agent": {"id": "agent-orion", "name": "orion"}},
            "/agents/agent-chatgpt": {"agent": {"id": "agent-chatgpt", "name": "chatgpt"}},
        },
    )

    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-review2",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-review2",
                        "reason": "review this",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 2,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["content"].startswith("@chatgpt Reminder:"), "falls back to creator"
    metadata = sent["metadata"]
    assert metadata["reminder_policy"]["target_resolved_from"] == "creator_fallback"


def test_pause_skips_due_policy_and_resume_reactivates(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-pause",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "review task state",
                        "target": "demo-agent",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 2,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    pause_result = runner.invoke(
        app,
        [
            "reminders",
            "pause",
            "rem-pause",
            "--reason",
            "blocked until review",
            "--paused-by",
            "cli_sentinel",
            "--file",
            str(policy_file),
            "--json",
        ],
    )
    assert pause_result.exit_code == 0, pause_result.output
    stored = _load(policy_file)["policies"][0]
    assert stored["paused"] is True
    assert stored["paused_reason"] == "blocked until review"
    assert stored["paused_by"] == "cli_sentinel"

    run_result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert run_result.exit_code == 0, run_result.output
    assert fake.sent == []
    assert _load(policy_file)["policies"][0]["fired_count"] == 0

    resume_result = runner.invoke(
        app,
        ["reminders", "resume", "rem-pause", "--fire-in-minutes", "0", "--file", str(policy_file), "--json"],
    )
    assert resume_result.exit_code == 0, resume_result.output
    resumed = _load(policy_file)["policies"][0]
    assert resumed["paused"] is False
    assert resumed["enabled"] is True
    assert resumed["resume_at"] is None

    fired_result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])
    assert fired_result.exit_code == 0, fired_result.output
    assert len(fake.sent) == 1


def test_resume_refuses_completed_or_terminal_disabled_policy(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-complete",
                        "enabled": False,
                        "source_task_id": "task-1",
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 1,
                    },
                    {
                        "id": "rem-terminal",
                        "enabled": False,
                        "source_task_id": "task-done",
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "disabled_reason": "source task task-done is completed",
                    },
                ],
            }
        )
    )

    complete = runner.invoke(app, ["reminders", "resume", "rem-complete", "--file", str(policy_file), "--json"])
    assert complete.exit_code == 1
    assert "has reached max_fires" in complete.output

    terminal = runner.invoke(app, ["reminders", "resume", "rem-terminal", "--file", str(policy_file), "--json"])
    assert terminal.exit_code == 1
    assert "source task is terminal" in terminal.output


def test_list_json_groups_policies_by_operational_state(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-due",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "next_fire_at": "2026-04-27T06:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                    {
                        "id": "rem-paused",
                        "enabled": True,
                        "paused": True,
                        "paused_reason": "too noisy",
                        "resume_at": "2999-01-01T00:00:00Z",
                        "source_task_id": "task-2",
                        "next_fire_at": "2026-04-27T06:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                    {
                        "id": "rem-disabled",
                        "enabled": False,
                        "source_task_id": "task-3",
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                    {
                        "id": "rem-complete",
                        "enabled": False,
                        "source_task_id": "task-4",
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 1,
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "list", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [p["id"] for p in payload["groups"]["paused"]] == ["rem-paused"]
    assert [p["id"] for p in payload["groups"]["disabled"]] == ["rem-disabled"]
    assert [p["id"] for p in payload["groups"]["completed"]] == ["rem-complete"]
    assert "summary" in payload
    assert payload["policies"][0]["id"] in {"rem-due", "rem-paused", "rem-disabled", "rem-complete"}


def test_groom_reports_terminal_source_task_and_apply_disables(monkeypatch, tmp_path):
    _install_task_aware_client(
        monkeypatch,
        {
            "/tasks/task-done": {
                "task": {
                    "id": "task-done",
                    "title": "Already shipped",
                    "status": "completed",
                    "assignee_id": "agent-demo-agent",
                }
            },
            "/agents/agent-demo-agent": {"agent": {"id": "agent-demo-agent", "name": "demo-agent"}},
        },
    )
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-groom",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-done",
                        "reason": "finished work",
                        "target": "demo-agent",
                        "cadence_seconds": 300,
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "groom", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["summary"]["needs_attention"] == 1
    assert report["items"][0]["reasons"] == ["source_task_terminal:completed"]
    assert report["items"][0]["recommendation"] == "disable_or_remove_completed"
    assert any("Pause blocked/noisy" in item for item in report["hygiene"])

    apply_result = runner.invoke(app, ["reminders", "groom", "--apply", "--file", str(policy_file), "--json"])
    assert apply_result.exit_code == 0, apply_result.output
    assert json.loads(apply_result.output)["changed"] == ["rem-groom"]
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "source_task_terminal:completed"


# ---- Helper function unit tests ----


def test_now_returns_utc_datetime():
    import datetime as _dt

    from ax_cli.commands.reminders import _now

    result = _now()
    assert result.tzinfo == _dt.timezone.utc
    assert result.microsecond == 0


def test_iso_format_produces_z_suffix():
    import datetime as _dt

    from ax_cli.commands.reminders import _iso

    dt = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    assert _iso(dt) == "2026-05-11T12:00:00Z"


def test_parse_iso_handles_z_suffix():
    import datetime as _dt

    from ax_cli.commands.reminders import _parse_iso

    result = _parse_iso("2026-05-11T12:00:00Z")
    assert result == _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)


def test_parse_iso_handles_offset():

    from ax_cli.commands.reminders import _parse_iso

    result = _parse_iso("2026-05-11T12:00:00+00:00")
    assert result.tzinfo is not None


def test_parse_iso_assumes_utc_for_naive():
    import datetime as _dt

    from ax_cli.commands.reminders import _parse_iso

    result = _parse_iso("2026-05-11T12:00:00")
    assert result.tzinfo == _dt.timezone.utc


def test_default_policy_file_respects_env(monkeypatch, tmp_path):
    from ax_cli.commands.reminders import _default_policy_file

    monkeypatch.setenv("AX_REMINDERS_FILE", str(tmp_path / "custom.json"))
    result = _default_policy_file()
    assert result == tmp_path / "custom.json"


def test_default_policy_file_walks_up_for_ax_dir(monkeypatch, tmp_path):
    from ax_cli.commands.reminders import _default_policy_file

    monkeypatch.delenv("AX_REMINDERS_FILE", raising=False)
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    result = _default_policy_file()
    assert result == ax_dir / "reminders.json"


def test_policy_file_with_explicit_path():
    from ax_cli.commands.reminders import _policy_file

    result = _policy_file("/tmp/test-reminders.json")
    assert result == Path("/tmp/test-reminders.json")


def test_policy_file_without_path_uses_default(monkeypatch, tmp_path):
    from ax_cli.commands.reminders import _policy_file

    monkeypatch.setenv("AX_REMINDERS_FILE", str(tmp_path / "default.json"))
    result = _policy_file(None)
    assert result == tmp_path / "default.json"


def test_normalize_mode_valid():
    from ax_cli.commands.reminders import _normalize_mode

    assert _normalize_mode("auto") == "auto"
    assert _normalize_mode("draft") == "draft"
    assert _normalize_mode("manual") == "manual"
    assert _normalize_mode("AUTO") == "auto"
    assert _normalize_mode("  Draft  ") == "draft"
    assert _normalize_mode(None) == "auto"


def test_normalize_mode_invalid():
    import pytest

    from ax_cli.commands.reminders import _normalize_mode

    with pytest.raises(_typer.BadParameter, match="--mode must be one of"):
        _normalize_mode("invalid")


def test_normalize_priority_valid():
    from ax_cli.commands.reminders import _normalize_priority

    assert _normalize_priority(None) == 50
    assert _normalize_priority(0) == 0
    assert _normalize_priority(100) == 100
    assert _normalize_priority(50) == 50


def test_normalize_priority_invalid():
    import pytest

    from ax_cli.commands.reminders import _normalize_priority

    with pytest.raises(_typer.BadParameter, match="--priority must be between"):
        _normalize_priority(-1)
    with pytest.raises(_typer.BadParameter, match="--priority must be between"):
        _normalize_priority(101)


def test_empty_store_shape():
    from ax_cli.commands.reminders import _empty_store

    store = _empty_store()
    assert store == {"version": 2, "policies": [], "drafts": []}


def test_load_store_creates_default_for_missing(tmp_path):
    from ax_cli.commands.reminders import _load_store

    path = tmp_path / "nonexistent.json"
    result = _load_store(path)
    assert result["version"] == 2
    assert result["policies"] == []
    assert result["drafts"] == []


def test_load_store_rejects_invalid_json(tmp_path):
    from ax_cli.commands.reminders import _load_store

    path = tmp_path / "bad.json"
    path.write_text("not json")
    with pytest.raises(_typer.Exit):
        _load_store(path)


def test_load_store_rejects_non_dict(tmp_path):
    from ax_cli.commands.reminders import _load_store

    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(_typer.Exit):
        _load_store(path)


def test_load_store_rejects_non_list_policies(tmp_path):
    from ax_cli.commands.reminders import _load_store

    path = tmp_path / "bad_policies.json"
    path.write_text(json.dumps({"policies": "not a list", "drafts": []}))
    with pytest.raises(_typer.Exit):
        _load_store(path)


def test_load_store_rejects_non_list_drafts(tmp_path):
    from ax_cli.commands.reminders import _load_store

    path = tmp_path / "bad_drafts.json"
    path.write_text(json.dumps({"policies": [], "drafts": "not a list"}))
    with pytest.raises(_typer.Exit):
        _load_store(path)


def test_save_store_creates_directory_and_file(tmp_path):
    from ax_cli.commands.reminders import _save_store

    path = tmp_path / "sub" / "dir" / "reminders.json"
    store = {"version": 2, "policies": [], "drafts": []}
    _save_store(path, store)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == store


def test_find_policy_unique_prefix(tmp_path):
    from ax_cli.commands.reminders import _find_policy

    store = {
        "policies": [
            {"id": "rem-abc123", "enabled": True},
            {"id": "rem-def456", "enabled": True},
        ]
    }
    found = _find_policy(store, "rem-abc")
    assert found["id"] == "rem-abc123"


def test_find_policy_not_found(tmp_path):
    from ax_cli.commands.reminders import _find_policy

    store = {"policies": [{"id": "rem-abc123"}]}
    with pytest.raises(_typer.Exit):
        _find_policy(store, "rem-xyz")


def test_find_policy_ambiguous(tmp_path):
    from ax_cli.commands.reminders import _find_policy

    store = {"policies": [{"id": "rem-abc123"}, {"id": "rem-abc456"}]}
    with pytest.raises(_typer.Exit):
        _find_policy(store, "rem-abc")


def test_is_completed():
    from ax_cli.commands.reminders import _is_completed

    assert _is_completed({"fired_count": 3, "max_fires": 3}) is True
    assert _is_completed({"fired_count": 2, "max_fires": 3}) is False
    assert _is_completed({"fired_count": 0}) is False
    assert _is_completed({}) is False


def test_is_paused():
    from ax_cli.commands.reminders import _is_paused

    assert _is_paused({"paused": True}) is True
    assert _is_paused({"paused": False}) is False
    assert _is_paused({}) is False


def test_is_paused_auto_resumes_when_past_resume_at():
    import datetime as _dt

    from ax_cli.commands.reminders import _is_paused

    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    policy = {
        "paused": True,
        "resume_at": "2026-05-11T11:00:00Z",  # 1 hour ago
    }
    result = _is_paused(policy, now=now)
    assert result is False
    assert policy["paused"] is False


def test_policy_state_classifications():
    import datetime as _dt

    from ax_cli.commands.reminders import _policy_state

    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)

    assert _policy_state({"paused": True}, now=now) == "paused"
    assert _policy_state({"fired_count": 3, "max_fires": 3}, now=now) == "completed"
    assert _policy_state({"enabled": False}, now=now) == "disabled"
    assert _policy_state({"enabled": True, "next_fire_at": "2026-05-11T11:00:00Z"}, now=now) == "due"
    assert _policy_state({"enabled": True, "next_fire_at": "2026-05-12T12:00:00Z"}, now=now) == "active"
    # Stale: next_fire is more than STALE_AFTER_DAYS ago
    assert _policy_state({"enabled": True, "next_fire_at": "2026-04-01T00:00:00Z"}, now=now) == "stale"


def test_parse_optional_iso():
    from ax_cli.commands.reminders import _parse_optional_iso

    assert _parse_optional_iso(None) is None
    assert _parse_optional_iso("") is None
    assert _parse_optional_iso(123) is None
    assert _parse_optional_iso("not a date") is None
    result = _parse_optional_iso("2026-05-11T12:00:00Z")
    assert result is not None


def test_pause_until_with_first_at():
    from ax_cli.commands.reminders import _pause_until

    result = _pause_until("2026-06-01T00:00:00Z", None)
    assert result is not None
    assert "2026-06-01" in result


def test_pause_until_with_minutes():
    from ax_cli.commands.reminders import _pause_until

    result = _pause_until(None, 30)
    assert result is not None
    assert "T" in result


def test_pause_until_rejects_invalid_minutes():
    from ax_cli.commands.reminders import _pause_until

    with pytest.raises(_typer.BadParameter, match="--minutes must be at least 1"):
        _pause_until(None, 0)


def test_pause_until_returns_none():
    from ax_cli.commands.reminders import _pause_until

    assert _pause_until(None, None) is None


# ---- CLI command tests ----


def test_add_validates_max_fires(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        ["reminders", "add", "task-1", "--max-fires", "0", "--file", str(policy_file)],
    )
    assert result.exit_code != 0


def test_add_validates_cadence(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        ["reminders", "add", "task-1", "--cadence-minutes", "0", "--file", str(policy_file)],
    )
    assert result.exit_code != 0


def test_add_validates_first_in_minutes(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        ["reminders", "add", "task-1", "--first-in-minutes", "-1", "--file", str(policy_file)],
    )
    assert result.exit_code != 0


def test_add_with_mode_and_priority(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        [
            "reminders",
            "add",
            "task-1",
            "--mode",
            "draft",
            "--priority",
            "10",
            "--first-in-minutes",
            "0",
            "--file",
            str(policy_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["policy"]["mode"] == "draft"
    assert data["policy"]["priority"] == 10


def test_add_non_json_output(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        [
            "reminders",
            "add",
            "task-1",
            "--first-in-minutes",
            "0",
            "--file",
            str(policy_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Reminder policy added" in result.output


def test_add_with_space_id_skips_network(monkeypatch, tmp_path):
    """When --space-id is provided, add should not try to get_client."""
    policy_file = tmp_path / "reminders.json"

    def boom():
        raise RuntimeError("should not be called")

    monkeypatch.setattr("ax_cli.commands.reminders.get_client", boom)

    result = runner.invoke(
        app,
        [
            "reminders",
            "add",
            "task-1",
            "--space-id",
            "space-manual",
            "--first-in-minutes",
            "0",
            "--file",
            str(policy_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["policy"]["space_id"] == "space-manual"


def test_add_space_resolution_failure(monkeypatch, tmp_path):
    policy_file = tmp_path / "reminders.json"

    def failing_client():
        raise RuntimeError("no config")

    monkeypatch.setattr("ax_cli.commands.reminders.get_client", failing_client)

    result = runner.invoke(
        app,
        ["reminders", "add", "task-1", "--first-in-minutes", "0", "--file", str(policy_file)],
    )
    assert result.exit_code == 2
    assert "Space ID not resolvable" in result.output


def test_disable_json_output(monkeypatch, tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-dis", "enabled": True, "source_task_id": "t1"}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "disable", "rem-dis", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["policy"]["enabled"] is False


def test_disable_non_json_output(monkeypatch, tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-dis2", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "disable", "rem-dis2", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Disabled" in result.output


def test_pause_non_json_output(monkeypatch, tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-p", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "pause", "rem-p", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Paused" in result.output


def test_resume_non_json_output(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-res", "enabled": True, "paused": True, "max_fires": 5, "fired_count": 0}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "resume", "rem-res", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Resumed" in result.output


def test_resume_rejects_negative_fire_in():
    result = runner.invoke(app, ["reminders", "resume", "rem-1", "--fire-in-minutes", "-1"])
    assert result.exit_code != 0


def test_cancel_json_output(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-can", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "cancel", "rem-can", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["policy"]["disabled_reason"] == "cancelled"


def test_cancel_non_json_output(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-can2", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "cancel", "rem-can2", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Cancelled" in result.output


def test_update_priority_and_mode(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-up", "enabled": True, "priority": 50, "mode": "auto"}],
            }
        )
    )

    result = runner.invoke(
        app,
        [
            "reminders",
            "update",
            "rem-up",
            "--priority",
            "10",
            "--mode",
            "draft",
            "--cadence-minutes",
            "10",
            "--max-fires",
            "5",
            "--reason",
            "new reason",
            "--target",
            "@orion",
            "--file",
            str(policy_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["policy"]["priority"] == 10
    assert data["policy"]["mode"] == "draft"
    assert data["policy"]["cadence_seconds"] == 600
    assert data["policy"]["max_fires"] == 5
    assert data["policy"]["reason"] == "new reason"
    assert data["policy"]["target"] == "orion"  # @ stripped


def test_update_non_json_output(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-up2", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "update", "rem-up2", "--priority", "20", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Updated" in result.output


def test_update_rejects_invalid_cadence(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-up3", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "update", "rem-up3", "--cadence-minutes", "0", "--file", str(policy_file)],
    )
    assert result.exit_code != 0


def test_update_rejects_invalid_max_fires(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-up4", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "update", "rem-up4", "--max-fires", "0", "--file", str(policy_file)],
    )
    assert result.exit_code != 0


def test_list_non_json_output(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-l1",
                        "enabled": True,
                        "source_task_id": "t1",
                        # Far-future so the policy is deterministically "active"
                        # regardless of the wall clock (a near date becomes "due"
                        # once it passes). Matches the 2999 convention used by the
                        # other active-state fixtures in this file.
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "list", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    # Rich may truncate the ID in narrow terminals; check for prefix or truncated form
    assert "rem" in result.output
    assert "active" in result.output.lower()


def test_list_empty_store(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(json.dumps({"version": 1, "policies": []}))

    result = runner.invoke(
        app,
        ["reminders", "list", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "No reminder policies" in result.output


def test_run_once_non_json_with_results(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-table",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "test",
                        "target": "orion",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file)])
    assert result.exit_code == 0, result.output
    assert "rem-table" in result.output


def test_run_once_no_due_reminders(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(json.dumps({"version": 1, "policies": []}))

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file)])
    assert result.exit_code == 0
    assert "No due reminders" in result.output


def test_run_rejects_invalid_interval():
    result = runner.invoke(app, ["reminders", "run", "--once", "--interval", "0"])
    assert result.exit_code != 0


def test_snooze_command(monkeypatch, tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [{"id": "rem-sn", "enabled": True}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "snooze", "rem-sn", "--minutes", "30", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    stored = _load(policy_file)["policies"][0]
    assert stored["paused"] is True
    assert stored["resume_at"] is not None


# ---- Status command ----


def test_status_with_skip_probe(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-s1",
                        "enabled": True,
                        "next_fire_at": "2026-06-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                ],
                "drafts": [
                    {"id": "draft-1", "status": "pending", "auto_degraded": True},
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "status", "--skip-probe", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["online"] is False
    assert data["offline_reason"] == "probe skipped"
    assert data["policies_total"] == 1
    assert data["policies_enabled"] == 1
    assert data["drafts_pending"] == 1
    assert data["drafts_auto_degraded"] == 1
    assert data["next_due"] is not None


def test_status_non_json_output(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(json.dumps({"version": 1, "policies": [], "drafts": []}))

    result = runner.invoke(
        app,
        ["reminders", "status", "--skip-probe", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "OFFLINE" in result.output
    assert "no enabled policies" in result.output


def test_status_non_json_with_enabled_policies(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-ns",
                        "enabled": True,
                        "next_fire_at": "2026-06-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                ],
                "drafts": [],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "status", "--skip-probe", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "rem-ns" in result.output


# ---- Drafts subcommands ----


def test_drafts_list_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-1",
                        "status": "pending",
                        "policy_id": "rem-1",
                        "target": "orion",
                        "content": "@orion hello",
                        "created_at": "2026-05-11T00:00:00Z",
                    },
                    {"id": "draft-2", "status": "sent"},
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "drafts", "list", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data["drafts"]) == 1
    assert data["drafts"][0]["id"] == "draft-1"


def test_drafts_list_non_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-1",
                        "status": "pending",
                        "policy_id": "rem-1",
                        "target": "orion",
                        "content": "@orion hello",
                        "created_at": "2026-05-11T00:00:00Z",
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "drafts", "list", "--file", str(policy_file)])
    assert result.exit_code == 0
    assert "draft-1" in result.output


def test_drafts_list_empty(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(json.dumps({"version": 2, "policies": [], "drafts": []}))

    result = runner.invoke(app, ["reminders", "drafts", "list", "--file", str(policy_file)])
    assert result.exit_code == 0
    assert "No pending drafts" in result.output


def test_drafts_show_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-show",
                        "status": "pending",
                        "content": "@orion test",
                        "target": "orion",
                        "channel": "main",
                        "created_at": "2026-05-11T00:00:00Z",
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "drafts", "show", "draft-show", "--file", str(policy_file), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["draft"]["id"] == "draft-show"


def test_drafts_show_non_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-show2",
                        "status": "pending",
                        "content": "@orion test",
                        "target": "orion",
                        "channel": "main",
                        "created_at": "2026-05-11T00:00:00Z",
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "drafts", "show", "draft-show2", "--file", str(policy_file)])
    assert result.exit_code == 0
    assert "draft-show2" in result.output


def test_drafts_edit_body(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {"id": "draft-edit", "status": "pending", "content": "@orion old text", "target": "orion"},
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "edit", "draft-edit", "--body", "updated text", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "updated text" in data["draft"]["content"]
    assert data["draft"]["edited"] is True


def test_drafts_edit_target_rewrittes_content(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {"id": "draft-retarget", "status": "pending", "content": "@orion old reminder", "target": "orion"},
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        [
            "reminders",
            "drafts",
            "edit",
            "draft-retarget",
            "--target",
            "@newagent",
            "--file",
            str(policy_file),
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["draft"]["target"] == "newagent"
    assert "@newagent" in data["draft"]["content"]


def test_drafts_edit_non_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {"id": "draft-edit2", "status": "pending", "content": "@orion text", "target": "orion"},
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "edit", "draft-edit2", "--body", "new", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Edited" in result.output


def test_drafts_edit_requires_body_or_target(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [{"id": "draft-x", "status": "pending"}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "edit", "draft-x", "--file", str(policy_file)],
    )
    assert result.exit_code != 0


def test_drafts_send_json(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-send",
                        "status": "pending",
                        "content": "@orion hello",
                        "space_id": "space-abc",
                        "channel": "main",
                        "metadata": {},
                    },
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "send", "draft-send", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["draft"]["status"] == "sent"
    assert data["message_id"] is not None


def test_drafts_send_non_json(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {
                        "id": "draft-send2",
                        "status": "pending",
                        "content": "@orion hello",
                        "space_id": "space-abc",
                        "channel": "main",
                    },
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "send", "draft-send2", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Sent draft" in result.output


def test_drafts_cancel_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [
                    {"id": "draft-cancel", "status": "pending"},
                ],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "cancel", "draft-cancel", "--file", str(policy_file), "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["draft"]["status"] == "cancelled"


def test_drafts_cancel_non_json(tmp_path):
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 2,
                "policies": [],
                "drafts": [{"id": "draft-cancel2", "status": "pending"}],
            }
        )
    )

    result = runner.invoke(
        app,
        ["reminders", "drafts", "cancel", "draft-cancel2", "--file", str(policy_file)],
    )
    assert result.exit_code == 0
    assert "Cancelled" in result.output


def test_find_draft_not_found(tmp_path):
    from ax_cli.commands.reminders import _find_draft

    store = {"drafts": [{"id": "draft-1", "status": "pending"}]}
    with pytest.raises(_typer.Exit):
        _find_draft(store, "nonexistent")


def test_find_draft_ambiguous(tmp_path):
    from ax_cli.commands.reminders import _find_draft

    store = {
        "drafts": [
            {"id": "draft-abc1", "status": "pending"},
            {"id": "draft-abc2", "status": "pending"},
        ]
    }
    with pytest.raises(_typer.Exit):
        _find_draft(store, "draft-abc")


# ---- _due_policies edge cases ----


def test_due_policies_disables_at_max_fires():
    import datetime as _dt

    from ax_cli.commands.reminders import _due_policies

    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    store = {
        "policies": [
            {
                "id": "rem-maxed",
                "enabled": True,
                "fired_count": 3,
                "max_fires": 3,
                "next_fire_at": "2026-05-11T11:00:00Z",
            }
        ]
    }
    due = _due_policies(store, now=now)
    assert due == []
    assert store["policies"][0]["enabled"] is False


def test_due_policies_disables_invalid_next_fire():
    import datetime as _dt

    from ax_cli.commands.reminders import _due_policies

    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    store = {
        "policies": [
            {
                "id": "rem-bad",
                "enabled": True,
                "fired_count": 0,
                "max_fires": 3,
                "next_fire_at": "not-a-date",
            }
        ]
    }
    due = _due_policies(store, now=now)
    assert due == []
    assert store["policies"][0]["enabled"] is False
    assert store["policies"][0]["disabled_reason"] == "invalid next_fire_at"


def test_due_policies_skips_fired_keys():
    import datetime as _dt

    from ax_cli.commands.reminders import _due_policies

    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    store = {
        "policies": [
            {
                "id": "rem-dup",
                "enabled": True,
                "fired_count": 0,
                "max_fires": 3,
                "next_fire_at": "2026-05-11T11:00:00Z",
                "fired_keys": ["rem-dup:2026-05-11T11:00:00Z"],
            }
        ]
    }
    due = _due_policies(store, now=now)
    assert due == []


def test_due_policies_excludes_manual_by_default():
    import datetime as _dt

    from ax_cli.commands.reminders import _due_policies

    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    store = {
        "policies": [
            {
                "id": "rem-man",
                "enabled": True,
                "mode": "manual",
                "fired_count": 0,
                "max_fires": 3,
                "next_fire_at": "2026-05-11T11:00:00Z",
            }
        ]
    }
    due = _due_policies(store, now=now)
    assert due == []

    due_with_manual = _due_policies(store, now=now, include_manual=True)
    assert len(due_with_manual) == 1


# ---- _fire_policy mode=manual and mode=draft ----


def test_fire_policy_manual_mode_skips(monkeypatch, tmp_path):
    import datetime as _dt

    from ax_cli.commands.reminders import _fire_policy

    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    policy = {
        "id": "rem-manual",
        "mode": "manual",
        "enabled": True,
        "space_id": "space-abc",
        "source_task_id": "task-1",
        "reason": "test",
        "target": "orion",
        "severity": "info",
        "_current_fire_key": "rem-manual:key",
    }
    result = _fire_policy(fake, policy, now=now)
    assert result["skipped"] is True
    assert result["reason"] == "manual_mode"


def test_fire_policy_draft_mode_creates_draft(monkeypatch, tmp_path):
    import datetime as _dt

    from ax_cli.commands.reminders import _fire_policy

    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    now = _dt.datetime(2026, 5, 11, 12, 0, 0, tzinfo=_dt.timezone.utc)
    policy = {
        "id": "rem-draft",
        "mode": "draft",
        "enabled": True,
        "space_id": "space-abc",
        "source_task_id": "task-1",
        "reason": "test",
        "target": "orion",
        "severity": "info",
        "_current_fire_key": "rem-draft:key",
    }
    drafts: list[dict] = []
    result = _fire_policy(fake, policy, now=now, drafts=drafts)
    assert result.get("drafted") is True
    assert len(drafts) == 1
    assert drafts[0]["status"] == "pending"


def test_groom_non_json_output(monkeypatch, tmp_path):
    _install_task_aware_client(
        monkeypatch,
        {"/tasks/task-1": {"task": {"id": "task-1", "status": "in_progress"}}},
    )
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-g",
                        "enabled": True,
                        "source_task_id": "task-1",
                        "next_fire_at": "2026-06-01T00:00:00Z",
                        "max_fires": 5,
                        "fired_count": 0,
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "groom", "--file", str(policy_file)])
    assert result.exit_code == 0
    assert "Reminder grooming" in result.output
    assert "Hygiene" in result.output
