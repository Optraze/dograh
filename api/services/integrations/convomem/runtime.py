"""ConvoMem runtime session.

Dograh's integration contract is "collect live, deliver after the call" — no
outbound I/O on the live path. This session does the *collect* half: at
call-finish it reads the final transcript out of the live LLM context and seals
a compact, JSON-serializable snapshot. The generic framework persists it into
``workflow_run.logs`` under ``"convomem_snapshot"``; ``completion`` reads it back
and does the actual POST to ConvoMem.

**Live capture (opt-in).** When the node turns on
``convomem_live_capture``, the session *also* streams transcript deltas to
ConvoMem *during* the call, fire-and-forget, on an independent asyncio task that
never blocks the pipeline. This is outbound I/O on the live path, which Dograh's
contract discourages ("unless there is a very strong reason") — so it is off by
default and additive: the end-of-call flush still runs as the source of truth and
the close-out (endConversation/escalate). See the package AGENTS notes.

Note: the live-flush self-heal and the end-of-call carrier below both re-send
turns that ConvoMem may already have, and rely on ConvoMem dropping exact-content
duplicates within a conversation. If that server-side dedup ever goes away,
re-sent turns would double-store and we'd stop overlapping the sends here instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from api.services.integrations.base import (
    IntegrationRuntimeContext,
    IntegrationRuntimeSession,
)

from .capabilities import resolve_customer_phone
from .client import ConvoMemConfig, capture
from .node import find_enabled_node

SNAPSHOT_KEY = "convomem_snapshot"

CHANNEL_VOICE = "VOICE"

# Live-capture cadence. Conservative on purpose: this is live-path I/O, so flush
# rarely and cap the total per call.
LIVE_CAPTURE_INTERVAL_SECONDS = 20.0
LIVE_CAPTURE_MAX_FLUSHES = 8

# A finished call counts as an escalation (transfer to a human) when the engine
# ended it for a transfer, or an external-PBX transfer succeeded. ConvoMem uses
# this only to label the conversation ESCALATED vs COMPLETED; memory extraction
# is identical either way, so a mislabel is harmless.
_TRANSFER_DISPOSITIONS = {"transfer_call", "call_transferred"}


def _was_escalated(gathered_context: dict[str, Any]) -> bool:
    if gathered_context.get("external_pbx_transferred"):
        return True

    disposition = (gathered_context.get("call_disposition") or "").strip().lower()
    if disposition in _TRANSFER_DISPOSITIONS:
        return True

    tags = gathered_context.get("call_tags") or []
    return any(
        isinstance(tag, str) and tag.strip().lower() in _TRANSFER_DISPOSITIONS
        for tag in tags
    )


def _coerce_content(content: Any) -> str:
    """Reduce an LLM message's ``content`` to plain text.

    Content is usually a string, but multimodal turns arrive as a list of parts;
    join the text parts and drop the rest. ConvoMem's capture schema requires a
    non-empty string per message.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return " ".join(p for p in parts if p).strip()
    return ""


