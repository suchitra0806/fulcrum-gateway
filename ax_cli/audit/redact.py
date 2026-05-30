"""Per-event secret and PII redactor for audit export.

The activity.jsonl log was designed to be operator-readable locally, so
fields like agent_id and runtime_instance_id are surfaced verbatim. For
ATO / STIG export to a SIEM that may be read by a wider operator pool,
mask keys whose *name* matches a known-sensitive shape.

**Scope and limits.** This is name-based redaction only: it masks
``token``, ``*_secret``, ``*_key``, ``Authorization``, and similar
credential-shaped keys, plus (opt-in) message body fields. It does
**not** scan free-text values for embedded secrets — e.g.
``connector_tool_failed`` records ``error=str(exc)``, and an exception
message can legitimately embed a bearer token or a ``user:pass@host``
URL. The key name ``error`` is not credential-shaped, so the value
passes through unredacted. Operators exporting from environments where
this matters should pair this with a SIEM-side scrubber, or wait for
the value-level scrubber follow-up (``axp_…`` / bearer-pattern
detection on free-text fields).

The redaction rule is deny-by-pattern, not allow-by-pattern: we only
mask keys whose name matches a known-sensitive shape. Unknown fields
pass through. That keeps custom event payloads usable for compliance
review without per-event allowlist maintenance.
"""

from __future__ import annotations

from typing import Any

_REDACTED = "<redacted>"

# Exact key names (case-insensitive) that always carry secret material.
# ``token_file`` is a credential-locator path — masked so SIEM forwards
# don't leak the on-disk bearer-token location.
_SECRET_KEYS_EXACT: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "session_token",
        "session_proof",
        "next_session_proof",
        "api_key",
        "secret",
        "password",
        "authorization",
        "x-api-key",
        "x-gateway-session",
        "token_file",
    }
)

# Key-name suffix patterns (case-insensitive) that carry secret material.
# Lets us catch ``managed_agent_token``, ``ax_user_token``, ``client_secret``,
# ``hmac_secret`` etc. without enumerating every variant.
_SECRET_KEY_SUFFIXES: tuple[str, ...] = (
    "_token",
    "_secret",
    "_key",
    "_password",
    "_credential",
)

# Keys whose VALUES may carry message-body content. These are configurable —
# some compliance reviews want full text, others want them masked. The
# ``redact_message_content`` flag in :func:`redact_record` controls this
# separately from the always-on secret redaction above.
_MESSAGE_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "content",
        "message",
        "reply_preview",
        "last_reply_preview",
        "body",
    }
)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEYS_EXACT:
        return True
    return any(lowered.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


def redact_record(
    record: dict[str, Any],
    *,
    redact_message_content: bool = False,
) -> dict[str, Any]:
    """Return a deep-copied record with secret and (optionally) PII fields masked.

    Always masks credential-shaped keys (``token``, ``*_secret``, ``*_key`` etc.).
    When ``redact_message_content`` is True, also masks user-authored message
    content fields (``content``, ``reply_preview``, ``body``) which can carry
    PII in operator workflows.
    """
    return _redact(record, redact_message_content=redact_message_content)


def _redact(value: Any, *, redact_message_content: bool) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if _is_secret_key(key):
                out[key] = _REDACTED
            elif redact_message_content and key.lower() in _MESSAGE_CONTENT_KEYS:
                out[key] = _REDACTED
            else:
                out[key] = _redact(v, redact_message_content=redact_message_content)
        return out
    if isinstance(value, list):
        return [_redact(item, redact_message_content=redact_message_content) for item in value]
    return value
