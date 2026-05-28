# LangGraph + Composio Gateway bridge

Gateway-managed exec bridge for the `langgraph_composio` agent template. It searches
third-party tools through a registered outbound connector (Composio provider) and
optionally executes one tool when the mention includes a `RUN:` directive.

## Prerequisites

1. Register a Composio connector:

```bash
ax gateway connectors add demo --provider composio --managed-auth
ax gateway connectors auth write demo COMPOSIO_API_KEY=<your-key>
```

2. Add a managed agent bound to that connector:

```bash
ax gateway agents add lgc-demo \
  --template langgraph_composio \
  --connector-ref demo \
  --no-start
ax gateway agents start lgc-demo
```

Gateway sets `AX_GATEWAY_CONNECTOR_REF` in the agent exec environment (never the
Composio API key).

## Per-mention behavior

1. **Search** — natural-language mention text is passed to `search_connector_tools`.
2. **Optional execute** — append `RUN:<TOOL_SLUG> {"arg": "value"}` to run one tool.
3. **Reply** — human-readable summary on stdout for Gateway to deliver inline.

## Local smoke test

```bash
export AX_GATEWAY_CONNECTOR_REF=demo
python3 examples/gateway_langgraph_composio/langgraph_composio_bridge.py \
  "list github pull requests for my org"
```

With execution:

```bash
python3 examples/gateway_langgraph_composio/langgraph_composio_bridge.py \
  'RUN:GITHUB_LIST_PRS {"owner":"my-org","repo":"my-repo"}'
```

## Optional LangGraph wrapper

If `langgraph` is installed, the bridge compiles a one-node `StateGraph` around the
same connector round-trip. Without LangGraph, the sequential path runs unchanged.

## Related docs

- [Composio integration guide](../../docs/composio-integration.md)
- Skill: `skills/gateway-composio-connectors/SKILL.md`
