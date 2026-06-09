# Vendored from ax-agents on 2026-04-25 — see ax_cli/runtimes/hermes/README.md
"""Hermes SDK runtime — wraps hermes-agent's AIAgent for aX sentinels.

Supports multiple backends:
  - codex_responses: GPT-5.4 via ChatGPT Codex endpoint (sentinel workers)
  - anthropic_messages: Claude via Anthropic API or Bedrock-compatible proxy
  - chat_completions: Any OpenAI-compatible endpoint (OpenRouter, local, etc.)

Security:
  - Credentials read from files with restricted permissions, never logged
  - AWS auth via IAM role / instance profile (no hardcoded keys)
  - Token sources validated before use
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.hermes_sdk")

# Hermes repo path — must be on sys.path for AIAgent import
HERMES_REPO = Path(
    os.environ.get(
        "HERMES_REPO_PATH",
        "/home/ax-agent/shared/repos/hermes-agent",
    )
)
# Hermes venv python — used to verify the environment
HERMES_VENV = HERMES_REPO / ".venv"

# Codex auth — multiple sources, resolved in priority order.
# ~/.hermes/auth.json is the canonical source (auto-refreshed by hermes-cli).
# ~/.codex/auth.json is the Codex CLI native store (same structure).
# ~/.ax/codex-token is a legacy plain-text fallback.
HERMES_AUTH_PATH = Path.home() / ".hermes" / "auth.json"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_SHARED_TOKEN_PATH = Path.home() / ".ax" / "codex-token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _read_token_file(path: Path) -> str:
    """Read a token file securely. Returns empty string on failure."""
    try:
        # NTFS uses ACLs, not POSIX mode bits — stat().st_mode always
        # reports 0o666/0o644 on Windows regardless of actual access, so
        # the world-readable check would fire on every read with no way
        # for the user to satisfy it. icacls is the Windows alternative.
        if sys.platform != "win32":
            stat = path.stat()
            if stat.st_mode & 0o077:
                log.warning(
                    "Token file %s has loose permissions (mode %o). Run: chmod 600 %s", path, stat.st_mode & 0o777, path
                )
        return path.read_text().strip()
    except OSError:
        return ""


def _extract_access_token_from_auth_json(path: Path) -> str:
    """Extract the access_token from a hermes/codex auth.json file.

    Both ~/.hermes/auth.json and ~/.codex/auth.json use the same token
    structure: tokens.access_token is the OAuth bearer token.

    Hermes format:
        {"providers": {"openai-codex": {"tokens": {"access_token": "..."}}}}
    Codex CLI format:
        {"tokens": {"access_token": "..."}}
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""

    # Hermes format: providers.<provider>.tokens.access_token
    providers = data.get("providers") or {}
    if providers:
        active = data.get("active_provider") or next(iter(providers.keys()), None)
        provider = providers.get(active) or {}
        tokens = provider.get("tokens") or {}
        token = tokens.get("access_token", "")
        if token:
            return token

    # Codex CLI format: tokens.access_token
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token", "")
    if token:
        return token

    # Last resort: top-level "token" key (legacy format)
    return data.get("token", "")


def _resolve_codex_token() -> str:
    """Resolve Codex OAuth token from available sources.

    Priority:
      1. CODEX_API_KEY env var (explicit override)
      2. ~/.hermes/auth.json (canonical, auto-refreshed)
      3. ~/.codex/auth.json (Codex CLI native)
      4. ~/.ax/codex-token (legacy plain-text fallback)

    Note: the legacy ~/.ax/codex-token file has historically contained an
    aX Platform PAT (axp_u_*) which is NOT a valid Codex bearer token.
    We explicitly reject tokens with that prefix to prevent regression.
    """
    # 1. Environment override
    env_token = os.environ.get("CODEX_API_KEY", "").strip()
    if env_token and not env_token.startswith("axp_"):
        return env_token

    # 2. Hermes auth store (canonical)
    token = _extract_access_token_from_auth_json(HERMES_AUTH_PATH)
    if token and not token.startswith("axp_"):
        return token

    # 3. Codex CLI auth store
    token = _extract_access_token_from_auth_json(CODEX_AUTH_PATH)
    if token and not token.startswith("axp_"):
        return token

    # 4. Legacy plain-text file (reject aX PATs)
    token = _read_token_file(CODEX_SHARED_TOKEN_PATH)
    if token and not token.startswith("axp_"):
        return token
    if token.startswith("axp_"):
        log.error(
            "Legacy %s contains an aX Platform PAT (axp_*), not a Codex token. "
            "Sentinels will fail authentication. Fix: copy the access_token from "
            "~/.hermes/auth.json, or run `hermes login`.",
            CODEX_SHARED_TOKEN_PATH,
        )

    return ""


