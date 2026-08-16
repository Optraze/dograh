"""ConvoMem HTTP client.

Thin ``httpx`` wrapper over the two ConvoMem endpoints this integration uses:

- ``GET  /api/v1/customers/lookup`` — pre-call recall (who is calling, what we
  already know about them).
- ``POST /api/v1/capture``          — post-call flush (the transcript, and
  whether the call ended normally or was escalated to a human).

Every function here is best-effort by construction and **never raises** into the
caller. A third-party memory service being slow or down must degrade to "no
enrichment / no flush", never fail a call or a post-call task. ConvoMem is not a
PyPI package, so there is no SDK to depend on; ``httpx`` is already a Dograh
dependency.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, field_validator

# The base URL is a deployment constant, not a per-workflow field: like the peer
# integrations (paygent/tuner/noveum) the editor never sets it, so there is no
# user-controlled host to validate against SSRF. Self-hosted ConvoMem overrides
# it with the CONVOMEM_BASE_URL env var, the same way tuner uses TUNER_BASE_URL.
DEFAULT_BASE_URL = os.getenv("CONVOMEM_BASE_URL", "https://api.convomem.com")

# Recall sits on the pre-call ringer budget, so keep it short; capture runs in a
# post-call task and can afford a little more.
_LOOKUP_TIMEOUT_SECONDS = 8.0
_CAPTURE_TIMEOUT_SECONDS = 10.0

_API_PREFIX = "/api/v1"


class ConvoMemConfig(BaseModel):
    """Connection config resolved from a ``convomem`` node."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str

    @field_validator("api_key")
    @classmethod
    def _api_key_must_not_be_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, value: str) -> str:
        cleaned = (value or "").strip() or DEFAULT_BASE_URL
        return cleaned.rstrip("/")

    def url(self, path: str) -> str:
        return f"{self.base_url}{_API_PREFIX}{path}"

    def headers(self) -> dict[str, str]:
        # ConvoMem authenticates an org (optionally agent-scoped) API key via the
        # X-API-Key header — the key looks like ``sk-org-...``.
        return {"X-API-Key": self.api_key}


async def lookup_customer(
    config: ConvoMemConfig,
    *,
    phone: str,
    auto_create: bool = True,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Fetch the caller's profile + memory briefing. Returns ``{}`` on any failure.

    A ``{}`` result means "no enrichment for this call" — the caller is treated
    as unknown and the conversation proceeds without a memory briefing.

    ``max_tokens`` caps the injected memory block for this call only (ConvoMem's
    ``?maxTokens=`` — clamped server-side to <= 800). ``None`` leaves it to
    ConvoMem's per-agent / default budget.
    """
    params = {"phone": phone, "autoCreate": "true" if auto_create else "false"}
    if max_tokens is not None:
        params["maxTokens"] = str(max_tokens)
    try:
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                config.url("/customers/lookup"),
                params=params,
                headers=config.headers(),
            )
    except Exception as exc:
        logger.warning("[convomem] lookup failed for {}: {}", phone, exc)
        return {}

    if response.status_code >= 400:
        logger.warning(
            "[convomem] lookup HTTP {} for {}: {}",
            response.status_code,
            phone,
            response.text[:200],
        )
        return {}

    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def capture(
    config: ConvoMemConfig,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Post the completed transcript to ConvoMem. Never raises.

    Returns a small status dict — ``{"status": "delivered", ...}`` on success,
    ``{"status": "error", ...}`` otherwise — so the completion handler can record
    an outcome per node without a dropped flush affecting anything else.
    """
    try:
        async with httpx.AsyncClient(timeout=_CAPTURE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                config.url("/capture"),
                json=payload,
                headers=config.headers(),
            )
    except Exception as exc:
        logger.error("[convomem] capture failed: {}", exc)
        return {"status": "error", "error": str(exc)}

    if response.status_code >= 400:
        logger.error(
            "[convomem] capture HTTP {}: {}",
            response.status_code,
            response.text[:200],
        )
        return {"status": "error", "status_code": response.status_code}

    try:
        body = response.json() if response.content else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    logger.info(
        "[convomem] capture delivered ({}), conversation={}",
        response.status_code,
        body.get("conversationId"),
    )
    # Spread the body first so our delivery status/status_code always win — the
    # response body carries its own "status" (the conversation state), which must
    # not shadow the delivery outcome the caller checks.
    return {**body, "status": "delivered", "status_code": response.status_code}