def extract_transcript(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Turn LLM context messages into a ConvoMem transcript.

    Keeps only real conversation turns (user + assistant with text). The system
    prompt is deliberately excluded — it carries the agent's own instructions and
    the injected memory briefing, neither of which should be fed back into memory
    extraction. Assistant tool-call turns (no text content) fall away naturally.
    """
    transcript: list[dict[str, str]] = []
    for message in messages or []:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _coerce_content(message.get("content"))
        if not content:
            continue
        transcript.append({"role": role, "content": content})
    return transcript


class ConvoMemRuntimeSession(IntegrationRuntimeSession):
    """Seals the final transcript into a snapshot for the completion handler.

    Optionally (``live_capture``) also streams transcript deltas during the call.
    """

    name = "convomem"

    def __init__(
        self,
        *,
        context_messages_provider,
        phone: str | None,
        channel: str = CHANNEL_VOICE,
        workflow_run_id: int | None = None,
        live_capture: bool = False,
        config: ConvoMemConfig | None = None,
        interval_seconds: float = LIVE_CAPTURE_INTERVAL_SECONDS,
        max_flushes: int = LIVE_CAPTURE_MAX_FLUSHES,
    ) -> None:
        self._context_messages_provider = context_messages_provider
        self._phone = phone
        self._channel = channel
        self._workflow_run_id = workflow_run_id
        # Live capture needs a phone (identity) and a config (where to POST).
        self._live_capture = bool(live_capture and config is not None and phone)
        self._config = config
        self._interval_seconds = interval_seconds
        self._max_flushes = max_flushes

        # How many transcript turns have already been streamed live, so each
        # flush sends only the delta since the last one.
        self._flushed_count = 0
        self._checkpoint = 0
        self._bg_task: asyncio.Task | None = None

    def attach(self, task: Any) -> None:
        # Nothing to observe live for the end-of-call snapshot — the whole
        # transcript is read at call-finish. Live capture, when enabled, runs as
        # its own background task so it never touches the frame path.
        if self._live_capture:
            self._bg_task = asyncio.create_task(self._live_capture_loop())

    async def _live_capture_loop(self) -> None:
        """Periodically stream transcript deltas, off the frame path.

        Runs as an independent task: awaiting a flush here yields the event loop
        (async httpx) so pipeline frames keep flowing, and a slow/failed flush
        can never block or crash the call.
        """
        try:
            while self._checkpoint < self._max_flushes:
                await asyncio.sleep(self._interval_seconds)
                await self._flush_live_delta()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a background task must never surface
            logger.warning("[convomem] live-capture loop stopped early: {}", exc)

    async def _flush_live_delta(self) -> bool:
        """POST the transcript turns added since the last live flush.

        Never raises. No close-out flags — these are ACTIVE-conversation appends;
        the conversation is closed only by the end-of-call capture. Distinct
        idempotency key per checkpoint so ConvoMem doesn't treat a flush as a
        replay of the previous one.
        """
        if not self._live_capture or self._config is None or not self._phone:
            return False
        try:
            messages = list(self._context_messages_provider() or [])
        except Exception:
            return False

        transcript = extract_transcript(messages)
        delta = transcript[self._flushed_count :]
        if not delta:
            return False

        payload = {
            "messages": delta,
            "phoneNumber": self._phone,
            "channel": self._channel,
            "idempotencyKey": f"dograh-run-{self._workflow_run_id}-chk-{self._checkpoint}",
        }
        # Advance optimistically: the end-of-call capture re-sends the full
        # transcript as the backstop, and ConvoMem content-dedups, so a dropped
        # live flush self-heals rather than duplicating.
        self._flushed_count = len(transcript)
        self._checkpoint += 1

        result = await capture(self._config, payload)
        logger.info(
            "[convomem] live flush #{} sent {} turn(s): {}",
            self._checkpoint - 1,
            len(delta),
            result.get("status"),
        )
        return True

    async def on_call_finished(
        self,
        *,
        gathered_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        # Stop the live loop first, if running.
        if self._bg_task is not None:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except (asyncio.CancelledError, Exception):
                pass
            self._bg_task = None

        try:
            messages = self._context_messages_provider() or []
        except Exception as exc:
            logger.warning("[convomem] could not read transcript at call end: {}", exc)
            return None

        transcript = extract_transcript(messages)
        if not transcript:
            logger.info("[convomem] empty transcript at call end, nothing to capture")
            return None

        # Live capture off → the end flush is the whole transcript (source of
        # truth). Live capture on → the earlier turns were already streamed, so
        # send only the turns since the last live flush; that avoids re-extracting
        # everything and the exact-content repeat-drop a full re-send would cause.
        # The end flush must still CLOSE the conversation, and /capture needs >=1
        # message, so if everything was already streamed we re-send just the last
        # turn as a close-out carrier (ConvoMem content-dedups it — no duplicate).
        if self._live_capture:
            final_messages = transcript[self._flushed_count :] or [transcript[-1]]
        else:
            final_messages = transcript

        snapshot = {
            "messages": final_messages,
            "phone": self._phone,
            "channel": self._channel,
            "escalate": _was_escalated(gathered_context),
            "disposition": gathered_context.get("call_disposition"),
        }
        return {SNAPSHOT_KEY: snapshot}


def create_runtime_sessions(
    context: IntegrationRuntimeContext,
) -> list[IntegrationRuntimeSession]:
    """Return a runtime session if an enabled ConvoMem node wants post-call capture."""
    node = find_enabled_node(context.workflow_graph.nodes.values())
    if node is None:
        return []

    live_capture = bool(getattr(node, "convomem_live_capture", False))
    config: ConvoMemConfig | None = None
    if live_capture:
        try:
            config = ConvoMemConfig(api_key=node.convomem_api_key or "")
        except Exception as exc:
            # Can't stream live without a usable config — fall back to end-of-call
            # capture only, which needs no live config.
            logger.warning(
                "[convomem] live capture requested but config invalid, "
                "disabling it: {}",
                exc,
            )
            live_capture = False

    return [
        ConvoMemRuntimeSession(
            context_messages_provider=context.context_messages_provider,
            phone=resolve_customer_phone(context.workflow_run),
            workflow_run_id=context.workflow_run_id,
            live_capture=live_capture,
            config=config,
        )
    ]