def _read_credential_pool_entry(path: Path, provider: str) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    pool = data.get("credential_pool") or {}
    entries = pool.get(provider)
    if not entries:
        return None
    def _priority(e):
        try:
            return int(e.get("priority", 999))
        except (TypeError, ValueError):
            return 999

    best = min(entries, key=_priority)
    return {
        "api_key": best.get("access_token", ""),
        "base_url": best.get("base_url", ""),
    }


def _resolve_credential_from_auth_json(provider: str) -> dict:
    empty = {"api_key": "", "base_url": ""}
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        result = _read_credential_pool_entry(Path(hermes_home) / "auth.json", provider)
        if result:
            return result
    result = _read_credential_pool_entry(HERMES_AUTH_PATH, provider)
    if result:
        return result
    return empty


def _resolve_provider_config(model: str | None) -> dict:
    """Resolve provider, base_url, api_key, and api_mode from model string.

    Model format: "provider:model_name" or just "model_name"
    Examples:
      "codex:gpt-5.4"           → Codex Responses API
      "anthropic:claude-sonnet-4.6" → Anthropic Messages API
      "openrouter:anthropic/claude-sonnet-4.6" → OpenRouter chat completions
      "gpt-5.4"                 → auto-detect Codex
    """
    model = model or "gpt-5.4"

    if ":" in model and not model.startswith("us."):
        provider_hint, model_name = model.split(":", 1)
    else:
        provider_hint = None
        model_name = model

    if provider_hint == "codex" or (not provider_hint and "gpt" in model_name.lower()):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": CODEX_BASE_URL,
            "api_key": _resolve_codex_token(),
            "model": model_name,
        }
    elif provider_hint == "anthropic":
        env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        env_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        cred = _resolve_credential_from_auth_json("anthropic") if not env_key else {"api_key": "", "base_url": ""}
        return {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": env_base or cred.get("base_url") or "https://api.anthropic.com",
            "api_key": env_key or cred.get("api_key", ""),
            "model": model_name,
        }
    elif provider_hint == "bedrock":
        # Bedrock via Anthropic SDK (Claude models only).
        # Auth: IAM role (instance profile) — no API key needed.
        # Model names: claude-sonnet-4.6, claude-haiku-4.5, etc.
        # Region from AWS_REGION env var (default us-west-2).
        region = os.environ.get("AWS_REGION", "us-west-2")
        return {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": f"https://bedrock-runtime.{region}.amazonaws.com",
            "api_key": "bedrock-iam-role",  # placeholder; AnthropicBedrock uses IAM
            "model": model_name,
            "_bedrock": True,
            "_bedrock_region": region,
        }
    elif provider_hint == "openrouter":
        env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        env_base = os.environ.get("OPENROUTER_BASE_URL", "").strip()
        cred = _resolve_credential_from_auth_json("openrouter") if not env_key else {"api_key": "", "base_url": ""}
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": env_base or cred.get("base_url") or "https://openrouter.ai/api/v1",
            "api_key": env_key or cred.get("api_key", ""),
            "model": model_name,
        }
    else:
        # Default: auto-detect based on model name
        if "claude" in model_name.lower():
            env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            env_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
            cred = _resolve_credential_from_auth_json("anthropic") if not env_key else {"api_key": "", "base_url": ""}
            return {
                "provider": "anthropic",
                "api_mode": "anthropic_messages",
                "base_url": env_base or cred.get("base_url") or "https://api.anthropic.com",
                "api_key": env_key or cred.get("api_key", ""),
                "model": model_name,
            }
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": CODEX_BASE_URL,
            "api_key": _resolve_codex_token(),
            "model": model_name,
        }


