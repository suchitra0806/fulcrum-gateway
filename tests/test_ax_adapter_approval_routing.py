"""Tests for the aX adapter's dangerous-command approval routing (#72).

The adapter imports ``gateway.*`` from a hermes-agent install that is not on
ax-gateway's own venv path, so the other adapter test modules skip cleanly
when those imports fail. The approval-routing logic, however, is the whole
point of this fix and must be exercised in *this* repo's CI — so this module
installs lightweight stubs for the handful of ``gateway.*`` / ``tools.*``
symbols the adapter touches at import time, then loads the adapter and tests
its pure helpers and redirect selection directly.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from collections import OrderedDict
from pathlib import Path


def _install_gateway_stubs() -> None:
    """Register minimal stand-ins so adapter.py imports without hermes-agent."""
    if "gateway.session" in sys.modules:
        return

    gateway = types.ModuleType("gateway")
    config = types.ModuleType("gateway.config")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    session = types.ModuleType("gateway.session")

    class _Platform(str):
        def __new__(cls, value):
            obj = super().__new__(cls, value)
            obj.value = value
            return obj

    class _PlatformConfig:  # pragma: no cover - constructed only by real runtime
        pass

    class _BasePlatformAdapter:
        def __init__(self, *a, **k):
            pass

    class _MessageType:
        TEXT = "text"

    class _SendResult:  # pragma: no cover - not used by these tests
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _MessageEvent:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _SessionSource:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def _build_session_key(source, *a, **k):
        # Deterministic and root-only, mirroring the real aX thread key shape.
        return f"agent:main:ax:thread:{source.chat_id}:{source.thread_id}"

    config.Platform = _Platform
    config.PlatformConfig = _PlatformConfig
    base.BasePlatformAdapter = _BasePlatformAdapter
    base.MessageEvent = _MessageEvent
    base.MessageType = _MessageType
    base.SendResult = _SendResult
    session.SessionSource = _SessionSource
    session.build_session_key = _build_session_key

    gateway.config = config
    gateway.platforms = platforms
    platforms.base = base
    gateway.session = session

    sys.modules.update(
        {
            "gateway": gateway,
            "gateway.config": config,
            "gateway.platforms": platforms,
            "gateway.platforms.base": base,
            "gateway.session": session,
        }
    )


_install_gateway_stubs()

_SPEC = importlib.util.spec_from_file_location(
    "ax_adapter_approval_routing_under_test",
    Path(__file__).resolve().parents[1] / "ax_cli" / "plugins" / "platforms" / "ax" / "adapter.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

AxAdapter = _MODULE.AxAdapter
_is_approval_command = _MODULE._is_approval_command
_resolve_thread_root = _MODULE._resolve_thread_root
_select_approval_redirect = _MODULE._select_approval_redirect
MAX_REMEMBERED_SESSIONS = _MODULE.MAX_REMEMBERED_SESSIONS


def _adapter() -> "AxAdapter":
    adapter = AxAdapter.__new__(AxAdapter)
    adapter.agent_name = "nova"
    adapter.space_id = "space_abcdef123456"
    adapter.platform = _MODULE.Platform("ax")
    adapter._recent_roots = OrderedDict()
    return adapter


# ── _is_approval_command ───────────────────────────────────────────────────


class TestIsApprovalCommand:
    def test_bare_approve_and_deny(self):
        assert _is_approval_command("/approve")
        assert _is_approval_command("/deny")

    def test_bang_alias_prefix(self):
        assert _is_approval_command("!approve")
        assert _is_approval_command("!deny")

    def test_arguments_allowed(self):
        for cmd in (
            "/approve all",
            "/approve session",
            "/approve always",
            "/approve all session",
            "/approve all always",
            "/deny all",
            "/approve permanently",
        ):
            assert _is_approval_command(cmd), cmd

    def test_surrounding_whitespace_tolerated(self):
        assert _is_approval_command("  /approve  ")
        assert _is_approval_command("/deny\n")

    def test_case_insensitive(self):
        assert _is_approval_command("/APPROVE")
        assert _is_approval_command("/Deny ALL")

    def test_rejects_command_in_a_sentence(self):
        # Must be the WHOLE message — never fire on prose that mentions /approve.
        assert not _is_approval_command("please /approve the budget")
        assert not _is_approval_command("/approve the deploy")
        assert not _is_approval_command("can you /deny that request")

    def test_rejects_non_approval_commands_and_lookalikes(self):
        for text in ("/stop", "/new", "approve", "deny", "/approved", "/denylist", "", "   "):
            assert not _is_approval_command(text), text


# ── _resolve_thread_root ───────────────────────────────────────────────────


class TestResolveThreadRoot:
    def test_conversation_id_wins(self):
        data = {"conversation_id": "root1", "parent_id": "p1"}
        assert _resolve_thread_root(data, "m1") == "root1"

    def test_falls_back_to_parent_id(self):
        assert _resolve_thread_root({"parent_id": "p1"}, "m1") == "p1"

    def test_parentid_and_thread_id_aliases(self):
        assert _resolve_thread_root({"parentId": "p2"}, "m1") == "p2"
        assert _resolve_thread_root({"thread_id": "t1"}, "m1") == "t1"

    def test_falls_back_to_message_id_for_top_level(self):
        assert _resolve_thread_root({}, "m1") == "m1"

    def test_blank_conversation_id_ignored(self):
        assert _resolve_thread_root({"conversation_id": "  ", "parent_id": "p1"}, "m1") == "p1"

    def test_matches_legacy_behavior_without_conversation_id(self):
        # Old logic was `parent_id or message_id`; verify byte-for-byte parity.
        assert _resolve_thread_root({"parent_id": "p"}, "m") == "p"
        assert _resolve_thread_root({}, "m") == "m"


# ── _select_approval_redirect ──────────────────────────────────────────────


class TestSelectApprovalRedirect:
    def test_current_session_blocked_never_redirects(self):
        # In-thread /approve already works — leave it alone.
        assert _select_approval_redirect("cur", ["cur", "other"], lambda k: True) is None

    def test_single_other_blocked_is_chosen(self):
        blocked = {"blocked"}
        out = _select_approval_redirect("cur", ["cur", "blocked", "idle"], lambda k: k in blocked)
        assert out == "blocked"

    def test_no_blocked_returns_none(self):
        assert _select_approval_redirect("cur", ["a", "b"], lambda k: False) is None

    def test_ambiguous_multiple_blocked_fails_closed(self):
        blocked = {"b1", "b2"}
        assert _select_approval_redirect("cur", ["b1", "b2"], lambda k: k in blocked) is None

    def test_dedupes_candidate_keys(self):
        calls = []

        def is_blocked(k):
            calls.append(k)
            return k == "blocked"

        out = _select_approval_redirect("cur", ["blocked", "blocked", "blocked"], is_blocked)
        assert out == "blocked"
        # Deduped: the same key isn't probed three times.
        assert calls.count("blocked") == 1


# ── _remember_session (LRU) ────────────────────────────────────────────────


class TestRememberSession:
    def test_records_and_bounds_to_max(self):
        adapter = _adapter()
        for i in range(MAX_REMEMBERED_SESSIONS + 10):
            adapter._remember_session(f"key{i}", f"root{i}")
        assert len(adapter._recent_roots) == MAX_REMEMBERED_SESSIONS
        # Oldest evicted, newest retained.
        assert "key0" not in adapter._recent_roots
        assert f"key{MAX_REMEMBERED_SESSIONS + 9}" in adapter._recent_roots

    def test_reinsert_refreshes_recency(self):
        adapter = _adapter()
        adapter._remember_session("a", "ra")
        adapter._remember_session("b", "rb")
        adapter._remember_session("a", "ra")  # touch 'a' so 'b' is now oldest
        # Fill to force a single eviction.
        for i in range(MAX_REMEMBERED_SESSIONS):
            adapter._remember_session(f"f{i}", f"r{i}")
        assert "b" not in adapter._recent_roots


# ── _approval_redirect_root (lazy tools.approval import) ───────────────────


class TestApprovalRedirectRoot:
    def test_routes_to_unique_blocked_session(self, monkeypatch):
        adapter = _adapter()
        adapter._remember_session("agent:main:ax:thread:R1:R1", "R1")
        adapter._remember_session("agent:main:ax:thread:R2:R2", "R2")

        approval = types.ModuleType("tools.approval")
        approval.has_blocking_approval = lambda key: key == "agent:main:ax:thread:R2:R2"
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.approval", approval)

        # A fresh top-level "@agent /approve" has its own (unblocked) key.
        assert adapter._approval_redirect_root("agent:main:ax:thread:NEW:NEW") == "R2"

    def test_returns_none_when_tools_unavailable(self, monkeypatch):
        adapter = _adapter()
        adapter._remember_session("agent:main:ax:thread:R1:R1", "R1")
        # No tools.approval in sys.modules and import will fail → defensive None.
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        sys.modules.pop("tools.approval", None)
        assert adapter._approval_redirect_root("agent:main:ax:thread:NEW:NEW") is None


# ── _dispatch_inbound wiring (the redirect/remember integration) ───────────


def _dispatch_adapter(captured: list) -> "AxAdapter":
    """An adapter wired for _dispatch_inbound: real helpers under test, the
    surrounding I/O (filtering, trigger-strip, handoff) stubbed to no-ops."""
    adapter = _adapter()
    adapter._seen_message_ids = OrderedDict()
    adapter._is_self_authored = lambda data: False
    adapter._is_for_me = lambda data: True
    # _clean_agent_trigger_text strips the "@agent" trigger; here the test
    # data already carries the bare command/text, so pass it through.
    adapter._clean_agent_trigger_text = lambda text: text

    async def _capture(event):
        captured.append(event)

    adapter.handle_message = _capture
    return adapter


class TestDispatchInbound:
    def test_approval_command_redirects_source_to_blocked_root(self, monkeypatch):
        captured: list = []
        adapter = _dispatch_adapter(captured)
        # A run was dispatched earlier on thread root R2 and is now blocked.
        adapter._remember_session("agent:main:ax:thread:R2:R2", "R2")
        approval = types.ModuleType("tools.approval")
        approval.has_blocking_approval = lambda key: key == "agent:main:ax:thread:R2:R2"
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.approval", approval)

        # Fresh top-level "@agent /approve" — its own root NEW has nothing pending.
        asyncio.run(
            adapter._dispatch_inbound({"id": "NEW", "content": "/approve", "sender": "alice", "sender_id": "u_alice"})
        )

        assert len(captured) == 1
        # Source was rewritten to the blocked session's root, not NEW.
        assert captured[0].source.chat_id == "R2"
        assert captured[0].source.thread_id == "R2"
        # An approval command is not itself remembered as a candidate run.
        assert "agent:main:ax:thread:NEW:NEW" not in adapter._recent_roots

    def test_non_approval_message_is_remembered(self, monkeypatch):
        captured: list = []
        adapter = _dispatch_adapter(captured)
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.approval", types.ModuleType("tools.approval"))

        asyncio.run(
            adapter._dispatch_inbound(
                {"id": "M1", "conversation_id": "ROOT", "content": "do the thing", "sender": "bob"}
            )
        )

        assert len(captured) == 1
        assert captured[0].source.chat_id == "ROOT"
        # The run is now a redirect candidate keyed on its thread root.
        assert adapter._recent_roots.get("agent:main:ax:thread:ROOT:ROOT") == "ROOT"

    def test_approval_command_with_no_blocked_session_is_not_redirected(self, monkeypatch):
        captured: list = []
        adapter = _dispatch_adapter(captured)
        approval = types.ModuleType("tools.approval")
        approval.has_blocking_approval = lambda key: False  # nothing pending anywhere
        monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
        monkeypatch.setitem(sys.modules, "tools.approval", approval)

        asyncio.run(
            adapter._dispatch_inbound({"id": "NEW", "content": "/approve", "sender": "alice", "sender_id": "u_alice"})
        )

        assert len(captured) == 1
        # Fails closed: source stays on the command's own root (current behavior).
        assert captured[0].source.chat_id == "NEW"
