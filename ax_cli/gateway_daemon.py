"""Gateway GatewayDaemon — reconciliation loop and signal handling.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from typing import Any, Callable

from .client import AxClient
from .gateway_constants import (
    _CONTROLLED_APPROVAL_STATES,
    _CONTROLLED_ATTESTATION_STATES,
    _CONTROLLED_ENVIRONMENT_STATUSES,
    _CONTROLLED_IDENTITY_STATUSES,
    _CONTROLLED_SPACE_STATUSES,
    _LIFECYCLE_PHASES,
    RUNTIME_STALE_AFTER_SECONDS,
    _is_sentinel_inference_sdk_runtime,
    _is_system_agent,
    _normalized_optional_controlled,
)


class GatewayDaemon:
    """Foreground Gateway supervisor."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., Any] = AxClient,
        logger: RuntimeLogger | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.client_factory = client_factory
        self.logger = logger or (lambda _msg: None)
        self.poll_interval = poll_interval
        self._runtimes: dict[str, ManagedAgentRuntime] = {}
        self._stop = threading.Event()

    def _log(self, message: str) -> None:
        self.logger(message)

    def stop(self) -> None:
        self._stop.set()

    def _reconcile_runtime(self, entry: dict[str, Any]) -> None:
        name = str(entry.get("name") or "")
        desired_state = str(entry.get("desired_state") or "stopped").lower()
        attestation_state = _normalized_optional_controlled(
            entry.get("attestation_state"), _CONTROLLED_ATTESTATION_STATES
        )
        approval_state = _normalized_optional_controlled(entry.get("approval_state"), _CONTROLLED_APPROVAL_STATES)
        identity_status = _normalized_optional_controlled(entry.get("identity_status"), _CONTROLLED_IDENTITY_STATUSES)
        environment_status = _normalized_optional_controlled(
            entry.get("environment_status"), _CONTROLLED_ENVIRONMENT_STATUSES
        )
        space_status = _normalized_optional_controlled(entry.get("space_status"), _CONTROLLED_SPACE_STATUSES)
        runtime = self._runtimes.get(name)
        runtime_type_lower = str(entry.get("runtime_type") or "").strip().lower()
        external_runtime_state = str(entry.get("external_runtime_state") or "").strip().lower()
        # The external-runtime branch is for plugin agents the operator runs
        # themselves (manual `hermes gateway run`). When Gateway is the
        # supervisor — runtime_type is hermes_plugin — Gateway owns the
        # process lifecycle and any external-runtime hints on the entry are
        # leftover announcement state from an earlier hand-launched run.
        # Skip the external branch so we reach the supervised-subprocess path.
        if (external_runtime_state or _external_runtime_expected(entry)) and not _is_hermes_plugin_runtime(
            runtime_type_lower
        ):
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)
            if desired_state == "stopped":
                entry.update(
                    {
                        "effective_state": "stopped",
                        "runtime_instance_id": None,
                        "current_status": None,
                        "current_tool": None,
                        "current_tool_call_id": None,
                        "backlog_depth": 0,
                    }
                )
                entry["local_attach_state"] = "external_stopped"
                entry["local_attach_detail"] = (
                    "Operator requested stop; external runtime heartbeats will not mark this agent live."
                )
                return
            last_seen_age = _age_seconds(entry.get("last_seen_at"))
            external_connected = _external_runtime_connected(entry, last_seen_age=last_seen_age)
            external_stopped = external_runtime_state in {"offline", "stopped", "disconnected"}
            entry.update(
                {
                    "effective_state": "running"
                    if external_connected
                    else ("stopped" if external_stopped else "stale"),
                    "runtime_instance_id": entry.get("external_runtime_instance_id"),
                    "backlog_depth": 0,
                }
            )
            if not external_connected:
                entry["current_status"] = None
                entry["current_tool"] = None
                entry["current_tool_call_id"] = None
                if not external_stopped:
                    entry["local_attach_state"] = "external_stale"
                    entry["local_attach_detail"] = (
                        "Gateway is waiting for a fresh external runtime heartbeat before routing work."
                    )
            return
        if desired_state == "stopped":
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)
            entry.update(
                {
                    "effective_state": "stopped",
                    "runtime_instance_id": None,
                    "current_status": None,
                    "current_activity": None,
                    "current_tool": None,
                    "current_tool_call_id": None,
                    "backlog_depth": 0,
                }
            )
            return
        hermes_status = hermes_setup_status(entry)
        if not hermes_status.get("ready", True):
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)
            entry.update(
                {
                    "effective_state": "error",
                    "runtime_instance_id": None,
                    "last_error": str(
                        hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
                    ),
                    "current_status": None,
                    "current_activity": str(hermes_status.get("summary") or "Hermes setup is incomplete."),
                    "current_tool": None,
                    "current_tool_call_id": None,
                    "backlog_depth": 0,
                }
            )
            return
        if hermes_status.get("resolved_path"):
            entry["hermes_repo_path"] = str(hermes_status["resolved_path"])
        profile = infer_operator_profile(entry)
        if profile["placement"] == "attached" and profile["activation"] == "attach_only":
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)
            last_seen_age = _age_seconds(entry.get("last_seen_at"))
            attached_state = (
                "running" if last_seen_age is not None and last_seen_age <= RUNTIME_STALE_AFTER_SECONDS else "stale"
            )
            entry.update(
                {
                    "effective_state": attached_state if desired_state == "running" else "stopped",
                    "runtime_instance_id": None,
                    "backlog_depth": 0,
                    "current_tool": None,
                    "current_tool_call_id": None,
                }
            )
            if str(entry.get("last_error") or "") == f"Unsupported runtime_type: {entry.get('runtime_type')}":
                entry["last_error"] = None
            if str(entry.get("current_status") or "").strip().lower() in {"queued", "error"}:
                entry["current_status"] = None
                if str(entry.get("current_activity") or "").strip().lower().startswith("queued in gateway"):
                    entry["current_activity"] = None
            return
        allowed_to_run = (
            desired_state == "running"
            and attestation_state in {None, "verified"}
            and approval_state not in {"pending", "rejected"}
            and identity_status in {None, "verified"}
            and environment_status not in {"environment_mismatch", "environment_blocked"}
            and space_status not in {"active_not_allowed", "no_active_space"}
        )
        if allowed_to_run:
            if runtime is not None:
                restart_fields = (
                    "space_id",
                    "base_url",
                    "agent_id",
                    "token_file",
                    "runtime_type",
                    "exec_command",
                    "workdir",
                    "model",
                    "restart_requested_at",
                )
                changed_fields = [
                    field
                    for field in restart_fields
                    if str(runtime.entry.get(field) or "") != str(entry.get(field) or "")
                ]
                # If the only difference on space_id is that the runtime's
                # cached entry held a non-UUID (legacy corruption that
                # `reconcile_corrupt_space_ids` just repaired on load), the
                # space hasn't actually changed — it's a clean-up. Drop
                # `space_id` from the change set so we don't emit a phantom
                # `runtime_rebinding` event on every registry load.
                if "space_id" in changed_fields:
                    prev_sid = str(runtime.entry.get("space_id") or "").strip()
                    new_sid = str(entry.get("space_id") or "").strip()
                    if prev_sid and not looks_like_space_uuid(prev_sid) and looks_like_space_uuid(new_sid):
                        changed_fields = [f for f in changed_fields if f != "space_id"]
                        runtime.entry["space_id"] = new_sid
                if changed_fields:
                    record_gateway_activity(
                        "runtime_rebinding",
                        entry=entry,
                        changed_fields=changed_fields,
                        previous_space_id=runtime.entry.get("space_id"),
                        new_space_id=entry.get("space_id"),
                    )
                    runtime.stop()
                    self._runtimes.pop(name, None)
                    runtime = None
            if runtime is None:
                runtime = ManagedAgentRuntime(entry, client_factory=self.client_factory, logger=self.logger)
                self._runtimes[name] = runtime
                runtime.start()
            else:
                runtime.entry.update(entry)
                runtime.start()
        else:
            if runtime is not None:
                runtime.stop()
                self._runtimes.pop(name, None)
            entry.update(
                {
                    "effective_state": "stopped",
                    "runtime_instance_id": None,
                    "backlog_depth": 0,
                    "current_status": None,
                    "current_activity": None,
                    "current_tool": None,
                    "current_tool_call_id": None,
                }
            )

    def _reconcile_registry(self, registry: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        _ensure_registry_lists(registry)
        agents = registry.setdefault("agents", [])
        agent_names = {str(entry.get("name") or "") for entry in agents}
        for name, runtime in list(self._runtimes.items()):
            if name not in agent_names:
                runtime.stop()
                self._runtimes.pop(name, None)

        for entry in agents:
            entry.setdefault("transport", "gateway")
            entry.setdefault("credential_source", "gateway")
            entry.setdefault("runtime_type", "echo")
            entry.setdefault("desired_state", "stopped")
            if not str(entry.get("install_id") or "").strip():
                entry["install_id"] = str(uuid.uuid4())

            # Hidden + archived entries are out-of-roster: stop any runtime
            # we may have started for them and skip the heavy per-agent work
            # (identity binding refresh, attestation eval, runtime reconcile).
            # They stay in the registry for the UI but the daemon won't talk
            # to paxai.app on their behalf — that's the difference between
            # "hidden" and "active". Unhide / restore reverts lifecycle_phase
            # to "active" and the next tick processes them normally.
            phase = str(entry.get("lifecycle_phase") or "active").strip().lower()
            if phase in {"hidden", "archived"}:
                name = str(entry.get("name") or "")
                runtime = self._runtimes.get(name)
                if runtime is not None:
                    runtime.stop()
                    self._runtimes.pop(name, None)
                continue

            if entry.get("setup_disabled"):
                name = str(entry.get("name") or "")
                runtime = self._runtimes.get(name)
                if runtime is not None:
                    runtime.stop()
                    self._runtimes.pop(name, None)
                continue

            asset_id = _asset_id_for_entry(entry)
            existing_binding = (
                find_binding(registry, install_id=str(entry.get("install_id") or "").strip()) if asset_id else None
            )
            if (
                not existing_binding
                and asset_id
                and not _bindings_for_asset(registry, asset_id)
                and not _entry_requires_operator_approval(entry)
            ):
                ensure_local_asset_binding(
                    registry,
                    entry,
                    created_via=str(entry.get("created_via") or "legacy_registry"),
                    auto_approve=True,
                )
            ensure_gateway_identity_binding(
                registry,
                entry,
                session=session,
                created_via=str(entry.get("created_via") or "legacy_registry"),
            )
            entry.update(evaluate_identity_space_binding(registry, entry))

            previous_attestation = (
                str(entry.get("attestation_state") or ""),
                str(entry.get("approval_state") or ""),
                str(entry.get("approval_id") or ""),
                str(entry.get("drift_reason") or ""),
            )
            attestation = evaluate_runtime_attestation(registry, entry)
            entry.update(attestation)
            current_attestation = (
                str(entry.get("attestation_state") or ""),
                str(entry.get("approval_state") or ""),
                str(entry.get("approval_id") or ""),
                str(entry.get("drift_reason") or ""),
            )
            if current_attestation != previous_attestation:
                state = str(entry.get("attestation_state") or "")
                if state == "verified":
                    record_gateway_activity(
                        "runtime_attested",
                        entry=entry,
                        install_id=entry.get("install_id"),
                        attestation_state=state,
                    )
                elif state == "drifted":
                    record_gateway_activity(
                        "attestation_drift_detected",
                        entry=entry,
                        install_id=entry.get("install_id"),
                        attestation_state=state,
                        approval_id=entry.get("approval_id"),
                        drift_reason=entry.get("drift_reason"),
                    )
                elif state in {"unknown", "blocked"}:
                    record_gateway_activity(
                        "invocation_blocked",
                        entry=entry,
                        install_id=entry.get("install_id"),
                        attestation_state=state,
                        approval_id=entry.get("approval_id"),
                        reason=entry.get("confidence_reason"),
                    )
            self._reconcile_runtime(entry)
            runtime = self._runtimes.get(str(entry.get("name") or ""))
            snapshot = (
                runtime.snapshot()
                if runtime is not None
                else {
                    "effective_state": entry.get("effective_state") or "stopped",
                    "runtime_instance_id": None,
                    "last_error": entry.get("last_error"),
                    "current_status": entry.get("current_status"),
                    "current_activity": entry.get("current_activity"),
                    "current_tool": entry.get("current_tool"),
                    "current_tool_call_id": entry.get("current_tool_call_id"),
                    "backlog_depth": int(entry.get("backlog_depth") or 0),
                }
            )
            entry.update(snapshot)
            entry.update(annotate_runtime_health(entry, registry=registry))

        gateway = registry.setdefault("gateway", {})
        gateway.update(
            {
                "desired_state": "running",
                "effective_state": "running" if session else "stopped",
                "session_connected": bool(session),
                "pid": os.getpid(),
                "last_started_at": gateway.get("last_started_at") or _now_iso(),
                "last_reconcile_at": _now_iso(),
            }
        )
        return registry

    def _sweep_client(self, session: dict[str, Any] | None) -> Any | None:
        """Build a session-bound client for upstream lifecycle signals.

        Returns None if the session is missing or client construction fails;
        local sweep work continues either way.
        """
        if not session:
            return None
        token = session.get("token")
        if not token:
            return None
        try:
            return self.client_factory(
                base_url=session.get("base_url"),
                token=token,
            )
        except Exception:  # noqa: BLE001
            return None

    def _sweep_lifecycle(
        self,
        registry: dict[str, Any],
        *,
        session: dict[str, Any] | None,
    ) -> None:
        """Per-tick sweep: observe liveness and skip non-roster agents.

        Hide and restore are operator-driven only. The sweep never mutates
        ``lifecycle_phase``. Use ``ax gateway agents hide`` / ``unhide``
        (or the Cleanup UI) to change lifecycle phase.

        Upstream liveness signaling (heartbeats) is intentionally absent here:
        the heartbeat endpoint requires an agent-bound token; the sweep's
        user token is always rejected (400 "Not a bound agent session").
        Connected heartbeats are sent from _listener_loop using the agent's
        own bound client. Offline is signaled from stop(). Stale/setup_error
        require a management endpoint that accepts user-admin tokens — not
        yet available.
        """
        agents = registry.get("agents") or []
        if not agents:
            return
        for entry in agents:
            if not isinstance(entry, dict):
                continue
            if _is_system_agent(entry):
                continue
            phase = str(entry.get("lifecycle_phase") or "active").strip().lower()
            if phase not in _LIFECYCLE_PHASES:
                phase = "active"

            # Out-of-roster phases (archived, hidden) get no upstream traffic
            # from the sweep. Archive already signaled upstream once; hidden
            # is operator-driven "remove from runtime" and shouldn't keep
            # heartbeating to paxai.app while the operator has taken the
            # agent out of the active set.
            if phase in {"archived", "hidden"}:
                continue

            if entry.get("setup_disabled"):
                continue

            # Placeholder: the sweep loop is retained for future per-tick
            # registry maintenance (e.g. auto-hide long-stale agents, clean
            # up orphaned entries). Nothing to act on here yet.

    def run(self, *, once: bool = False) -> None:
        session = load_gateway_session()
        if not session:
            raise RuntimeError("Gateway login required. Run `ax gateway login` first.")

        existing_pids = active_gateway_pids()
        if existing_pids:
            existing_pid = existing_pids[0]
            record_gateway_activity(
                "gateway_start_blocked",
                pid=os.getpid(),
                existing_pid=existing_pid,
                existing_pids=existing_pids,
            )
            raise RuntimeError(f"Gateway already running (pid {existing_pid}).")

        write_gateway_pid(os.getpid())
        registry = load_gateway_registry()
        registry.setdefault("gateway", {})
        registry["gateway"]["last_started_at"] = registry["gateway"].get("last_started_at") or _now_iso()
        record_gateway_activity("gateway_started", pid=os.getpid())
        previous_handlers: dict[signal.Signals, Any] = {}

        def _request_stop(_signum: int, _frame: Any) -> None:
            self.stop()

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _request_stop)

        _gw_heartbeat_client = None
        _gw_heartbeat_id = session.get("gateway_id")
        _gw_last_heartbeat = 0.0
        _GW_HEARTBEAT_INTERVAL = 60.0
        if _gw_heartbeat_id:
            try:
                _gw_heartbeat_client = self.client_factory(
                    base_url=str(session.get("base_url") or ""),
                    token=str(session.get("token") or ""),
                )
            except Exception:
                _gw_heartbeat_client = None

        try:
            while not self._stop.is_set():
                registry = load_gateway_registry()
                registry = self._reconcile_registry(registry, session)
                self._sweep_lifecycle(registry, session=session)
                save_gateway_registry(registry)
                if _gw_heartbeat_client and _gw_heartbeat_id:
                    _now_mono = time.monotonic()
                    if _now_mono - _gw_last_heartbeat >= _GW_HEARTBEAT_INTERVAL:
                        try:
                            _gw_heartbeat_client.send_gateway_heartbeat(_gw_heartbeat_id)
                        except Exception:
                            pass
                        _gw_last_heartbeat = _now_mono
                if once:
                    break
                time.sleep(self.poll_interval)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            runtimes = list(self._runtimes.values())
            for runtime in runtimes:
                if _is_sentinel_inference_sdk_runtime(runtime.entry.get("runtime_type")):
                    runtime.stop(timeout=2.0)
            for runtime in runtimes:
                if not _is_sentinel_inference_sdk_runtime(runtime.entry.get("runtime_type")):
                    runtime.stop(timeout=1.0)
            final_registry = load_gateway_registry()
            final_gateway = final_registry.setdefault("gateway", {})
            final_gateway.update(
                {
                    "desired_state": final_gateway.get("desired_state") or "stopped",
                    "effective_state": "stopped",
                    "session_connected": bool(session),
                    "pid": None,
                    "last_reconcile_at": _now_iso(),
                }
            )
            for entry in final_registry.get("agents", []):
                name = str(entry.get("name") or "")
                entry.update({"effective_state": "stopped", "backlog_depth": 0})
                runtime = self._runtimes.get(name)
                if runtime is not None:
                    entry.update(runtime.snapshot())
                entry.update(annotate_runtime_health(entry, registry=final_registry))
            save_gateway_registry(final_registry)
            record_gateway_activity("gateway_stopped")
            clear_gateway_pid(os.getpid())


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_assets import hermes_setup_status, infer_operator_profile  # noqa: E402
from .gateway_governance import (  # noqa: E402
    _entry_requires_operator_approval,
    ensure_local_asset_binding,
    evaluate_runtime_attestation,
)
from .gateway_health import _age_seconds, _now_iso, annotate_runtime_health  # noqa: E402
from .gateway_identity import (  # noqa: E402
    _asset_id_for_entry,
    _bindings_for_asset,
    _ensure_registry_lists,
    ensure_gateway_identity_binding,
    evaluate_identity_space_binding,
    find_binding,
)
from .gateway_runtime import ManagedAgentRuntime, RuntimeLogger, _is_hermes_plugin_runtime  # noqa: E402
from .gateway_state import _external_runtime_connected, _external_runtime_expected  # noqa: E402
from .gateway_storage import (  # noqa: E402
    active_gateway_pids,
    clear_gateway_pid,
    load_gateway_registry,
    load_gateway_session,
    looks_like_space_uuid,
    record_gateway_activity,
    save_gateway_registry,
    write_gateway_pid,
)