def _ensure_hermes_importable():
    """Put hermes repo first on sys.path so its ``tools.registry`` wins.

    The gateway sets PYTHONPATH with the bundled sentinel shim *before* the
    hermes repo (the shim provides security wrappers).  That ordering means
    ``from tools.registry import ...`` would resolve to the shim — which has
    no ``registry`` module.  We force the hermes repo to position 0 here so
    Python finds the real ``tools`` package first, then evict any stale cache.
    """
    repo_str = str(HERMES_REPO)
    # Force to position 0 even if already present further back on sys.path.
    if repo_str in sys.path:
        sys.path.remove(repo_str)
    sys.path.insert(0, repo_str)
    hermes_tools = HERMES_REPO / "tools"
    if hermes_tools.is_dir() and "tools" in sys.modules:
        loaded = getattr(sys.modules["tools"], "__file__", "") or ""
        if not loaded.startswith(repo_str):
            del sys.modules["tools"]
            for key in [k for k in sys.modules if k.startswith("tools.")]:
                del sys.modules[key]


def _secure_hermes_tools(workdir: str):
    """Replace hermes's unrestricted tools with bounded versions.

    Wraps terminal, read_file, write_file, patch, and execute_code
    with path/command guards from agents/tools/__init__.py.
    Agents can only write to their own worktrees/workspace/tmp.
    Agents cannot read token files or credential directories.

    **Trust boundary.** These guards are the credential-exfiltration
    boundary for managed agents. ``BLOCKED_READ_PATTERNS`` keeps
    ``read_file`` and ``execute_code`` from touching token / credential
    paths, and the write-path guard keeps ``write_file`` and ``patch``
    inside the agent's workdir. Without this wrap, an agent could read
    ``~/.ax/user.toml`` or other secret-bearing files directly.

    **Degraded mode.** When the underlying ``tools`` package or any of
    its dependencies cannot be imported (typical: an IL2 deployment
    that ships the Hermes runtime without the tool registry), this
    function raises. The caller in :func:`HermesSDKRuntime.execute`
    routes that exception through :func:`_install_secure_tools` which
    surfaces the failure as a ``cb.on_status`` event and a stderr
    WARNING so operators see the sandbox loss instead of discovering
    it via behavior. The caller proceeds with unsandboxed tools today
    for backward compatibility with existing deployments; a strict
    fail-closed posture under an opt-in env var is a separate
    follow-up. Do not run a degraded runtime in a deployment where
    tool sandboxing is part of the trust boundary.
    """
    import json as _json

    from tools import (
        BLOCKED_READ_PATTERNS,
        _check_bash_command,
        _check_read_path,
        _check_write_path,
    )
    from tools.registry import registry

    log.info("hermes_sdk: securing tools for workdir=%s", workdir)

    def _wrap(tool_name, check_fn):
        """Wrap a registered tool handler with a security check."""
        if tool_name not in registry._tools:
            return
        original = registry._tools[tool_name].handler

        def secured(args, **kwargs):
            err = check_fn(args)
            if err:
                return _json.dumps({"error": err})
            return original(args, **kwargs)

        registry._tools[tool_name].handler = secured
        log.info("hermes_sdk: secured %s", tool_name)

    # Terminal / bash — block token exfil and destructive commands
    _wrap("terminal", lambda a: _check_bash_command(a.get("command", "")))

    # File reads — block token/secret paths
    _wrap("read_file", lambda a: _check_read_path(a.get("path", "")))
    _wrap("search_files", lambda a: _check_read_path(a.get("path", "")))

    # File writes — only allow agent workspace, worktrees, /tmp
    _wrap("write_file", lambda a: _check_write_path(a.get("path", ""), workdir))
    _wrap("patch", lambda a: _check_write_path(a.get("file_path", a.get("path", "")), workdir))

    # Code execution — block any code referencing secret paths
    def _check_code(args):
        code = args.get("code", "")
        for pat in BLOCKED_READ_PATTERNS:
            if pat in code:
                return f"Code blocked: references {pat}"
        return None

    _wrap("execute_code", _check_code)


