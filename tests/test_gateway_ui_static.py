from pathlib import Path

DEMO_HTML = Path(__file__).resolve().parents[1] / "ax_cli" / "static" / "demo.html"


def test_agent_row_type_helpers_present() -> None:
    """Public-role helpers must exist; the row falls back to them when no
    specific runtime template is matched."""
    source = DEMO_HTML.read_text()

    assert "function publicAgentTypeLabel(agent)" in source
    assert "function publicAgentTypeMeta(label)" in source
    assert 'normalized.includes("on-demand")' in source


def test_agent_row_type_prefers_specific_runtime_over_public_role() -> None:
    """Specific runtime templates (Ollama, Hermes, Claude Code, ...) win
    over the public role abstraction (Live Listener / Pass-through). This
    keeps the row label informative when multiple agents share a role —
    the user wants to see "HERMES" vs "CLAUDE CODE" on Live Listener
    agents, not just the abstract "Live Listener" for both.

    Regression guard: an earlier rev called publicAgentTypeMeta first and
    flattened all managed agents to the public-role label.
    """
    source = DEMO_HTML.read_text()

    template_pos = source.index('if (template === "ollama")')
    public_type_pos = source.index("const publicType = publicAgentTypeMeta")
    assert template_pos < public_type_pos


def test_agent_row_type_falls_back_to_intake_model_when_no_template_match() -> None:
    """Custom / unknown templates without a specific meta still resolve to
    a useful label via intake_model (live_listener / launch_on_send /
    polling_mailbox). The fallback runs after the template-specific block."""
    source = DEMO_HTML.read_text()

    template_pos = source.index('if (template === "ollama")')
    launch_on_send_pos = source.index('intake === "launch_on_send"')
    live_listener_pos = source.index('intake === "live_listener"')

    assert template_pos < launch_on_send_pos
    assert template_pos < live_listener_pos


def test_agent_row_type_carries_tooltip_combining_runtime_and_role() -> None:
    """Operator hovering the type icon should see both the specific
    runtime and the public role (e.g. "Hermes · Live Listener") so the
    abstraction stays discoverable without taking row space."""
    source = DEMO_HTML.read_text()

    assert "tooltip" in source
    assert "${resolved.label} · ${publicLabel}" in source


def test_friendly_status_blocked_agent_shows_error_tone() -> None:
    """A BLOCKED agent is desired=running but cannot function — operator needs
    to take action. Error tone (red) is correct; warning (yellow) understates
    the severity."""
    source = DEMO_HTML.read_text()
    assert 'confidence === "BLOCKED"' in source
    # The BLOCKED → error mapping must appear; warning would be wrong
    blocked_error_pos = source.index('tone: "error", detail: detail || "Gateway has blocked')
    assert blocked_error_pos > 0


def test_friendly_status_stopping_label_exists() -> None:
    """When desired=stopped but the agent is still connected, operators should
    see 'Stopping' (yellow) rather than 'Stopped' (gray) so they know the
    transition is in progress."""
    source = DEMO_HTML.read_text()
    assert '"Stopping"' in source
    assert '"Stopped"' in source


def test_friendly_status_not_running_label_for_attached_stale() -> None:
    """An attached session (claude_code_channel) that goes STALE should show
    'Not running' in red — the process is gone and messages cannot be delivered.
    This is distinct from generic STALE (yellow, heartbeat overdue) which may
    self-heal."""
    source = DEMO_HTML.read_text()
    assert '"Not running"' in source
    assert "attached session is not running" in source


def test_friendly_status_unknown_fallback_exists() -> None:
    """The fallback status for unrecognised states should be 'Unknown' with a
    warning tone, not 'Idle' with a muted tone — an agent in an unknown state
    when desired=running needs operator attention."""
    source = DEMO_HTML.read_text()
    assert '"Unknown"' in source
    assert 'tone: "warning"' in source


def test_friendly_status_hidden_and_archived_override_health_signals() -> None:
    """Hidden and archived lifecycle phases must be checked before desired_state
    and before any health signals.  An operator archives or hides an agent to
    explicitly remove it from active operation — a common reason is that the
    agent did not record shutdown correctly and still appears yellow or red.
    The operator's intent must override observed runtime state."""
    source = DEMO_HTML.read_text()

    lifecycle_pos = source.index('lifecyclePhase === "hidden"')
    stopped_pos = source.index('if (desired === "stopped")')
    assert lifecycle_pos < stopped_pos
    assert '"Archived"' in source
    assert '"Hidden"' in source


def test_friendly_status_surfaces_external_plugin_attach_state_before_stopped() -> None:
    """Externally managed Hermes agents need a fresh plugin heartbeat; a stale
    reply alone should not read as connected.  The desired=stopped check is
    intentionally evaluated first (single source of truth for stopped state),
    with external plugin handling only applied when the agent is desired=running."""
    source = DEMO_HTML.read_text()

    stopped_pos = source.index('if (desired === "stopped")')
    plugin_pos = source.index("Plugin not attached")
    assert stopped_pos < plugin_pos
    assert "external_runtime_managed" in source
    assert "fresh Gateway heartbeat" in source
