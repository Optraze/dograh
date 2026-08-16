"""ConvoMem integration package.

Gives a Dograh agent long-term memory of each caller. Self-registers on import
via ``register_package``; auto-discovered by ``api/services/integrations/loader.py``.

Three parts:
- ``create_call_capabilities`` — pre-call recall: look the caller up and brief
  the agent before the first turn (``capabilities.py``).
- ``create_runtime_sessions`` — seal the final transcript at call-finish
  (``runtime.py``).
- ``run_completion`` — flush that transcript to ConvoMem after the call, closing
  the conversation as completed or escalated (``completion.py``).
"""

from __future__ import annotations

from api.services.integrations.base import IntegrationPackageSpec
from api.services.integrations.registry import register_package

from .capabilities import create_call_capabilities
from .completion import run_completion
from .node import NODE
from .runtime import create_runtime_sessions

PACKAGE = register_package(
    IntegrationPackageSpec(
        name="convomem",
        nodes=(NODE,),
        create_call_capabilities=create_call_capabilities,
        create_runtime_sessions=create_runtime_sessions,
        run_completion=run_completion,
    )
)

__all__ = ["PACKAGE"]