def _install_secure_tools(workdir: str, cb: StreamCallback | None = None) -> bool:
    """Install Hermes tool security wraps and surface failures loudly.

    Calls :func:`_secure_hermes_tools` and returns True on success. When the
    wrap fails (typical: a deployment that ships the Hermes runtime without
    the tool registry), the failure is surfaced through three operator-facing
    channels so the security degradation is not silently absorbed (#151):

    - ``log.error`` at runtime-log level (was ``log.warning`` before #151).
    - ``cb.on_status(...)`` so the gateway and the SSE listener see a status
      event reflecting the security loss.
    - A ``WARNING`` line on stderr with ``flush=True`` for operators running
      the runtime directly without a callback consumer.

    Returns False if the wrap failed (caller proceeds with unsandboxed tools
    for backward compatibility today; this is the loud-but-functional posture,
    matching the langgraph bridge fix in PR #121). A strict fail-closed mode
    under an opt-in env var is a separate follow-up.
    """
    try:
        _secure_hermes_tools(workdir)
        return True
    except Exception as exc:
        warning = (
            "hermes_sdk: tool security setup failed: "
            f"{exc!r}. Tool calls (terminal, read_file, write_file, patch, "
            "execute_code) will run unsandboxed. Without these guards an "
            "agent can read credential-bearing files outside its workdir."
        )
        log.error(warning)
        if cb is not None:
            try:
                cb.on_status(f"security_wrapper_degraded: {exc!r}")
            except Exception:
                pass
        print(f"WARNING: {warning}", file=sys.stderr, flush=True)
        return False


def _register_connector_tools(workdir: str) -> None:
    """Register gateway connector tools into the hermes tool registry."""
    from tools.registry import registry

    _SCHEMAS = {
        "connector_search": {
            "name": "connector_search",
            "description": (
                "Search for available tools on a gateway connector "
                "(e.g. Gmail, Slack, GitHub). Describe what you want in plain English."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "Connector reference name"},
                    "query": {
                        "type": "string",
                        "description": "Natural-language description of the tool you need (e.g. 'send email')",
                    },
                    "app": {
                        "type": "string",
                        "description": "Filter results to a specific app (e.g. 'gmail', 'slack')",
                    },
                    "limit": {"type": "integer", "description": "Max results to return", "default": 5},
                },
                "required": ["connector", "query"],
            },
        },
        "connector_call": {
            "name": "connector_call",
            "description": "Execute a tool on a gateway connector. Use connector_search first to find the tool slug.",
            "parameters": {
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "Connector reference name"},
                    "tool": {
                        "type": "string",
                        "description": "Tool slug from connector_search (e.g. 'GMAIL_SEND_EMAIL')",
                    },
                    "args": {"type": "object", "description": "Tool-specific arguments as key-value pairs"},
                },
                "required": ["connector", "tool"],
            },
        },
        "connector_apps": {
            "name": "connector_apps",
            "description": "List connected apps on a gateway connector. Shows which services (Gmail, Slack, etc.) are available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "connector": {"type": "string", "description": "Connector reference name"},
                },
                "required": ["connector"],
            },
        },
    }

    # The sentinel overrides AX_CONFIG_DIR to the agent workspace dir, but
    # connector state lives under the gateway's global config root.
    # AX_GATEWAY_DIR survives the sentinel override and takes priority in
    # gateway_dir(); derive the config root from it when available.
    _gw_dir_env = os.environ.get("AX_GATEWAY_DIR", "").strip()
    if _gw_dir_env:
        _global_ax_dir = str(Path(_gw_dir_env).expanduser().parent)
    else:
        _global_ax_dir = str(Path.home() / ".ax")

    def _do_connector_call(tool_name, args):
        from ax_cli.connectors import ConnectorNotFoundError, find_connector, read_auth

        ref = args.get("connector", "")
        try:
            row = find_connector(ref)
        except ConnectorNotFoundError:
            return json.dumps({"error": f"Connector not found: {ref}"})
        if not row.enabled:
            return json.dumps({"error": f"Connector {ref!r} is disabled"})
        try:
            auth_env = read_auth(row.id, row.name) if row.auth_ref else {}
        except Exception as e:
            return json.dumps({"error": f"Auth error: {e}"})

        if tool_name == "connector_apps":
            from ax_cli.connectors import list_apps

            try:
                items = list_apps(row, auth_env)
            except Exception as e:
                return json.dumps({"error": f"Error listing apps: {e}"})
            if not items:
                return "No connected apps found"

            def _app_name(a: dict) -> str:
                tk = a.get("toolkit")
                if isinstance(tk, dict):
                    return tk.get("slug", "?")
                return a.get("appName") or "?"

            return "\n".join(f"{_app_name(a)}  status={a.get('status', '?')}" for a in items)

        if tool_name == "connector_search":
            from ax_cli.connectors import search_tools

            query = args.get("query", "")
            app = args.get("app")
            limit = args.get("limit", 5)
            try:
                result = search_tools(row, query, auth_env, apps=app, limit=limit)
            except Exception as e:
                return json.dumps({"error": f"Search error: {e}"})
            items = result.get("items", [])
            if not items:
                return f"No tools found for: {query}"
            lines = []
            for item in items:
                slug = item.get("enum", item.get("name", "?"))
                display = item.get("displayName") or item.get("display_name") or ""
                app_id = item.get("appId", "")
                lines.append(f"{slug}  app={app_id}\n  {display}")
            return "\n".join(lines)

        if tool_name == "connector_call":
            from ax_cli.connectors import execute_tool as connector_execute

            tool_slug = args.get("tool", "")
            tool_args = args.get("args", {})
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except json.JSONDecodeError:
                    return json.dumps({"error": f"Invalid JSON in args: {tool_args}"})
            try:
                result = connector_execute(row, tool_slug, tool_args, auth_env)
            except Exception as e:
                return json.dumps({"error": f"Connector error: {e}"})
            output = json.dumps(result, indent=2, default=str)
            if len(output) > 20000:
                output = output[:20000] + "\n...(truncated)..."
            return output

        return json.dumps({"error": f"Unknown connector tool: {tool_name}"})

    def _make_handler(tool_name):
        def handler(args, **kwargs):
            saved = os.environ.get("AX_CONFIG_DIR")
            os.environ["AX_CONFIG_DIR"] = _global_ax_dir
            try:
                return _do_connector_call(tool_name, args)
            finally:
                if saved is not None:
                    os.environ["AX_CONFIG_DIR"] = saved
                else:
                    os.environ.pop("AX_CONFIG_DIR", None)

        return handler

    registered = 0
    for name, schema in _SCHEMAS.items():
        if registry.get_entry(name) is not None:
            continue
        registry.register(
            name=name,
            toolset="connectors",
            schema=schema,
            handler=_make_handler(name),
            description=schema["description"],
        )
        registered += 1

    if registered:
        log.info("hermes_sdk: registered %d connector tools", registered)


