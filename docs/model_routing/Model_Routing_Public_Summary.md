# Model resources and routing

`ModelRouter` selects a model resource for one request. `ProviderRouter` retains cross-provider failover; credentials remain owned by `ProviderManager` and the existing credential resolver. Selection changes apply to the next request and do not replace conversation, Memory or learning owners.

## Profiles

| Profile | Resource | Declared capabilities | Recorded validation scope |
| --- | --- | --- | --- |
| `tju-stable` | `tju-llm` | text, JSON, tools, vision | text |
| `tju-max` | `tju-llm-max`, variant `dsv4.1flash` | text, JSON, tools | text |

Capability declarations and recorded validation are separate from current availability. The shipped default is stable. The policies `tju-stable`, `tju-max` and `auto` use the existing settings owner. Max falls back to stable within a bounded request budget; it does not receive vision requests. Other provider protocols preserve their adapters through metadata-only passthrough.

Routing events allowlist request IDs, tasks, profile/model/provider names, fallback reasons and status codes. They exclude prompts, headers, credentials, endpoints and raw exception text. Protocol passthrough has no default disk event write.

## Offline validation

The public tests use synthetic credentials, injected transports, temporary user state and offscreen Qt widgets. `tests/test_model_routing_layer.py` and `tests/test_model_management.py` cover selection, provider/protocol binding, capabilities, fallback, deadline allocation, event redaction and continuity across a policy change. These tests do not claim live-provider or visible-desktop acceptance. Original local live evidence is intentionally excluded from this branch.

## Optional external teach-mcp

Firefly does not bundle or modify the external TutorLoop. Configure `FIREFLY_TEACH_MCP_SERVER` with the installed `server.py` path, or pass it explicitly to `tools/teach_mcp_provider_launcher.py`. The portable default is `<Firefly user data>/plugins/teach-mcp/server.py`. Missing installation is reported as unavailable; lookup performs no installation or tool call.
