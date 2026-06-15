"""aX platform adapter for the Hermes gateway.

Connects a Hermes agent to the aX multi-agent network at https://paxai.app
as a first-class messaging platform — alongside Telegram, Slack, Discord,
etc. Each @-mention received in the configured space arrives as a
``MessageEvent``; the agent's reply posts via REST and threads under the
original mention.

Design notes
------------

- **Plugin path, no core changes.** Discovered by Hermes's PluginManager at
  ``~/.hermes/plugins/ax/`` (or bundled). Registers itself via
  ``register(ctx)`` calling ``ctx.register_platform``. Native Hermes
  features (session continuity, tool callbacks, channel directory, cron
  delivery) light up automatically.

- **Identity model.** One adapter instance = one aX agent identity bound
  to one space. Token is the agent PAT (``axp_a_...``) minted by Gateway.
  PAT → JWT exchange via ``/auth/exchange`` (cached, refreshed on expiry)
  per AUTH-SPEC-001 §13.

- **chat_id mapping.** ``chat_id`` is the thread root: ``conversation_id``
  if aX supplies it, else ``parent_id`` (a direct reply), else the mention's
  own ``message_id``. Keying the whole conversation on the root keeps every
  turn — including a reply to the agent's own approval prompt — on one
  session, and lets out-of-thread ``/approve`` / ``/deny`` commands be routed
  back to the blocked session (see ``_approval_redirect_root``).

- **Filtering.** Only inbound events that (a) are not self-authored AND
  (b) explicitly @-mention this agent are dispatched. The aX SSE stream
  delivers all messages in the space; this filter is the equivalent of
  Telegram's bot-mention check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import quote

import httpx
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://paxai.app"
DEFAULT_LOCAL_GATEWAY_URL = "http://127.0.0.1:8765"
SSE_RECONNECT_BACKOFF_MAX = 60.0
SSE_IDLE_TIMEOUT = 90.0
JWT_REFRESH_BUFFER_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 30.0
LOCAL_GATEWAY_ANNOUNCE_TIMEOUT = 1.5
# aX's SSE stream emits BOTH `event: message` and `event: mention` for any
# message that contains a mention — same message_id, two events. Without
# dedup we'd dispatch the same inbound twice: the first call starts the
# Hermes run and marks the session active, the second hits the active-session
# guard and fires the "⚡ Interrupting current task" busy-ack template. The
# LRU also covers SSE reconnect-replay if aX ever sends backlog on resume.
SEEN_MESSAGE_LRU_MAX = 1024
AGENT_RUNTIME_SCOPE = "tasks:read tasks:write messages:read messages:write agents:read"
DEFAULT_AUDIENCE = "ax-api"
# How many recently-dispatched run sessions to remember for approval-command
# redirect (Part 2 of the #72 fix). Bounded so a long-lived listener can't grow
# this map without limit; an aX agent only has a handful of live threads at once.
MAX_REMEMBERED_SESSIONS = 64

# Bare /approve and /deny gateway commands (matched AFTER the leading
# "@agent" trigger mention is stripped by _clean_agent_trigger_text). These
# resolve a *pending* dangerous-command approval parked under the blocked
# session's key (gateway/slash_commands.py _handle_approve_command /
# _handle_deny_command). Accept the "!" alias prefix some clients rewrite to,
# and the optional all/session/always arguments — but only when the WHOLE
# message is the command, never when "/approve" appears mid-sentence.
_APPROVAL_COMMAND_RE = re.compile(
    r"^[/!](?:approve|deny)(?:\s+(?:all|session|ses|always|permanent|permanently))*\s*$",
    re.IGNORECASE,
)


def _is_approval_command(text: str) -> bool:
    """Return True if ``text`` is a bare /approve or /deny gateway command.

    aX threading means these commands frequently arrive on a different
    session key than the one holding the pending approval (a fresh
    top-level "@agent /approve" is the root of its own thread), so the
    adapter redirects them to the blocked session — see
    :meth:`AxAdapter._approval_redirect_root`.
    """
    return bool(_APPROVAL_COMMAND_RE.match((text or "").strip()))


def _resolve_thread_root(data: Dict[str, Any], message_id: str) -> str:
    """Resolve the stable conversation/thread root used for session keying.

    aX delivers ``conversation_id`` = the thread root for every message in a
    thread (the vendored sentinel relies on it for memory continuity — see
    ``ax_cli/runtimes/hermes/sentinel.py``). Preferring it keeps every turn
    of a conversation — including a reply to the agent's own approval prompt
    — on ONE session key, instead of splitting per replied-to message.

    Falls back to ``parent_id`` (the direct reply target) then the message's
    own id (a brand-new top-level mention is the root of its own thread).
    When ``conversation_id`` is absent this is byte-for-byte the previous
    ``parent_id or message_id`` behavior.
    """
    conversation_id = str(data.get("conversation_id") or "").strip()
    if conversation_id:
        return conversation_id
    parent_id = data.get("parent_id") or data.get("parentId") or data.get("thread_id")
    if parent_id:
        return str(parent_id)
    return message_id


def _select_approval_redirect(current_key, candidate_keys, is_blocked) -> Optional[str]:
    """Pick the session key an out-of-thread approval command should resolve.

    Returns a redirect target only when it is *unambiguous*:

      * the command's own session (``current_key``) has no blocking approval
        (an in-thread /approve already works — never redirect those), and
      * exactly ONE remembered session is currently blocked on an approval.

    Fails closed on ambiguity (zero or 2+ blocked sessions → ``None``), so we
    never guess which of several pending approvals the user meant. ``is_blocked``
    is a predicate so this stays pure and unit-testable without the gateway's
    runtime approval state.
    """
    if is_blocked(current_key):
        return None
    blocked = [k for k in dict.fromkeys(candidate_keys) if k != current_key and is_blocked(k)]
    if len(blocked) == 1:
        return blocked[0]
    return None


class AxAdapter(BasePlatformAdapter):
    """aX adapter — SSE in, REST out, one agent identity per instance."""

    # aX has a first-class activity stream attached to the triggering message.
    # Keep Hermes chat output final-only and route tool/activity updates through
    # /agents/processing-status instead of transient message bubbles.
    SUPPORTS_MESSAGE_EDITING = False
    SUPPORTS_ACTIVITY_STATUS = True

    def __init__(self, config: PlatformConfig):
        extra: Dict[str, Any] = config.extra or {}

        base_url = (extra.get("base_url") or os.getenv("AX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        token = (config.token or os.getenv("AX_TOKEN") or "").strip()
        space_id = (extra.get("space_id") or os.getenv("AX_SPACE_ID") or "").strip()
        agent_name = (extra.get("agent_name") or os.getenv("AX_AGENT_NAME") or "").strip()
        agent_id = (extra.get("agent_id") or os.getenv("AX_AGENT_ID") or "").strip()
        local_gateway_url = (
            extra.get("local_gateway_url")
            or os.getenv("AX_LOCAL_GATEWAY_URL")
            or os.getenv("AX_GATEWAY_UI_URL")
            or DEFAULT_LOCAL_GATEWAY_URL
        )

        if not token:
            raise ValueError("aX adapter requires AX_TOKEN (agent PAT)")
        if not token.startswith("axp_a_"):
            raise ValueError("aX adapter requires AX_TOKEN to be an agent PAT (axp_a_...)")
        if not space_id:
            raise ValueError("aX adapter requires AX_SPACE_ID")
        if not agent_name:
            raise ValueError("aX adapter requires AX_AGENT_NAME")
        if not agent_id:
            raise ValueError(
                "aX adapter requires AX_AGENT_ID — needed for agent_access "
                "PAT exchange and /api/v1/agents/heartbeat (without it the "
                "UI online dot stays gray)"
            )

        super().__init__(config, Platform("ax"))

        self.base_url = base_url
        self.token = token
        self.space_id = space_id
        self.agent_name = agent_name
        self.agent_id = agent_id
        self.local_gateway_url = str(local_gateway_url or "").strip().rstrip("/")

        self._sse_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._jwt: Optional[str] = None
        self._jwt_expires_at: float = 0.0
        # Word-boundary mention pattern: rejects "@nova2" and "email@nova.com"
        # while accepting "@nova", "@nova.", "@nova!", " @nova\n", etc.
        self._mention_pattern = re.compile(
            rf"(?<!\w)@{re.escape(self.agent_name)}(?!\w)",
            re.IGNORECASE,
        )
        # See SEEN_MESSAGE_LRU_MAX for why we need this.
        self._seen_message_ids: "OrderedDict[str, None]" = OrderedDict()
        # Recently-dispatched run sessions: session_key -> thread root (chat_id).
        # An approval command that lands outside the blocked thread uses this to
        # find the session holding the pending approval (#72). Bounded LRU.
        self._recent_roots: "OrderedDict[str, str]" = OrderedDict()

    async def _announce_local_gateway(
        self,
        status: str,
        *,
        activity: Optional[str] = None,
        message_id: Optional[str] = None,
        current_tool: Optional[str] = None,
    ) -> None:
        """Best-effort local Gateway roster/activity update.

        The hosted aX heartbeat makes the web app show the agent online. This
        local announcement is separate: it tells `ax gateway start` that an
        externally managed Hermes plugin process is live, so the Gateway UI
        can show an active row without launching a duplicate runtime.
        """
        if not self.local_gateway_url:
            return
        body: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "space_id": self.space_id,
            "status": status,
            "runtime_kind": "hermes_plugin",
            "pid": os.getpid(),
            "workdir": os.getcwd(),
        }
        if activity:
            body["activity"] = activity
        if message_id:
            body["message_id"] = message_id
        if current_tool:
            body["current_tool"] = current_tool
        try:
            async with httpx.AsyncClient(timeout=LOCAL_GATEWAY_ANNOUNCE_TIMEOUT) as client:
                await client.post(
                    f"{self.local_gateway_url}/api/agents/{quote(self.agent_name)}/external-runtime-announce",
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
        except Exception:
            pass

    async def _post_processing_status(
        self,
        message_id: str,
        status: str,
        *,
        activity: Optional[str] = None,
    ) -> None:
        """Best-effort POST to aX's original-message activity stream."""
        try:
            jwt = await self._get_jwt()
        except Exception:
            await self._announce_local_gateway(status, activity=activity, message_id=message_id)
            return
        body = {
            "message_id": message_id,
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "space_id": self.space_id,
            "status": status,
        }
        if activity:
            body["activity"] = activity
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.base_url}/api/v1/agents/processing-status",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception:
            pass
        await self._announce_local_gateway(status, activity=activity, message_id=message_id)

    @property
    def name(self) -> str:
        return f"aX(@{self.agent_name})"

    # ------------------------------------------------------------------ auth

    async def _get_jwt(self, *, force: bool = False) -> str:
        """Return a cached or freshly-exchanged JWT.

        PAT never touches business endpoints — only ``/auth/exchange``
        per AUTH-SPEC-001 §13. The runtime adapter intentionally accepts
        agent PATs only so messages, heartbeats, and activity updates are
        authored by the bound aX agent identity.
        """
        if not force and self._jwt and time.time() < (self._jwt_expires_at - JWT_REFRESH_BUFFER_SECONDS):
            return self._jwt

        body: Dict[str, Any] = {
            "audience": DEFAULT_AUDIENCE,
            "requested_token_class": "agent_access",
            "scope": AGENT_RUNTIME_SCOPE,
            "agent_id": self.agent_id,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{self.base_url}/auth/exchange",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
        self._jwt = data["access_token"]
        self._jwt_expires_at = time.time() + int(data.get("expires_in", 600))
        return self._jwt

    # --------------------------------------------------------------- connect

    async def connect(self) -> bool:
        self._stop_event.clear()
        try:
            await self._get_jwt()
        except Exception as exc:
            logger.error("[%s] PAT→JWT exchange failed: %s", self.name, exc)
            self._set_fatal_error(
                "auth_failed",
                f"aX PAT exchange failed: {exc}",
                retryable=True,
            )
            return False

        self._sse_task = asyncio.create_task(self._sse_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._mark_connected()
        logger.info(
            "[%s] connected; space=%s base=%s",
            self.name,
            self.space_id[:8],
            self.base_url,
        )
        await self._announce_local_gateway("connected", activity="Hermes plugin listener connected")
        return True

    async def _heartbeat_loop(self) -> None:
        """Periodically POST /api/v1/agents/heartbeat so aX UI shows the agent online.

        Without this the agent record's last_seen_at never advances and the
        sidebar dot stays gray. Idempotent best-effort — exceptions never
        bubble out of the loop.
        """
        # Send one immediately at connect so the agent flips online without waiting a full interval.
        await self._send_heartbeat("connected")
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
                return  # stop_event triggered
            except asyncio.TimeoutError:
                pass
            await self._send_heartbeat("connected")

    async def _send_heartbeat(self, status: str) -> None:
        try:
            jwt = await self._get_jwt()
        except Exception:
            await self._announce_local_gateway(status)
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.base_url}/api/v1/agents/heartbeat",
                    json={"agent_id": self.agent_id, "status": status},
                    headers={
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception:
            pass  # heartbeat is best-effort
        await self._announce_local_gateway(status)

    async def disconnect(self) -> None:
        self._stop_event.set()
        # Mark offline before cancelling so the UI updates promptly.
        try:
            await self._send_heartbeat("offline")
            await self._announce_local_gateway("offline", activity="Hermes plugin listener stopped")
        except Exception:
            pass
        for task_attr in ("_sse_task", "_heartbeat_task"):
            task = getattr(self, task_attr, None)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            setattr(self, task_attr, None)
        self._mark_disconnected()
        logger.info("[%s] disconnected", self.name)

    # -------------------------------------------------------------- SSE loop

    async def _sse_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                jwt = await self._get_jwt()
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=10.0,
                        read=SSE_IDLE_TIMEOUT,
                        write=10.0,
                        pool=10.0,
                    ),
                ) as sse_client:
                    async with sse_client.stream(
                        "GET",
                        f"{self.base_url}/api/v1/sse/messages",
                        params={"token": jwt, "space_id": self.space_id},
                    ) as response:
                        if response.status_code != 200:
                            preview = (await response.aread()).decode("utf-8", errors="ignore")[:200]
                            raise ConnectionError(f"SSE status {response.status_code}: {preview}")
                        backoff = 1.0
                        logger.info(
                            "[%s] SSE connected to space %s",
                            self.name,
                            self.space_id[:8],
                        )
                        async for event_type, payload in self._iter_sse(response):
                            if self._stop_event.is_set():
                                break
                            await self._handle_sse_event(event_type, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[%s] SSE loop error (retry in %.1fs): %s",
                    self.name,
                    backoff,
                    exc,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0 + 0.5, SSE_RECONNECT_BACKOFF_MAX)

    @staticmethod
    async def _iter_sse(
        response: httpx.Response,
    ) -> AsyncIterator[Tuple[str, Any]]:
        """Parse SSE event stream → (event_type, parsed_payload) pairs."""
        event_type = "message"
        data_buf: list[str] = []
        async for raw_line in response.aiter_lines():
            line = raw_line.rstrip("\r")
            if line == "":
                if data_buf:
                    raw = "\n".join(data_buf)
                    try:
                        payload: Any = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = raw
                    yield event_type, payload
                event_type = "message"
                data_buf = []
                continue
            if line.startswith(":"):
                continue  # SSE comment
            if line.startswith("event:"):
                event_type = line[6:].strip() or "message"
            elif line.startswith("data:"):
                data_buf.append(line[5:].lstrip())

    async def _handle_sse_event(self, event_type: str, payload: Any) -> None:
        if event_type in {
            "bootstrap",
            "heartbeat",
            "ping",
            "connected",
            "identity_bootstrap",
        }:
            return
        if event_type not in {"message", "mention"}:
            return
        if not isinstance(payload, dict):
            return
        await self._dispatch_inbound(payload)

    # ----------------------------------------------------------- dispatch in

    def _is_self_authored(self, data: Dict[str, Any]) -> bool:
        sender = str(data.get("sender") or data.get("agent_name") or "").lower()
        sender_id = str(data.get("sender_id") or data.get("agent_id") or "")
        if sender and sender == self.agent_name.lower():
            return True
        if self.agent_id and sender_id and sender_id == self.agent_id:
            return True
        return False

    def _is_for_me(self, data: Dict[str, Any]) -> bool:
        mentions = data.get("mentions") or []
        if isinstance(mentions, list):
            for m in mentions:
                if isinstance(m, str) and m.lower() == self.agent_name.lower():
                    return True
                if isinstance(m, dict):
                    name = str(m.get("name") or m.get("agent_name") or "").lower()
                    if name == self.agent_name.lower():
                        return True
        text = str(data.get("content") or data.get("text") or "")
        return bool(self._mention_pattern.search(text))

    def _clean_agent_trigger_text(self, text: str) -> str:
        """Strip a leading addressed mention before Hermes command parsing.

        Hermes detects slash commands with ``text.startswith("/")``. aX users
        naturally address agents as ``@agent /command`` in shared spaces, so
        match Telegram's trigger-cleaning pattern and hand Hermes ``/command``.
        """
        if not text:
            return text
        cleaned = re.sub(
            rf"^\s*@{re.escape(self.agent_name)}(?!\w)[,:\-]*\s*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned or text

    def _seen_or_record(self, message_id: str) -> bool:
        """Return True if message_id was already dispatched recently.

        aX's SSE delivers each mention as both ``event: message`` and
        ``event: mention``; without this guard Hermes processes the inbound
        twice and the second run path posts a "⚡ Interrupting current task"
        busy-ack chat bubble even though the agent was idle.
        """
        if message_id in self._seen_message_ids:
            self._seen_message_ids.move_to_end(message_id)
            return True
        self._seen_message_ids[message_id] = None
        if len(self._seen_message_ids) > SEEN_MESSAGE_LRU_MAX:
            self._seen_message_ids.popitem(last=False)
        return False

    async def _dispatch_inbound(self, data: Dict[str, Any]) -> None:
        if self._is_self_authored(data):
            return
        if not self._is_for_me(data):
            return

        message_id = str(data.get("id") or data.get("message_id") or "").strip()
        if not message_id:
            return
        if self._seen_or_record(message_id):
            return

        text = self._clean_agent_trigger_text(str(data.get("content") or data.get("text") or "").strip())
        if not text:
            return

        sender_name = str(data.get("sender") or data.get("agent_name") or "user")
        sender_id = str(data.get("sender_id") or data.get("agent_id") or "")
        parent_id = data.get("parent_id")
        # Thread root = conversation_id (the aX thread root, if present) else
        # parent_id (a direct reply target) else the mention's own message_id.
        # Resolving to the conversation root keeps every turn — including a
        # reply to the agent's own approval prompt — on one session key.
        chat_id = _resolve_thread_root(data, message_id)

        # chat_type is always "thread": every aX message lives in a thread
        # (a top-level mention is the root of one). Letting it flip between
        # "channel" on turn 1 and "thread" on turn 2 would change the
        # build_session_key output mid-conversation and split a single thread
        # across two Hermes sessions, breaking continuity and the
        # active-session guard.
        source = self._make_source(chat_id, sender_id, sender_name, message_id)
        session_key = build_session_key(source)

        # Approval commands (/approve, /deny) resolve a *pending* dangerous
        # command parked under the BLOCKED session's key. In aX a bare
        # "@agent /approve" is the root of its own thread, so it lands on a
        # fresh session that has nothing pending → "No pending command to
        # approve" (#72). Redirect it to the blocked session when that target
        # is unambiguous; otherwise fall through unchanged.
        if _is_approval_command(text):
            redirect_root = self._approval_redirect_root(session_key)
            if redirect_root:
                logger.info(
                    "[ax] Routing approval command from session %s to blocked session root %s",
                    session_key,
                    redirect_root,
                )
                source = self._make_source(redirect_root, sender_id, sender_name, message_id)
        else:
            # Remember this run so a later out-of-thread approval can find it.
            self._remember_session(session_key, chat_id)

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=data,
            message_id=message_id,
            reply_to_message_id=str(parent_id) if parent_id else None,
        )

        # Dispatch through the base adapter so the level-1 active-session
        # guard (queue/interrupt) and inline command bypass (/stop, /new,
        # /approve, /deny) apply. handle_message itself returns quickly by
        # spawning its own background task, so the SSE loop is not blocked.
        await self.handle_message(event)

    def _make_source(
        self, chat_id: str, sender_id: str, sender_name: str, message_id: str
    ) -> SessionSource:
        """Build the SessionSource for an inbound aX message.

        ``chat_id`` and ``thread_id`` are both the thread root, so
        ``build_session_key`` keys the whole conversation on that root (aX
        threads are shared, so user_id is not part of the key — which is why
        an approval redirect only needs to swap the root, not the sender).
        """
        return SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            chat_name=f"@{self.agent_name} / {self.space_id[:8]}",
            chat_type="thread",
            user_id=sender_id or sender_name,
            user_name=sender_name,
            thread_id=chat_id,
            guild_id=self.space_id,
            message_id=message_id,
        )

    def _remember_session(self, session_key: str, root: str) -> None:
        """Record a dispatched run's session as a candidate for approval redirect."""
        self._recent_roots[session_key] = root
        self._recent_roots.move_to_end(session_key)
        while len(self._recent_roots) > MAX_REMEMBERED_SESSIONS:
            self._recent_roots.popitem(last=False)

    def _approval_redirect_root(self, current_key: str) -> Optional[str]:
        """Return the thread root an approval command should be routed to, or None.

        Looks across recently-dispatched run sessions for the one currently
        blocked on a dangerous-command approval. Returns None when the command
        already targets the blocked session, when the target is ambiguous, or
        when the gateway's approval state isn't importable (defensive — keeps
        the adapter usable outside a full Hermes runtime).
        """
        try:
            from tools.approval import has_blocking_approval
        except Exception:  # pragma: no cover - only in a full Hermes runtime
            return None
        target = _select_approval_redirect(
            current_key, list(self._recent_roots.keys()), has_blocking_approval
        )
        return self._recent_roots.get(target) if target else None

    # ----------------------------------------------------------- send (out)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            jwt = await self._get_jwt()
        except Exception as exc:
            return SendResult(success=False, error=f"auth: {exc}", retryable=True)

        body: Dict[str, Any] = {
            "content": content,
            "space_id": self.space_id,
        }
        chat_anchor = str(chat_id or "").strip()
        thread_anchor = str(reply_to).strip() if reply_to else ""
        if not thread_anchor and chat_anchor and chat_anchor != self.space_id:
            thread_anchor = chat_anchor
        if thread_anchor:
            body["parent_id"] = thread_anchor

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"{self.base_url}/api/v1/messages",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {jwt}",
                        "Content-Type": "application/json",
                        "X-Space-Id": self.space_id,
                    },
                )
        except Exception as exc:
            return SendResult(success=False, error=str(exc), retryable=True)

        if r.status_code in (200, 201):
            payload: Dict[str, Any] = {}
            if (r.headers.get("content-type") or "").startswith("application/json"):
                try:
                    payload = r.json()
                except Exception:
                    payload = {}
            return SendResult(
                success=True,
                message_id=payload.get("id") or payload.get("message_id"),
                raw_response=payload,
            )

        retryable = r.status_code in (429,) or 500 <= r.status_code < 600
        return SendResult(
            success=False,
            error=f"status {r.status_code}: {r.text[:200]}",
            retryable=retryable,
        )

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Best-effort original-message processing/activity update.

        Hermes uses ``send_typing`` both for generic keepalive status and, when
        ``SUPPORTS_ACTIVITY_STATUS`` is set, for tool-progress activity. aX
        renders these on the triggering message's activity stream instead of as
        separate chat bubbles.
        """
        metadata = metadata or {}
        status = str(metadata.get("status") or "thinking")
        activity = metadata.get("activity")
        await self._post_processing_status(
            chat_id,
            status,
            activity=str(activity) if activity else None,
        )

    async def stop_typing(self, chat_id: str) -> None:
        """Mark the aX processing lifecycle complete after final delivery."""
        await self._post_processing_status(chat_id, "completed")

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = "",
    ) -> SendResult:
        # MVP: send as text + URL. aX UI inline-renders image links.
        text = (caption + "\n\n" + image_url).strip()
        return await self.send(chat_id, text)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "name": f"@{self.agent_name} / {self.space_id[:8]}",
            "type": "thread",
            "chat_id": chat_id,
        }


# ---------------------------------------------------------- plugin contract


def check_requirements() -> bool:
    """Adapter-level dependency check. httpx is already a hermes-agent dep."""
    try:
        import httpx  # noqa: F401

        return True
    except ImportError:
        return False


def is_connected(config: Any = None) -> bool:
    """Coarse env-only check used by gateway status before adapter init.

    Hermes's registry-driven enable pass calls this as ``is_connected(config)``
    with a probe ``PlatformConfig`` (the contract Discord/Google Chat follow);
    the ``config`` argument is accepted but unused since aX identity is
    sourced from env vars, not YAML platform blocks.
    """
    return bool(
        os.getenv("AX_TOKEN") and os.getenv("AX_SPACE_ID") and os.getenv("AX_AGENT_NAME") and os.getenv("AX_AGENT_ID")
    )


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env so env-only setups show up in status.

    Also auto-defaults AX_HOME_CHANNEL to AX_SPACE_ID so the gateway's
    "no home channel" first-mention notice doesn't fire for env-only
    setups — the agent's bound space *is* the natural home channel.
    Operators who want a separate cron-delivery target can still set
    AX_HOME_CHANNEL explicitly.
    """
    token = os.getenv("AX_TOKEN")
    space = os.getenv("AX_SPACE_ID")
    agent = os.getenv("AX_AGENT_NAME")
    agent_id = os.getenv("AX_AGENT_ID")
    if not (token and space and agent and agent_id):
        return None
    os.environ.setdefault("AX_HOME_CHANNEL", space)
    extra: Dict[str, Any] = {
        "base_url": os.getenv("AX_BASE_URL", DEFAULT_BASE_URL),
        "space_id": space,
        "agent_name": agent,
        "agent_id": agent_id,
    }
    home_channel_id = os.getenv("AX_HOME_CHANNEL", space)
    return {
        "token": token,
        "extra": extra,
        "home_channel": {
            "chat_id": home_channel_id,
            "chat_name": f"aX/{home_channel_id[:8]}",
        },
    }


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
) -> Dict[str, Any]:
    """Out-of-process delivery for cron jobs running outside the gateway."""
    adapter = AxAdapter(pconfig)
    result = await adapter.send(chat_id, message)
    return {
        "success": result.success,
        "message_id": result.message_id,
        "error": result.error,
    }


def register(ctx: Any) -> None:
    """Plugin entry point — invoked by Hermes PluginManager on startup."""
    ctx.register_platform(
        name="ax",
        label="aX",
        adapter_factory=lambda cfg: AxAdapter(cfg),
        check_fn=check_requirements,
        is_connected=is_connected,
        required_env=["AX_TOKEN", "AX_SPACE_ID", "AX_AGENT_NAME", "AX_AGENT_ID"],
        install_hint="No extra packages needed (uses httpx bundled with hermes-agent)",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="AX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="AX_ALLOWED_USERS",
        allow_all_env="AX_ALLOW_ALL_USERS",
        emoji="◢",
        pii_safe=True,
        platform_hint=(
            "You are on aX, a multi-agent collaboration platform at https://paxai.app. "
            "Other agents in your space may @-mention you and expect a reply. "
            "Replies thread under the original mention automatically. "
            "Mention other agents with @<name> to delegate or ask for help. "
            "Keep responses concise — aX renders messages as chat. "
            "Markdown is supported."
        ),
    )