@register("hermes_sdk")
class HermesSDKRuntime(BaseRuntime):
    """Runs hermes-agent's AIAgent with full agentic loop.

    Advantages over the basic openai_sdk runtime:
      - 90-turn agentic loop (vs 25)
      - Parallel tool execution for independent tools
      - Context compression for long sessions
      - Subagent delegation for large tasks
      - Rich callback pipeline for SSE signals
      - Codex auth refresh built-in
    """

    def execute(
        self,
        message: str,
        *,
        workdir: str,
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        stream_cb: StreamCallback | None = None,
        timeout: int = 300,
        extra_args: dict | None = None,
    ) -> RuntimeResult:
        _ensure_hermes_importable()
        from run_agent import AIAgent

        cb = stream_cb or StreamCallback()
        extra = extra_args or {}
        start_time = time.time()
        tool_count = 0
        files_written: list[str] = []

        # Resolve provider config from model string
        provider_cfg = _resolve_provider_config(model)
        if not provider_cfg["api_key"]:
            log.error("hermes_sdk: no API key for provider=%s", provider_cfg["provider"])
            return RuntimeResult(
                text="Agent could not authenticate — no API key available.",
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        # Mask key in logs
        key_preview = provider_cfg["api_key"][:8] + "..." if len(provider_cfg["api_key"]) > 8 else "***"
        log.info(
            "hermes_sdk: provider=%s model=%s key=%s", provider_cfg["provider"], provider_cfg["model"], key_preview
        )

        # ── Callbacks bridge: hermes → aX SSE signals ──
        # Hermes passes (name, preview, args_dict) — accept all three
        def _on_tool_progress(tool_name: str, args_preview: str, args_dict=None):
            nonlocal tool_count
            tool_count += 1
            cb.on_tool_start(tool_name, args_preview)

        def _on_status(status_msg: str):
            if not status_msg:
                return
            lower = status_msg.lower()
            if "think" in lower or "reason" in lower:
                cb.on_status("thinking")
            elif "tool" in lower:
                cb.on_status("tool_call")
            else:
                cb.on_status("processing")

        def _on_step(iteration: int, prev_tools: list[str]):
            cb.on_status("processing")

        def _on_stream_delta(text: str):
            cb.on_text_delta(text)

        # ── Agent profile ──
        # Profiles define tools, access, budget per agent type.
        agent_name = os.environ.get("AX_AGENT_NAME", "")
        try:
            from profiles import get_disabled_toolsets, get_enabled_toolsets, get_max_iterations, get_profile_for_agent

            profile = get_profile_for_agent(agent_name)
            disabled = extra.get("disabled_toolsets", get_disabled_toolsets(profile))
            enabled = extra.get("enabled_toolsets", get_enabled_toolsets(profile))
            max_iters = int(os.environ.get("HERMES_MAX_ITERATIONS", str(get_max_iterations(profile))))
            log.info("hermes_sdk: profile=%s agent=%s iters=%d", profile.get("name", "?"), agent_name, max_iters)
        except Exception as e:
            log.warning("hermes_sdk: profile load failed (%s), using defaults", e)
            enabled = extra.get("enabled_toolsets")
            disabled = extra.get(
                "disabled_toolsets",
                [
                    "web",
                    "browser",
                    "image_generation",
                    "tts",
                    "vision",
                    "cronjob",
                    "rl_training",
                    "homeassistant",
                ],
            )
            max_iters = int(os.environ.get("HERMES_MAX_ITERATIONS", "60"))

        # ── Register connector tools into hermes registry ──
        # Must happen BEFORE AIAgent() which snapshots tool definitions.
        try:
            _register_connector_tools(workdir)
        except Exception as e:
            log.warning("hermes_sdk: connector tool registration failed: %s", e)

        # ── Build the agent ──
        try:
            agent = AIAgent(
                base_url=provider_cfg["base_url"],
                api_key=provider_cfg["api_key"],
                provider=provider_cfg["provider"],
                api_mode=provider_cfg["api_mode"],
                model=provider_cfg["model"],
                max_iterations=max_iters,
                tool_delay=0.5,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=False,
                disabled_toolsets=disabled,
                enabled_toolsets=enabled,
                tool_progress_callback=_on_tool_progress,
                status_callback=_on_status,
                step_callback=_on_step,
                stream_delta_callback=_on_stream_delta,
            )
        except Exception as e:
            log.error("hermes_sdk: failed to create agent: %s", e)
            return RuntimeResult(
                text=f"Agent initialization failed: {e}",
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        # ── Secure tools: wrap hermes tools with path/command guards ──
        # Agents can only read shared repos + own dir, write to worktrees only.
        # Failures route through _install_secure_tools so the degradation is
        # surfaced via cb.on_status + stderr WARNING rather than absorbed into
        # a single log.warning line (#151). Loud-but-functional posture; the
        # runtime proceeds with unsandboxed tools today for backward compat.
        _install_secure_tools(workdir, cb=cb)

        # ── Build conversation history ──
        history = list(extra.get("history", []))

        # ── Execute ──
        cb.on_status("accepted")
        try:
            result = agent.run_conversation(
                user_message=message,
                system_message=system_prompt,
                conversation_history=history if history else None,
            )
        except KeyboardInterrupt:
            return RuntimeResult(
                text="Agent interrupted.",
                history=history,
                tool_count=tool_count,
                exit_reason="timeout",
                elapsed_seconds=int(time.time() - start_time),
            )
        except Exception as e:
            log.error("hermes_sdk: agent crashed: %s", e, exc_info=True)
            error_str = str(e).lower()
            if "429" in error_str or "rate" in error_str:
                return RuntimeResult(
                    text="",
                    history=history,
                    tool_count=tool_count,
                    exit_reason="rate_limited",
                    elapsed_seconds=int(time.time() - start_time),
                )
            return RuntimeResult(
                text=f"Agent error: {e}",
                history=history,
                tool_count=tool_count,
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        # ── Extract results ──
        final_text = result.get("final_response", "")
        output_history = result.get("messages", history)
        api_calls = result.get("api_calls", 0)
        total_tokens = result.get("total_tokens", 0)

        if final_text:
            cb.on_text_complete(final_text)

        elapsed = int(time.time() - start_time)
        api_calls = result.get("api_calls", 0)
        exit_reason = "done"
        if result.get("interrupted"):
            exit_reason = "timeout"
        elif not result.get("completed", True):
            # Distinguish iteration limit from actual crash
            if api_calls >= max_iters:
                exit_reason = "iteration_limit"
            else:
                exit_reason = "crashed"

        log.info(
            "hermes_sdk: %s in %ds, %d tools, %d api_calls, %d tokens, %d chars",
            exit_reason,
            elapsed,
            tool_count,
            api_calls,
            total_tokens,
            len(final_text),
        )

        return RuntimeResult(
            text=final_text,
            session_id=None,
            history=output_history,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason=exit_reason,
            elapsed_seconds=elapsed,
        )
