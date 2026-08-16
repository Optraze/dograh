"""GENERATED — do not edit by hand.

Regenerate with `python -m dograh_sdk.codegen` against the target
Dograh backend. Source of truth: the backend's model-backed node-spec
catalog served from `/api/v1/node-types`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Optional

from dograh_sdk.typed._base import TypedNode


@dataclass(kw_only=True)
class Convomem(TypedNode):
    """
    Give the agent long-term memory of each caller via ConvoMem  LLM hint:
    ConvoMem is a customer-memory layer. It enriches the call before it
    starts and stores the transcript after it ends. It does not participate
    in the conversation graph and should not be connected to other nodes.
    """

    type: ClassVar[str] = 'convomem'

    convomem_api_key: str
    """
    Your ConvoMem org API key (starts with sk-org-). Scope it to a single
    agent so recall and capture land under the right agent.
    """

    name: str = 'ConvoMem'
    """
    Short identifier for this ConvoMem configuration.
    """

    convomem_enabled: bool = True
    """
    When false, Dograh skips ConvoMem entirely for this call.
    """

    convomem_live_capture: bool = False
    """
    Also stream the transcript to ConvoMem *during* the call, fire-and-
    forget, so memory is fresh mid-call. The end-of-call capture still runs
    as the source of truth. Off by default.
    """

    convomem_memory_budget_tokens: Optional[float] = None
    """
    Optional cap on how much recalled memory is injected into the prompt, in
    tokens. Leave empty to use ConvoMem's default (up to 800, or your
    agent's own setting). Lower it to keep the agent tighter on its node
    instructions.
    """

