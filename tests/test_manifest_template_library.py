"""Tests for the discoverable manifest template library (GH #259)."""

from __future__ import annotations

from pathlib import Path

from ax_cli import gateway_runtime_types
from ax_cli.manifest_template_library import (
    _BUNDLED_DIR,
    agent_template_catalog,
    agent_template_definition,
    agent_template_list,
    copy_template_manifest,
    list_template_ids,
    template_manifest_path,
)


def test_bundled_templates_ship_in_package():
    assert _BUNDLED_DIR.is_dir()
    meta_files = list(_BUNDLED_DIR.glob("*.meta.toml"))
    assert len(meta_files) >= 13
    for meta_path in meta_files:
        template_id = meta_path.name.removesuffix(".meta.toml")
        agent_path = _BUNDLED_DIR / f"{template_id}.agent.toml"
        assert agent_path.is_file(), f"missing agent manifest for {template_id}"


def test_catalog_matches_legacy_entrypoints():
    catalog = agent_template_catalog()
    assert "hermes" in catalog
    assert "ollama" in catalog
    assert "echo_test" in catalog
    assert catalog["hermes"]["runtime_type"] == "hermes_plugin"
    assert catalog["ollama"]["defaults"]["runtime_type"] == "exec"
    assert "exec_command" in catalog["ollama"]["defaults"]


def test_gateway_runtime_types_reexports_catalog():
    assert gateway_runtime_types.agent_template_catalog() == agent_template_catalog()


def test_template_definition_normalizes_echo_alias():
    assert agent_template_definition("echo")["id"] == "echo_test"


def test_list_order_and_advanced_filter():
    listed = agent_template_list()
    listed_ids = [item["id"] for item in listed]
    assert listed_ids[0] == "hermes"
    assert "inbox" not in listed_ids
    advanced = agent_template_list(include_advanced=True)
    assert any(item["id"] == "inbox" for item in advanced)


def test_langgraph_defaults_include_resolved_bridge_paths():
    template = agent_template_definition("langgraph")
    defaults = template["defaults"]
    repo_root = Path(__file__).resolve().parents[1]
    assert str(repo_root) in defaults["exec_command"]
    assert str(repo_root) in defaults["bridge_source"]
    assert defaults["bridge_source"].endswith("langgraph_bridge.py")


def test_copy_template_manifest_renames_agent():
    text = copy_template_manifest("hermes", suggested_name="my-hermes")
    assert 'name = "my-hermes"' in text
    assert 'template = "hermes"' in text


def test_template_manifest_path_resolves_bundled_file():
    path = template_manifest_path("hermes")
    assert path.name == "hermes.agent.toml"
    assert path.is_file()


def test_user_local_template_overrides_bundled(tmp_path, monkeypatch):
    user_dir = tmp_path / "templates"
    user_dir.mkdir()
    (user_dir / "echo_test.meta.toml").write_text(
        '\n'.join(
            [
                'id = "echo_test"',
                'label = "Custom Echo"',
                'description = "override"',
                'runtime_type = "echo"',
                'asset_class = "interactive_agent"',
                'intake_model = "live_listener"',
                'trigger_sources = ["direct_message"]',
                'return_paths = ["inline_reply"]',
                'telemetry_shape = "basic"',
                'suggested_name = "custom-echo"',
                'operator_summary = "custom"',
                'recommended_test_message = "ping"',
                'what_you_need = []',
                "",
                "[advanced]",
                'adapter_label = "custom"',
                "supports_command_override = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (user_dir / "echo_test.agent.toml").write_text('name = "custom-echo"\ntemplate = "echo_test"\n', encoding="utf-8")
    monkeypatch.setattr("ax_cli.manifest_template_library._USER_DIR", user_dir)
    template = agent_template_definition("echo_test")
    assert template["label"] == "Custom Echo"
    assert "echo_test" in list_template_ids(include_advanced=True)
