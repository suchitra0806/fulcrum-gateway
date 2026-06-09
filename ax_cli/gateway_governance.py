"""Gateway approval CRUD, local asset binding, and runtime attestation.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import uuid
from typing import Any

from .gateway_constants import _normalized_optional_controlled


def _approval_status(approval: dict[str, Any]) -> str:
    status = str(approval.get("status") or "").strip().lower()
    if status == "denied":
        return "rejected"
    return status


def _find_approval_by_id(registry: dict[str, Any], approval_id: str) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    for approval in registry.get("approvals", []):
        if str(approval.get("approval_id") or "") == approval_id:
            return approval
    return None


def _find_approval_for_signature(registry: dict[str, Any], candidate_signature: str) -> dict[str, Any] | None:
    _ensure_registry_lists(registry)
    matches = [
        approval
        for approval in registry.get("approvals", [])
        if str(approval.get("candidate_signature") or "") == candidate_signature
        and _approval_status(approval) != "archived"
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("requested_at") or ""))[-1]


def _approval_is_stale(registry: dict[str, Any], approval: dict[str, Any]) -> bool:
    if _approval_status(approval) != "pending":
        return False

    asset_id = str(approval.get("asset_id") or "").strip()
    install_id = str(approval.get("install_id") or "").strip()
    signature = str(approval.get("candidate_signature") or "").strip()
    if not asset_id and not install_id:
        return True

    matching_entries = [
        entry
        for entry in registry.get("agents", [])
        if (asset_id and str(entry.get("asset_id") or entry.get("agent_id") or "") == asset_id)
        or (install_id and str(entry.get("install_id") or "") == install_id)
    ]
    if not matching_entries:
        return True

    gateway_id = _gateway_id_from_registry(registry)
    for entry in matching_entries:
        candidate = _binding_candidate_for_entry(entry, registry)
        if signature and str(candidate.get("candidate_signature") or "") != signature:
            continue
        binding = find_binding(registry, install_id=str(entry.get("install_id") or ""))
        if not binding:
            return False
        if str(binding.get("gateway_id") or "") != gateway_id:
            return False
        if str(binding.get("asset_id") or "") != str(candidate.get("asset_id") or ""):
            return False
        if str(binding.get("approved_state") or "approved").lower() == "rejected":
            return False
        if str(binding.get("path") or "") != str(candidate.get("path") or ""):
            return False
        if str(binding.get("launch_spec_hash") or "") != str(candidate.get("launch_spec_hash") or ""):
            return False
        return True

    return True


def archive_stale_gateway_approvals(*, decided_by: str | None = None) -> dict[str, Any]:
    registry = load_gateway_registry()
    _ensure_registry_lists(registry)
    archived: list[dict[str, Any]] = []
    for approval in registry.get("approvals", []):
        if not _approval_is_stale(registry, approval):
            continue
        approval["status"] = "archived"
        approval["decision"] = "archive"
        approval["decided_by"] = decided_by or "local_gateway_operator"
        approval["decided_at"] = _now_iso()
        approval["archived_reason"] = "Approval no longer matches a current managed agent binding."
        archived.append(dict(approval))
    if archived:
        save_gateway_registry(registry)
    return {
        "archived": archived,
        "archived_count": len(archived),
        "remaining_pending": len(
            [item for item in registry.get("approvals", []) if _approval_status(item) == "pending"]
        ),
    }


def list_gateway_approvals(*, status: str | None = None, include_archived: bool = False) -> list[dict[str, Any]]:
    registry = load_gateway_registry()
    _ensure_registry_lists(registry)
    normalized_status = _normalized_optional_controlled(status, {"pending", "approved", "rejected", "archived"})
    approvals: list[dict[str, Any]] = []
    for approval in registry.get("approvals", []):
        row = dict(approval)
        row["status"] = _approval_status(row)
        if row["status"] == "archived" and not include_archived and normalized_status != "archived":
            continue
        if normalized_status and row["status"] != normalized_status:
            continue
        approvals.append(row)
    approvals.sort(key=lambda item: str(item.get("requested_at") or ""), reverse=True)
    return approvals


def get_gateway_approval(approval_id: str) -> dict[str, Any]:
    registry = load_gateway_registry()
    approval = _find_approval_by_id(registry, approval_id)
    if approval is None:
        raise LookupError(f"Approval not found: {approval_id}")
    result = dict(approval)
    result["status"] = _approval_status(result)
    return result


def _refresh_attestation_for_matching_entries(
    registry: dict[str, Any],
    *,
    install_id: str | None = None,
    asset_id: str | None = None,
) -> None:
    for entry in registry.get("agents", []):
        if install_id and str(entry.get("install_id") or "") != install_id:
            continue
        if asset_id and _asset_id_for_entry(entry) != asset_id:
            continue
        ensure_gateway_identity_binding(registry, entry)
        entry.update(evaluate_identity_space_binding(registry, entry))
        entry.update(evaluate_runtime_attestation(registry, entry))


def approve_gateway_approval(
    approval_id: str, *, scope: str = "asset", decided_by: str | None = None
) -> dict[str, Any]:
    normalized_scope = str(scope or "asset").strip().lower()
    if normalized_scope not in {"once", "asset", "gateway"}:
        raise ValueError("Approval scope must be one of: once, asset, gateway.")
    registry = load_gateway_registry()
    approval = _find_approval_by_id(registry, approval_id)
    if approval is None:
        raise LookupError(f"Approval not found: {approval_id}")
    candidate_binding = (
        approval.get("candidate_binding") if isinstance(approval.get("candidate_binding"), dict) else None
    )
    if not candidate_binding:
        raise ValueError("Approval is missing its candidate binding.")
    now = _now_iso()
    approval["status"] = "approved"
    approval["decision"] = "approve"
    approval["decision_scope"] = normalized_scope
    approval["decided_at"] = now
    approval["decided_by"] = decided_by or "local_gateway_operator"
    binding = dict(candidate_binding)
    binding["approved_state"] = "approved"
    binding["approved_at"] = now
    binding["approval_scope"] = normalized_scope
    binding["last_verified_at"] = now
    stored_binding = upsert_binding(registry, binding)
    _refresh_attestation_for_matching_entries(
        registry,
        install_id=str(approval.get("install_id") or "") or None,
        asset_id=str(approval.get("asset_id") or "") or None,
    )
    save_gateway_registry(registry)
    _record_governance_activity(
        "approval_granted",
        asset_id=approval.get("asset_id"),
        install_id=approval.get("install_id"),
        approval_id=approval.get("approval_id"),
        decision_scope=normalized_scope,
        decided_by=approval["decided_by"],
        gateway_id=approval.get("gateway_id"),
        path=stored_binding.get("path"),
    )
    result = dict(approval)
    result["status"] = _approval_status(result)
    return {"approval": result, "binding": stored_binding}


def deny_gateway_approval(approval_id: str, *, decided_by: str | None = None) -> dict[str, Any]:
    registry = load_gateway_registry()
    approval = _find_approval_by_id(registry, approval_id)
    if approval is None:
        raise LookupError(f"Approval not found: {approval_id}")
    now = _now_iso()
    approval["status"] = "rejected"
    approval["decision"] = "deny"
    approval["decided_at"] = now
    approval["decided_by"] = decided_by or "local_gateway_operator"
    _refresh_attestation_for_matching_entries(
        registry,
        install_id=str(approval.get("install_id") or "") or None,
        asset_id=str(approval.get("asset_id") or "") or None,
    )
    save_gateway_registry(registry)
    _record_governance_activity(
        "approval_denied",
        asset_id=approval.get("asset_id"),
        install_id=approval.get("install_id"),
        approval_id=approval.get("approval_id"),
        decided_by=approval["decided_by"],
        gateway_id=approval.get("gateway_id"),
    )
    result = dict(approval)
    result["status"] = _approval_status(result)
    return result


def _record_governance_activity(event: str, *, entry: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return record_gateway_activity(event, entry=entry, **fields)


def ensure_local_asset_binding(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    created_via: str | None = None,
    auto_approve: bool = True,
    replace_existing: bool = False,
) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip()
    if not install_id:
        install_id = str(uuid.uuid4())
        entry["install_id"] = install_id
    existing = find_binding(registry, install_id=install_id) or find_binding(
        registry, asset_id=asset_id, gateway_id=gateway_id
    )
    if existing:
        entry["install_id"] = str(existing.get("install_id") or install_id)
        if not replace_existing:
            return existing
        candidate = _binding_candidate_for_entry(
            {**entry, "created_via": created_via or entry.get("created_via")}, registry
        )
        candidate["first_seen_at"] = str(existing.get("first_seen_at") or candidate.get("first_seen_at") or _now_iso())
        if auto_approve:
            candidate["approved_state"] = "approved"
            candidate["approved_at"] = _now_iso()
        binding = upsert_binding(registry, candidate)
        if str(existing.get("candidate_signature") or "") != str(binding.get("candidate_signature") or ""):
            _record_governance_activity(
                "asset_binding_updated",
                entry=entry,
                asset_id=asset_id,
                install_id=entry["install_id"],
                binding_type=binding.get("binding_type"),
                gateway_id=gateway_id,
                path=binding.get("path"),
            )
        return binding
    candidate = _binding_candidate_for_entry(
        {**entry, "created_via": created_via or entry.get("created_via")}, registry
    )
    if auto_approve:
        candidate["approved_state"] = "approved"
        candidate["approved_at"] = _now_iso()
    binding = upsert_binding(registry, candidate)
    entry["install_id"] = str(binding.get("install_id") or install_id)
    _record_governance_activity(
        "asset_bound",
        entry=entry,
        asset_id=asset_id,
        install_id=entry["install_id"],
        binding_type=binding.get("binding_type"),
        gateway_id=gateway_id,
        path=binding.get("path"),
    )
    return binding


def _entry_requires_operator_approval(entry: dict[str, Any]) -> bool:
    template_id = str(entry.get("template_id") or "").strip().lower()
    return bool(entry.get("requires_approval")) or template_id in {"pass_through"}


def _create_binding_approval(
    registry: dict[str, Any],
    entry: dict[str, Any],
    *,
    candidate_binding: dict[str, Any],
    action: str,
    reason: str,
    risk: str,
    approval_kind: str,
) -> dict[str, Any]:
    existing = _find_approval_for_signature(registry, str(candidate_binding.get("candidate_signature") or ""))
    if existing:
        return existing
    approval = {
        "approval_id": str(uuid.uuid4()),
        "asset_id": candidate_binding.get("asset_id"),
        "gateway_id": candidate_binding.get("gateway_id"),
        "install_id": candidate_binding.get("install_id"),
        "action": action,
        "resource": candidate_binding.get("path") or candidate_binding.get("launch_spec_hash"),
        "reason": reason,
        "risk": risk,
        "status": "pending",
        "decision": None,
        "requested_at": _now_iso(),
        "expires_at": None,
        "candidate_signature": candidate_binding.get("candidate_signature"),
        "candidate_binding": candidate_binding,
        "approval_kind": approval_kind,
    }
    registry.setdefault("approvals", []).append(approval)
    _record_governance_activity(
        "approval_requested",
        entry=entry,
        approval_id=approval["approval_id"],
        asset_id=approval["asset_id"],
        install_id=approval["install_id"],
        approval_kind=approval_kind,
        reason=reason,
        risk=risk,
    )
    return approval


def evaluate_runtime_attestation(registry: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    _ensure_registry_lists(registry)
    gateway_id = _gateway_id_from_registry(registry)
    asset_id = _asset_id_for_entry(entry)
    install_id = str(entry.get("install_id") or "").strip()
    candidate = _binding_candidate_for_entry(entry, registry)
    latest_approval = _find_approval_for_signature(registry, candidate["candidate_signature"])

    def blocked(
        reason: str, detail: str, *, approval: dict[str, Any] | None = None, state: str = "blocked"
    ) -> dict[str, Any]:
        return {
            "asset_id": asset_id or None,
            "gateway_id": gateway_id,
            "install_id": install_id or candidate["install_id"],
            "binding": None,
            "candidate_binding": candidate,
            "runtime_instance_id": str(entry.get("runtime_instance_id") or "") or None,
            "attestation_state": state,
            "drift_reason": reason,
            "approval_state": "rejected"
            if approval and _approval_status(approval) == "rejected"
            else ("pending" if approval and _approval_status(approval) == "pending" else "not_required"),
            "approval_id": approval.get("approval_id") if approval else None,
            "confidence_reason": reason,
            "confidence_detail": detail,
        }

    if not asset_id:
        return blocked("unknown_asset", "Runtime is missing a registered asset identity.")

    install_binding = find_binding(registry, install_id=install_id) if install_id else None
    asset_bindings = _bindings_for_asset(registry, asset_id)

    if install_binding and str(install_binding.get("asset_id") or "") != asset_id:
        return blocked("asset_mismatch", "Runtime install is bound to a different asset id than the one it claimed.")

    if latest_approval and _approval_status(latest_approval) == "rejected":
        return blocked(
            "approval_denied", "A prior approval request for this runtime binding was denied.", approval=latest_approval
        )

    if not install_binding:
        if asset_bindings:
            same_gateway = next(
                (binding for binding in asset_bindings if str(binding.get("gateway_id") or "") == gateway_id), None
            )
            if same_gateway is None:
                approval = latest_approval or _create_binding_approval(
                    registry,
                    entry,
                    candidate_binding=candidate,
                    action="runtime.bind",
                    reason="Asset is requesting access from a different Gateway than the approved binding.",
                    risk="high",
                    approval_kind="new_gateway",
                )
                return blocked(
                    "new_gateway",
                    "This asset is requesting access from a new Gateway and needs approval.",
                    approval=approval,
                    state="unknown",
                )
        approval = latest_approval or _create_binding_approval(
            registry,
            entry,
            candidate_binding=candidate,
            action="runtime.bind",
            reason="Gateway discovered a runtime binding that has not been approved yet.",
            risk="medium",
            approval_kind="new_binding",
        )
        return blocked(
            "approval_required",
            "Gateway needs approval before trusting this new asset binding.",
            approval=approval,
            state="unknown",
        )

    binding = install_binding
    if str(binding.get("gateway_id") or "") != gateway_id:
        approval = latest_approval or _create_binding_approval(
            registry,
            entry,
            candidate_binding=candidate,
            action="runtime.bind",
            reason="Asset binding is attempting to run from a different Gateway than the approved one.",
            risk="high",
            approval_kind="new_gateway",
        )
        return blocked(
            "new_gateway",
            "This asset binding is tied to a different Gateway and needs approval.",
            approval=approval,
            state="unknown",
        )

    if str(binding.get("approved_state") or "approved").lower() == "rejected":
        return blocked("approval_denied", "This asset binding was previously rejected.")

    current_path = str(candidate.get("path") or "")
    bound_path = str(binding.get("path") or "")
    current_hash = str(candidate.get("launch_spec_hash") or "")
    bound_hash = str(binding.get("launch_spec_hash") or "")
    if current_path != bound_path or current_hash != bound_hash:
        approval = latest_approval or _create_binding_approval(
            registry,
            entry,
            candidate_binding=candidate,
            action="runtime.attest",
            reason="Runtime launch path or launch spec changed since approval.",
            risk="high",
            approval_kind="binding_drift",
        )
        detail = "Runtime launch path or spec changed since approval. Review and approve the new binding before Gateway will trust it."
        return {
            "asset_id": asset_id,
            "gateway_id": gateway_id,
            "install_id": str(binding.get("install_id") or candidate["install_id"]),
            "binding": binding,
            "candidate_binding": candidate,
            "runtime_instance_id": str(entry.get("runtime_instance_id") or "") or None,
            "attestation_state": "drifted",
            "drift_reason": "binding_drift",
            "approval_state": "pending" if approval and _approval_status(approval) == "pending" else "not_required",
            "approval_id": approval.get("approval_id") if approval else None,
            "confidence_reason": "binding_drift",
            "confidence_detail": detail,
        }

    return {
        "asset_id": asset_id,
        "gateway_id": gateway_id,
        "install_id": str(binding.get("install_id") or candidate["install_id"]),
        "binding": binding,
        "candidate_binding": candidate,
        "runtime_instance_id": str(entry.get("runtime_instance_id") or "") or None,
        "attestation_state": "verified",
        "drift_reason": None,
        "approval_state": "not_required",
        "approval_id": None,
        "confidence_reason": None,
        "confidence_detail": "Runtime matches the approved local binding.",
    }


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_health import _now_iso  # noqa: E402
from .gateway_identity import (  # noqa: E402
    _asset_id_for_entry,
    _binding_candidate_for_entry,
    _bindings_for_asset,
    _ensure_registry_lists,
    _gateway_id_from_registry,
    ensure_gateway_identity_binding,
    evaluate_identity_space_binding,
    find_binding,
    upsert_binding,
)
from .gateway_storage import load_gateway_registry, record_gateway_activity, save_gateway_registry  # noqa: E402
