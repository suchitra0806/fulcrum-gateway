"""Per-agent delivery queues for offline single-agent smoke testing.

Each subscribed agent gets exactly one queue. Messages posted to
/api/v1/messages are delivered only to the specifically mentioned agent —
no fan-out, no agent-to-agent routing.

Used by the gateway HTTP server (runs in the UI process).
"""

from __future__ import annotations

import json
import queue
import re
import threading

_OFFLINE_SPACE_ID = "00000000-0000-0000-0000-000000000001"
_TOKEN_PREFIX = "offline-"


class OfflineAgentQueues:
    """One queue per subscribed agent. Replaced on reconnect."""

    _instance: OfflineAgentQueues | None = None
    _class_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue] = {}

    @classmethod
    def get(cls) -> OfflineAgentQueues:
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def subscribe(self, agent_name: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[agent_name.lower()] = q
        return q

    def unsubscribe(self, agent_name: str) -> None:
        with self._lock:
            self._queues.pop(agent_name.lower(), None)

    def deliver(self, agent_name: str, message: dict) -> bool:
        """Deliver to one specific agent. Returns True if they were subscribed."""
        with self._lock:
            q = self._queues.get(agent_name.lower())
        if q is None:
            return False
        q.put(message)
        return True

    def is_subscribed(self, agent_name: str) -> bool:
        with self._lock:
            return agent_name.lower() in self._queues


def make_token(agent_name: str) -> str:
    return f"{_TOKEN_PREFIX}{agent_name}"


def agent_name_from_token(token: str) -> str | None:
    if not token.startswith(_TOKEN_PREFIX):
        return None
    name = token[len(_TOKEN_PREFIX):]
    return name if name else None


def extract_mentions(content: str) -> list[str]:
    return [m.lstrip("@") for m in re.findall(r"@[\w-]+", content)]


def sse_frame(event_type: str, data: dict) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
