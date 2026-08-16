"""ConvoMem post-call completion handler.

Reads the ``convomem_snapshot`` the runtime session left in ``workflow_run.logs``
and flushes it to ConvoMem with a single ``POST /capture``. This is the one and
only outbound call of the integration's collect-then-deliver flow, and it runs in
the post-call task, off the live pipeline.

The same capture both stores the transcript and closes the conversation:
``endConversation`` for a normal hang-up, ``escalate`` when the call was
transferred to a human. ConvoMem's worker persists the transcript *before* it
closes the conversation, so there is no capture-vs-close race to manage here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from api.services.integrations.base import IntegrationCompletionContext

from .client import ConvoMemConfig, capture
from .node import ConvoMemNodeData
from .runtime import CHANNEL_VOICE, SNAPSHOT_KEY


def _idempotency_key(workflow_run_id: int) -> str:
    # One capture per run: a retried post-call task must not double-store the
    # transcript. ConvoMem dedups on this key.
    return f"dograh-run-{workflow_run_id}"


def _build_payload(
    snapshot: dict[str, Any],
    *,
    workflow_run_id: int,
) -> dict[str, Any] | None:
    """Assemble the ``/capture`` body, or ``None`` if it cannot be identified/sent."""
    messages = snapshot.get("messages") or []
    if not messages:
        return None

    phone = snapshot.get("phone")
    if not isinstance(phone, str) or not phone.strip():
        # ConvoMem needs an identity; phone is the only one a voice call carries.
        return None

    payload: dict[str, Any] = {
        "messages": messages,
        "phoneNumber": phone.strip(),
        "channel": snapshot.get("channel") or CHANNEL_VOICE,
        "idempotencyKey": _idempotency_key(workflow_run_id),
    }

    if snapshot.get("escalate"):
        payload["escalate"] = True
        disposition = snapshot.get("disposition")
        if isinstance(disposition, str) and disposition.strip():
            payload["reason"] = disposition.strip()[:500]
    else:
        payload["endConversation"] = True

    return payload


async def run_completion(
    nodes: list[dict[str, Any]],
    context: IntegrationCompletionContext,
) -> dict[str, Any]:
    """Post-call: flush the captured transcript to ConvoMem."""
    results: dict[str, Any] = {}

    raw_snapshot: dict[str, Any] | None = (context.workflow_run.logs or {}).get(
        SNAPSHOT_KEY
    )

    for node in nodes:
        node_id = node.get("id", "unknown")

        try:
            node_data = ConvoMemNodeData.model_validate(node.get("data", {}))
        except Exception:
            results[f"convomem_{node_id}"] = {"error": "validation_failed"}
            continue

        if not node_data.convomem_enabled:
            continue

        if not raw_snapshot:
            # No transcript was sealed (empty call, or capture wired off at runtime).
            results[f"convomem_{node_id}"] = {"error": "missing_runtime_snapshot"}
            continue

        payload = _build_payload(raw_snapshot, workflow_run_id=context.workflow_run_id)
        if payload is None:
            results[f"convomem_{node_id}"] = {"error": "nothing_to_capture"}
            continue

        try:
            config = ConvoMemConfig(api_key=node_data.convomem_api_key or "")
        except Exception as exc:
            results[f"convomem_{node_id}"] = {"error": f"invalid_config: {exc}"}
            continue

        delivery = await capture(config, payload)
        results[f"convomem_{node_id}"] = {
            **delivery,
            "escalated": bool(payload.get("escalate")),
            "message_count": len(payload["messages"]),
            "captured_at": datetime.now(UTC).isoformat(),
        }

    return results
