"""ConvoMem configuration node.

A configuration-only node (no incoming/outgoing edges): its presence in a
workflow turns the integration on and carries the API key. Enabling the node
*is* the intent — "give this agent memory" — so recall (pre-call briefing) and
capture (post-call storage) both always run; there are no sub-toggles to split
them. The Pydantic model is the source of truth; the serialized ``NodeSpec`` is
derived from it via ``build_spec``.
"""

from __future__ import annotations

from pydantic import model_validator

from api.services.integrations.base import IntegrationNodeRegistration
from api.services.workflow.node_data import BaseNodeData
from api.services.workflow.node_specs._base import (
    GraphConstraints,
    NodeCategory,
    NodeExample,
    PropertyType,
)
from api.services.workflow.node_specs.model_spec import (
    build_spec,
    node_spec,
    spec_field,
)


@node_spec(
    name="convomem",
    display_name="ConvoMem",
    description="Give the agent long-term memory of each caller via ConvoMem",
    llm_hint=(
        "ConvoMem is a customer-memory layer. It enriches the call before it "
        "starts and stores the transcript after it ends. It does not participate "
        "in the conversation graph and should not be connected to other nodes."
    ),
    docs_url="https://docs.dograh.com/integrations/convomem",
    category=NodeCategory.integration,
    icon="Brain",
    examples=[
        NodeExample(
            name="convomem_memory",
            data={
                "name": "Caller Memory",
                "convomem_enabled": True,
                "convomem_api_key": "sk-org-xxxxxxxxxxxxxxxx",
            },
        )
    ],
    graph_constraints=GraphConstraints(
        min_incoming=0, max_incoming=0, min_outgoing=0, max_outgoing=0, max_instances=1
    ),
    property_order=(
        "name",
        "convomem_enabled",
        "convomem_api_key",
        "convomem_live_capture",
        "convomem_memory_budget_tokens",
    ),
    field_overrides={
        "name": {
            "spec_default": "ConvoMem",
            "description": "Short identifier for this ConvoMem configuration.",
        },
        "convomem_enabled": {
            "display_name": "Enabled",
            "description": "When false, Dograh skips ConvoMem entirely for this call.",
        },
        "convomem_api_key": {
            "display_name": "API Key",
            "description": (
                "Your ConvoMem org API key (starts with sk-org-). Scope it to a "
                "single agent so recall and capture land under the right agent."
            ),
            "required": True,
        },
        "convomem_live_capture": {
            "display_name": "Live capture",
            "description": (
                "Also stream the transcript to ConvoMem *during* the call, "
                "fire-and-forget, so memory is fresh mid-call. The end-of-call "
                "capture still runs as the source of truth. Off by default."
            ),
        },
        "convomem_memory_budget_tokens": {
            "display_name": "Memory budget (tokens)",
            "description": (
                "Optional cap on how much recalled memory is injected into the "
                "prompt, in tokens. Leave empty to use ConvoMem's default (up to "
                "800, or your agent's own setting). Lower it to keep the agent "
                "tighter on its node instructions."
            ),
        },
    },
)
class ConvoMemNodeData(BaseNodeData):
    convomem_enabled: bool = spec_field(
        default=True,
        ui_type=PropertyType.boolean,
        display_name="Enabled",
        description="When false, Dograh skips ConvoMem entirely for this call.",
    )
    convomem_api_key: str | None = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="API Key",
        description="Your ConvoMem org API key (starts with sk-org-).",
    )
    convomem_live_capture: bool = spec_field(
        default=False,
        ui_type=PropertyType.boolean,
        display_name="Live capture",
        description=(
            "Also stream the transcript during the call (fire-and-forget). The "
            "end-of-call capture still runs. Off by default."
        ),
    )
    convomem_memory_budget_tokens: int | None = spec_field(
        default=None,
        ui_type=PropertyType.number,
        display_name="Memory budget (tokens)",
        description=(
            "Optional token cap on the injected memory briefing. Empty = "
            "ConvoMem's default (up to 800, or your agent's own setting)."
        ),
    )

    @model_validator(mode="after")
    def _validate_enabled_config(self):
        if not self.convomem_enabled:
            return self

        if not self.convomem_api_key or not self.convomem_api_key.strip():
            raise ValueError(
                "ConvoMem node is enabled but missing required field: convomem_api_key"
            )
        return self


SPEC = build_spec(ConvoMemNodeData)


NODE = IntegrationNodeRegistration(
    type_name="convomem",
    data_model=ConvoMemNodeData,
    node_spec=SPEC,
    sensitive_fields=("convomem_api_key",),
)


def find_enabled_node(nodes) -> ConvoMemNodeData | None:
    """Return the first enabled ConvoMem node's data, or ``None``.

    ``nodes`` is any iterable of workflow-graph node objects exposing
    ``node_type`` and ``data`` (runtime path). Used by the runtime + capabilities
    factories, which run per workflow run.
    """
    for node in nodes:
        if getattr(node, "node_type", None) != "convomem":
            continue
        data = getattr(node, "data", None)
        if data is not None and getattr(data, "convomem_enabled", False):
            return data
    return None
