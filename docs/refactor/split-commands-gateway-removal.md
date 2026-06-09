# Gateway split (#28 Phase 1) — obsolete-test & dead-code summary

This document accompanies the `feat/split-commands-gateway` refactor, which split the
11,538-line `ax_cli/commands/gateway.py` monolith into a thin orchestrator plus 13 focused
`gateway_*` modules (issue #28 Phase 1).

The refactor **moves** code out of the monolith (it does not duplicate it) and deliberately
**drops the test-only backwards-compatibility shim** that a prior attempt (PR #43) used. As a
result, tests that reached into the old `ax_cli.commands.gateway` namespace (direct imports or
`monkeypatch.setattr(gateway_cmd, "...")` of moved helpers) no longer resolve those names there.

## Status: the skipped command tests have been ported per-module

The five monolith-coupled test files were **rewritten into per-module files and deleted**. Each
test now patches the module that actually resolves the helper at call time — the routing rule is:

> Patch the **command's owning module**. After the split a command resolves every helper it calls
> in its own module's namespace (defined locally, top-imported, or bottom-imported from a sibling),
> so `monkeypatch.setattr("ax_cli.commands.gateway_<owner>.<helper>", ...)` is effective.
> `ax_cli.gateway` (`gateway_core`) was not split, so those patches are unchanged.

### New per-module files (replacing the 5 deleted originals)

Shared fixtures/fakes live in **`tests/gateway_cmd_testlib.py`** (each helper's monkeypatches routed
to the module its callers exercise). The ported tests live in:

| File | Module under test |
|---|---|
| `tests/test_gateway_agents_cmds.py` | `gateway_agents` |
| `tests/test_gateway_auth_cmds.py` | `gateway_auth` |
| `tests/test_gateway_daemon_cmd_cmds.py` | `gateway_daemon_cmd` |
| `tests/test_gateway_diagnostics_cmds.py` | `gateway_diagnostics` (status/doctor/approvals) |
| `tests/test_gateway_lifecycle_cmds.py` | `gateway_lifecycle` |
| `tests/test_gateway_local_cmds.py` | `gateway_local` |
| `tests/test_gateway_messaging_cmds.py` | `gateway_messaging` (agents send/inbox) |
| `tests/test_gateway_runtime_cmd_cmds.py` | `gateway_runtime_cmd` |
| `tests/test_gateway_session_cmds.py` | `gateway_session` |
| `tests/test_gateway_spaces_cmds.py` | `gateway_spaces` |
| `tests/test_gateway_ui_cmds.py` | `gateway_ui` (handler GET/POST/PUT/DELETE, rendering) |
| `tests/test_gateway_core_cmds.py` | core/process-level (`ax_cli.gateway` only) |

**Deleted originals** (fully ported): `test_gateway_commands.py`, `test_gateway_commands_ext.py`,
`test_gateway_commands_ext2.py`, `test_offline_mode.py`, `test_gateway_offline_visibility.py`.
`test_gateway_ui_connectors.py` now imports the shared HTTP-handler helpers from the testlib.

### All command tests ported (no remaining gateway skips)

The two deep cross-module cases that initially needed restructuring were also ported by mocking the
client seam **in each module that resolves it** (rather than a single namespace):

- `test_local_session_send_hydrates_space_from_database` — DB hydration runs in
  `gateway_spaces._hydrate_entry_space_from_database` (patch `gateway_spaces._load_gateway_user_client`),
  the managed send in `gateway_session` (patch `gateway_session._load_managed_agent_client`).
- `test_gateway_ui_handler_supports_agent_mutations` — the UI handler delegates `/send`+`/test` to
  `gateway_messaging` and `/doctor` to `gateway_diagnostics`; the agent-client seams are mirrored on
  those modules in addition to `gateway_agents`.

All 643 previously-skipped command tests are restored. The full suite returns to the original
`main` baseline of **3597 passed, 7 skipped** (the 7 skips are pre-existing and unrelated to the
gateway split).

## Re-pointed files (kept, passing)

These were salvaged with a mechanical change only (import path, module alias, or patch-target
string) and continue to provide their original coverage:

| File | Change |
|---|---|
| `tests/test_gateway_runtime_install.py` | imports → `gateway_runtime_cmd`; patch `...gateway.subprocess`/`load_gateway_session` → `gateway_runtime_cmd`. |
| `tests/test_hermes_provider.py` | import `_validate_hermes_provider` → `gateway_runtime_cmd`. |
| `tests/test_connectors_cli.py` | import `connectors_app` → `gateway_app`. |
| `tests/test_gateway_host_header.py`, `tests/test_gateway_security_headers.py`, `tests/test_gateway_ui_connectors.py` | alias → `gateway_ui`. |
| `tests/test_audit_export.py` | patch `...gateway.activity_log_path` → `gateway_audit`. |
| `tests/test_agents_test_invoking_principal.py` | alias → `gateway_messaging`. |
| `tests/test_gateway_helpers.py` | per-symbol routing to `gateway_auth` / `gateway_session` / `gateway_spaces` / `gateway_agents` / `gateway_messaging` / `gateway_runtime_cmd`. |
| `tests/test_channel.py` | import → `gateway_agents`. |
| `tests/test_gateway_log_timestamps.py` | import/ref → `gateway_daemon_cmd`. |
| `tests/test_gateway_langgraph_composio_demo.py` | alias → `gateway_agents`. |
| `tests/test_demo_script_contracts.py` | alias → `gateway_local`. |

## Production callers repointed (not back-compat shimmed)

The orchestrator no longer re-exports moved helpers, so the three internal callers were updated to
import from the new module homes (these are the only non-test importers of the moved names):

- `ax_cli/commands/messages.py` → `_approval_required_guidance`, `_local_route_failure_guidance`
  from `gateway_local`; `_local_process_fingerprint` from `gateway_session`.
- `ax_cli/commands/tasks.py` → `_approval_required_guidance` from `gateway_local`.
- `ax_cli/commands/channel.py` → `_render_agent_persona_markdown`, `_write_marker_section` from
  `gateway_agents`.

`ax_cli/main.py` is unchanged: the orchestrator still exposes `gateway.app` and re-exports
`gateway.GatewaySessionRejectedError`.

## No identified dead production code

The split relocated code without removing behavior; no production code path became unreachable.
After the per-module rewrite, the only remaining "to do" is the 2 skipped tests above.
