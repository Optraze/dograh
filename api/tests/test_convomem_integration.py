"""Tests for the ConvoMem integration package.

Covers the AGENTS.md integration checklist: node model validation, generated
spec/example validity, secret masking + masked round-trip preservation, runtime
snapshot creation, and the completion handler (happy path + skip paths). Plus the
two ConvoMem-specific seams: the never-raises HTTP client and the pre-call
capabilities factory.

No network or DB: the HTTP layer is faked, and the higher-level client functions
are monkeypatched where the capabilities/completion logic is under test.
"""

from __future__ import annotations

import pytest

from api.services.configuration.masking import (
    MASK_MARKER,
    mask_workflow_definition,
    merge_workflow_api_keys,
)
from api.services.integrations import get_node_secret_fields
from api.services.integrations.base import IntegrationCompletionContext
from api.services.integrations.convomem import capabilities as caps
from api.services.integrations.convomem import client as convomem_client
from api.services.integrations.convomem import completion as completion_mod
from api.services.integrations.convomem import runtime as runtime_mod
from api.services.integrations.convomem.client import ConvoMemConfig
from api.services.integrations.convomem.node import ConvoMemNodeData
from api.services.integrations.convomem.runtime import (
    ConvoMemRuntimeSession,
    create_runtime_sessions,
    extract_transcript,
)

_KEY = "sk-org-abcdef0123456789"


# --------------------------------------------------------------------------- #
# Lightweight fakes for the runtime/completion contexts                        #
# --------------------------------------------------------------------------- #


class _FakeNode:
    def __init__(self, node_type: str, data):
        self.node_type = node_type
        self.data = data


class _FakeGraph:
    def __init__(self, nodes):
        self._nodes = {i: n for i, n in enumerate(nodes)}

    @property
    def nodes(self):
        return self._nodes


class _FakeWorkflowRun:
    def __init__(self, *, initial_context=None, logs=None, call_type=None):
        self.initial_context = initial_context or {}
        self.logs = logs or {}
        self.call_type = call_type


class _FakeRuntimeContext:
    def __init__(
        self,
        nodes,
        *,
        initial_context=None,
        messages=None,
        call_type=None,
        workflow_run_id=42,
    ):
        self.workflow_graph = _FakeGraph(nodes)
        self.workflow_run = _FakeWorkflowRun(
            initial_context=initial_context, call_type=call_type
        )
        self.workflow_run_id = workflow_run_id
        self.context_messages_provider = lambda: messages or []


def _node_data(**over):
    base = {"name": "Caller Memory", "convomem_enabled": True, "convomem_api_key": _KEY}
    base.update(over)
    return ConvoMemNodeData.model_validate(base)


def _completion_context(logs, workflow_run_id=42):
    return IntegrationCompletionContext(
        workflow_run_id=workflow_run_id,
        workflow_run=_FakeWorkflowRun(logs=logs),
        workflow_definition={},
        definition_id=1,
        organization_id=1,
        public_token=None,
    )


# --------------------------------------------------------------------------- #
# Fake HTTP layer for client.py                                                #
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=b"{}"):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeAsyncClient:
    def __init__(self, *, response=None, raise_exc=None, captured=None):
        self._response = response
        self._raise = raise_exc
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *a, **k):
        if self._captured is not None:
            self._captured.update(k)
        if self._raise:
            raise self._raise
        return self._response

    async def post(self, *a, **k):
        if self._captured is not None:
            self._captured.update(k)
        if self._raise:
            raise self._raise
        return self._response


def _patch_http(monkeypatch, *, response=None, raise_exc=None, captured=None):
    monkeypatch.setattr(
        convomem_client.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeAsyncClient(
            response=response, raise_exc=raise_exc, captured=captured
        ),
    )


# --------------------------------------------------------------------------- #
# Node model validation                                                        #
# --------------------------------------------------------------------------- #


def test_enabled_node_requires_api_key():
    with pytest.raises(ValueError, match="convomem_api_key"):
        ConvoMemNodeData.model_validate({"name": "x", "convomem_enabled": True})


def test_disabled_node_allows_missing_key():
    data = ConvoMemNodeData.model_validate({"name": "x", "convomem_enabled": False})
    assert data.convomem_enabled is False


def test_defaults():
    data = _node_data()
    assert data.convomem_enabled is True
    assert data.convomem_live_capture is False


def test_recall_and_capture_have_no_toggles():
    # Enabling the node IS the intent: recall + capture always run and the agent
    # may greet by name. No sub-toggles — matches the peers' one-enable norm
    # (paygent/tuner/noveum), so the node stays Enabled + API Key + Live capture.
    for removed in (
        "convomem_recall_enabled",
        "convomem_capture_enabled",
        "convomem_greeting_hint",
    ):
        assert removed not in ConvoMemNodeData.model_fields


def test_base_url_is_not_a_user_field():
    # The base URL is a deployment constant/env, not a per-workflow field: there
    # is no user-controlled host to redirect (SSRF), matching the peer
    # integrations (paygent/tuner/noveum). Self-host overrides via CONVOMEM_BASE_URL.
    from api.services.integrations.convomem.client import (
        DEFAULT_BASE_URL,
        ConvoMemConfig,
    )

    assert "convomem_base_url" not in ConvoMemNodeData.model_fields
    assert ConvoMemConfig(api_key="sk-org-x").base_url == DEFAULT_BASE_URL


def test_secret_field_registered():
    assert get_node_secret_fields("convomem") == ("convomem_api_key",)


def test_example_validates_against_model():
    # The node ships exactly one example; it must validate against the model.
    from api.services.integrations.convomem.node import SPEC

    assert SPEC.examples
    for example in SPEC.examples:
        ConvoMemNodeData.model_validate(example.data)


# --------------------------------------------------------------------------- #
# Secret masking + masked round-trip preservation                             #
# --------------------------------------------------------------------------- #


def _definition_with_key(api_key):
    return {
        "nodes": [
            {
                "id": "n1",
                "type": "convomem",
                "data": {"name": "M", "convomem_api_key": api_key},
            },
        ]
    }


def test_masking_masks_the_api_key():
    masked = mask_workflow_definition(_definition_with_key(_KEY))
    value = masked["nodes"][0]["data"]["convomem_api_key"]
    assert MASK_MARKER in value
    assert value != _KEY
    assert value.endswith(_KEY[-4:])


def test_masked_roundtrip_preserves_real_key():
    existing = _definition_with_key(_KEY)
    masked_value = mask_workflow_definition(existing)["nodes"][0]["data"][
        "convomem_api_key"
    ]
    # UI sends the definition back with the still-masked key.
    incoming = _definition_with_key(masked_value)
    merged = merge_workflow_api_keys(incoming, existing)
    assert merged["nodes"][0]["data"]["convomem_api_key"] == _KEY


# --------------------------------------------------------------------------- #
# Client — never raises, correct status handling                              #
# --------------------------------------------------------------------------- #


def test_config_rejects_empty_key_and_normalises_base_url():
    with pytest.raises(ValueError):
        ConvoMemConfig(api_key="   ")
    cfg = ConvoMemConfig(api_key=_KEY, base_url="https://x.example.com/")
    assert cfg.base_url == "https://x.example.com"
    assert cfg.url("/capture") == "https://x.example.com/api/v1/capture"
    assert cfg.headers() == {"X-API-Key": _KEY}


async def test_lookup_returns_data_on_success(monkeypatch):
    _patch_http(monkeypatch, response=_FakeResponse(200, {"found": True}))
    out = await convomem_client.lookup_customer(
        ConvoMemConfig(api_key=_KEY), phone="+1"
    )
    assert out == {"found": True}


async def test_lookup_returns_empty_on_http_error(monkeypatch):
    _patch_http(monkeypatch, response=_FakeResponse(500, text="boom"))
    out = await convomem_client.lookup_customer(
        ConvoMemConfig(api_key=_KEY), phone="+1"
    )
    assert out == {}


async def test_lookup_returns_empty_on_network_error(monkeypatch):
    _patch_http(monkeypatch, raise_exc=RuntimeError("connection refused"))
    out = await convomem_client.lookup_customer(
        ConvoMemConfig(api_key=_KEY), phone="+1"
    )
    assert out == {}


async def test_lookup_sends_max_tokens_when_set(monkeypatch):
    captured = {}
    _patch_http(
        monkeypatch, response=_FakeResponse(200, {"found": True}), captured=captured
    )
    await convomem_client.lookup_customer(
        ConvoMemConfig(api_key=_KEY), phone="+1", max_tokens=250
    )
    assert captured["params"]["maxTokens"] == "250"


async def test_lookup_omits_max_tokens_when_unset(monkeypatch):
    captured = {}
    _patch_http(
        monkeypatch, response=_FakeResponse(200, {"found": True}), captured=captured
    )
    await convomem_client.lookup_customer(ConvoMemConfig(api_key=_KEY), phone="+1")
    assert "maxTokens" not in captured["params"]


async def test_capture_success(monkeypatch):
    _patch_http(
        monkeypatch,
        response=_FakeResponse(200, {"conversationId": "c1"}, content=b"{}"),
    )
    out = await convomem_client.capture(ConvoMemConfig(api_key=_KEY), {"messages": []})
    assert out["status"] == "delivered"
    assert out["conversationId"] == "c1"


async def test_capture_delivery_status_wins_over_body_status(monkeypatch):
    # ConvoMem's 201 body carries its own "status" (conversation state, e.g.
    # "new"); it must not shadow our delivery status.
    _patch_http(
        monkeypatch,
        response=_FakeResponse(201, {"status": "new", "conversationId": "c9"}),
    )
    out = await convomem_client.capture(ConvoMemConfig(api_key=_KEY), {"messages": []})
    assert out["status"] == "delivered"
    assert out["conversationId"] == "c9"


async def test_capture_error_status_on_http_error(monkeypatch):
    _patch_http(monkeypatch, response=_FakeResponse(422, text="bad"))
    out = await convomem_client.capture(ConvoMemConfig(api_key=_KEY), {"messages": []})
    assert out["status"] == "error"
    assert out["status_code"] == 422


async def test_capture_never_raises_on_network_error(monkeypatch):
    _patch_http(monkeypatch, raise_exc=RuntimeError("down"))
    out = await convomem_client.capture(ConvoMemConfig(api_key=_KEY), {"messages": []})
    assert out["status"] == "error"


# --------------------------------------------------------------------------- #
# Capabilities — pre-call recall                                              #
# --------------------------------------------------------------------------- #


def test_lookup_result_to_context_vars_known_customer():
    out = caps.lookup_result_to_context_vars(
        {
            "found": True,
            "isNewCustomer": False,
            "customer": {"name": "Jane Doe"},
            "context": "Prefers mornings.",
            "openIssue": {"summary": "refund pending"},
        }
    )
    assert out == {
        "convomem_customer_known": True,
        "convomem_customer_name": "Jane Doe",
        "convomem_memory_briefing": "Prefers mornings.",
        "convomem_open_issue": "refund pending",
    }


@pytest.mark.parametrize(
    "result",
    [
        {"found": True, "isNewCustomer": True, "customer": {"name": "New Caller"}},
        {"found": False, "customer": None},
        {},
    ],
)
def test_lookup_result_to_context_vars_unknown_or_new(result):
    # New/unknown callers surface only the flag — never a placeholder name.
    assert caps.lookup_result_to_context_vars(result) == {
        "convomem_customer_known": False
    }


# Real numbers from the outbound-call bug: caller_number is the agent's Twilio
# number, called/phone is the customer.
_TWILIO = "+19043319841"
_CUSTOMER = "+919850055395"


def test_resolve_phone_outbound_uses_customer_not_twilio():
    # On outbound, caller_number is the agent's Twilio number and MUST be ignored.
    wr = _FakeWorkflowRun(
        initial_context={
            "caller_number": _TWILIO,
            "called_number": _CUSTOMER,
            "phone_number": _CUSTOMER,
        },
        call_type="outbound",
    )
    assert caps.resolve_customer_phone(wr) == _CUSTOMER


def test_resolve_phone_inbound_uses_caller():
    wr = _FakeWorkflowRun(
        initial_context={"caller_number": _CUSTOMER, "called_number": _TWILIO},
        call_type="inbound",
    )
    assert caps.resolve_customer_phone(wr) == _CUSTOMER


def test_resolve_phone_defaults_to_outbound_ordering():
    # No call_type (the column defaults to 'outbound') → outbound ordering, so a
    # present phone_number wins over the Twilio caller_number.
    wr = _FakeWorkflowRun(
        initial_context={"caller_number": _TWILIO, "phone_number": _CUSTOMER}
    )
    assert caps.resolve_customer_phone(wr) == _CUSTOMER


def test_resolve_phone_tolerates_enum_call_type():
    class _Enum:
        value = "inbound"

    wr = _FakeWorkflowRun(
        initial_context={"caller_number": _CUSTOMER, "called_number": _TWILIO},
        call_type=_Enum(),
    )
    assert caps.resolve_customer_phone(wr) == _CUSTOMER


def test_resolve_phone_none_when_no_identity():
    assert caps.resolve_customer_phone(_FakeWorkflowRun(initial_context={})) is None


def test_create_capabilities_opts_out_without_node():
    assert caps.create_call_capabilities(_FakeRuntimeContext([])) is None


def test_create_capabilities_runs_recall_whenever_enabled():
    # Recall has no toggle: an enabled node with a caller phone always produces
    # a pre-call capability.
    ctx = _FakeRuntimeContext(
        [_FakeNode("convomem", _node_data())],
        initial_context={"caller_number": "+15551234567"},
    )
    assert caps.create_call_capabilities(ctx) is not None


def test_create_capabilities_opts_out_without_phone():
    ctx = _FakeRuntimeContext([_FakeNode("convomem", _node_data())], initial_context={})
    assert caps.create_call_capabilities(ctx) is None


async def test_run_pre_call_maps_lookup(monkeypatch):
    async def fake_lookup(config, *, phone, auto_create=True, max_tokens=None):
        assert phone == "+15551234567"
        assert max_tokens is None  # default node sets no budget
        return {
            "found": True,
            "isNewCustomer": False,
            "customer": {"name": "Jane"},
            "context": "Loves mornings.",
        }

    monkeypatch.setattr(caps, "lookup_customer", fake_lookup)
    ctx = _FakeRuntimeContext(
        [_FakeNode("convomem", _node_data())],
        initial_context={"caller_number": "+15551234567"},
    )
    capability = caps.create_call_capabilities(ctx)
    assert capability is not None and capability.name == "convomem"

    vars_out = await capability.run_pre_call()
    assert vars_out["convomem_customer_known"] is True
    assert vars_out["convomem_customer_name"] == "Jane"

    # Addendum reflects the merged pre-call vars, framed as untrusted background
    # (the briefing is caller-derived, so the wrapper must not present it as
    # trusted fact — it reinforces ConvoMem's own "unverified" spotlighting).
    addendum = capability.prompt_addendum(vars_out)
    assert "unverified" in addendum.lower()
    assert "never authorises" in addendum or "not instructions" in addendum
    assert "Loves mornings." in addendum

    # No briefing → no addendum (agent's prompt is byte-identical).
    assert capability.prompt_addendum({"convomem_customer_known": False}) is None


async def test_run_pre_call_passes_memory_budget(monkeypatch):
    seen = {}

    async def fake_lookup(config, *, phone, auto_create=True, max_tokens=None):
        seen["max_tokens"] = max_tokens
        return {
            "found": True,
            "isNewCustomer": False,
            "customer": {"name": "Jane"},
            "context": "x",
        }

    monkeypatch.setattr(caps, "lookup_customer", fake_lookup)
    ctx = _FakeRuntimeContext(
        [_FakeNode("convomem", _node_data(convomem_memory_budget_tokens=300))],
        initial_context={"caller_number": "+15551234567"},
    )
    capability = caps.create_call_capabilities(ctx)
    await capability.run_pre_call()
    assert seen["max_tokens"] == 300


# --------------------------------------------------------------------------- #
# Runtime — transcript snapshot                                               #
# --------------------------------------------------------------------------- #


def test_extract_transcript_filters_system_and_toolcalls():
    messages = [
        {"role": "system", "content": "SECRET agent prompt"},
        {"role": "user", "content": "  I was double charged  "},
        {"role": "assistant", "content": None},  # tool call turn
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Checking"}, {"type": "image"}],
        },
        {"role": "assistant", "content": "Refund issued"},
        {"role": "user", "content": ""},
    ]
    assert extract_transcript(messages) == [
        {"role": "user", "content": "I was double charged"},
        {"role": "assistant", "content": "Checking"},
        {"role": "assistant", "content": "Refund issued"},
    ]


async def test_on_call_finished_builds_snapshot():
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
        phone="+15551234567",
    )
    out = await session.on_call_finished(
        gathered_context={"call_disposition": "user_hangup"}
    )
    snap = out["convomem_snapshot"]
    assert snap["phone"] == "+15551234567"
    assert snap["channel"] == "VOICE"
    assert snap["escalate"] is False
    assert len(snap["messages"]) == 2


async def test_on_call_finished_marks_transfer_as_escalated():
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: [
            {"role": "user", "content": "get me a human"}
        ],
        phone="+1",
    )
    out = await session.on_call_finished(
        gathered_context={"call_disposition": "transfer_call"}
    )
    assert out["convomem_snapshot"]["escalate"] is True


async def test_on_call_finished_empty_transcript_returns_none():
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: [
            {"role": "system", "content": "prompt only"}
        ],
        phone="+1",
    )
    assert await session.on_call_finished(gathered_context={}) is None


def test_create_runtime_sessions_builds_one_session():
    ctx = _FakeRuntimeContext(
        [_FakeNode("convomem", _node_data())],
        initial_context={"caller_number": "+15551234567"},
    )
    sessions = create_runtime_sessions(ctx)
    assert len(sessions) == 1 and sessions[0].name == "convomem"


# --------------------------------------------------------------------------- #
# Runtime — live (mid-call) capture, opt-in                                   #
# --------------------------------------------------------------------------- #


def test_node_live_capture_defaults_off():
    assert _node_data().convomem_live_capture is False


def test_live_capture_off_by_default_in_runtime():
    ctx = _FakeRuntimeContext(
        [_FakeNode("convomem", _node_data())],
        initial_context={"caller_number": "+15551234567"},
        call_type="outbound",
    )
    session = create_runtime_sessions(ctx)[0]
    assert session._live_capture is False


def test_live_capture_enabled_builds_streaming_session():
    ctx = _FakeRuntimeContext(
        [_FakeNode("convomem", _node_data(convomem_live_capture=True))],
        initial_context={"phone_number": "+15551234567"},
        call_type="outbound",
    )
    session = create_runtime_sessions(ctx)[0]
    assert session._live_capture is True


async def test_live_flush_sends_only_new_turns(monkeypatch):
    sent = []

    async def fake_capture(config, payload):
        sent.append(payload)
        return {"status": "delivered"}

    monkeypatch.setattr(runtime_mod, "capture", fake_capture)

    messages: list = []
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: messages,
        phone="+919850055395",
        workflow_run_id=5,
        live_capture=True,
        config=ConvoMemConfig(api_key=_KEY),
    )

    # First turn arrives → flush sends it, no close-out flags, checkpoint 0.
    messages.append({"role": "user", "content": "hi"})
    assert await session._flush_live_delta() is True
    assert sent[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert "endConversation" not in sent[0] and "escalate" not in sent[0]
    assert sent[0]["idempotencyKey"] == "dograh-run-5-chk-0"
    assert sent[0]["phoneNumber"] == "+919850055395"

    # No new turns → nothing sent.
    assert await session._flush_live_delta() is False
    assert len(sent) == 1

    # New turns → only the delta, next checkpoint key.
    messages.append({"role": "assistant", "content": "hello"})
    messages.append({"role": "user", "content": "refund?"})
    assert await session._flush_live_delta() is True
    assert sent[1]["messages"] == [
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "refund?"},
    ]
    assert sent[1]["idempotencyKey"] == "dograh-run-5-chk-1"


async def test_live_flush_noops_when_disabled(monkeypatch):
    async def fail_capture(config, payload):  # pragma: no cover - must not run
        raise AssertionError("live flush must not POST when disabled")

    monkeypatch.setattr(runtime_mod, "capture", fail_capture)
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: [{"role": "user", "content": "hi"}],
        phone="+1",
        live_capture=False,
    )
    assert await session._flush_live_delta() is False


def test_live_capture_needs_phone():
    # Requested on, but no phone to identify the customer → not live.
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: [],
        phone=None,
        live_capture=True,
        config=ConvoMemConfig(api_key=_KEY),
    )
    assert session._live_capture is False


async def test_end_flush_is_full_when_live_capture_off():
    # Live off → the end-of-call snapshot is the whole transcript (source of truth).
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
        phone="+1",
    )
    out = await session.on_call_finished(gathered_context={})
    assert [m["content"] for m in out["convomem_snapshot"]["messages"]] == ["a", "b"]


async def test_end_flush_is_delta_only_when_live(monkeypatch):
    async def fake_capture(config, payload):
        return {"status": "delivered"}

    monkeypatch.setattr(runtime_mod, "capture", fake_capture)

    messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: messages,
        phone="+1",
        workflow_run_id=5,
        live_capture=True,
        config=ConvoMemConfig(api_key=_KEY),
    )
    # Stream the first two turns live.
    assert await session._flush_live_delta() is True
    # Two more turns arrive after the last live flush.
    messages.append({"role": "user", "content": "c"})
    messages.append({"role": "assistant", "content": "d"})

    out = await session.on_call_finished(
        gathered_context={"call_disposition": "user_hangup"}
    )
    # The end flush carries ONLY the new turns — not the already-streamed a/b.
    assert [m["content"] for m in out["convomem_snapshot"]["messages"]] == ["c", "d"]
    assert out["convomem_snapshot"]["escalate"] is False


async def test_end_flush_carrier_when_everything_streamed(monkeypatch):
    async def fake_capture(config, payload):
        return {"status": "delivered"}

    monkeypatch.setattr(runtime_mod, "capture", fake_capture)

    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    session = ConvoMemRuntimeSession(
        context_messages_provider=lambda: messages,
        phone="+1",
        workflow_run_id=5,
        live_capture=True,
        config=ConvoMemConfig(api_key=_KEY),
    )
    # Everything gets streamed live; no new turns before hang-up.
    assert await session._flush_live_delta() is True

    out = await session.on_call_finished(
        gathered_context={"call_disposition": "transfer_call"}
    )
    snap = out["convomem_snapshot"]
    # Delta is empty, so the last turn rides as a deduped close-out carrier...
    assert [m["content"] for m in snap["messages"]] == ["hi"]
    # ...and it still carries the escalation so the conversation is closed.
    assert snap["escalate"] is True


# --------------------------------------------------------------------------- #
# Completion — the post-call flush                                            #
# --------------------------------------------------------------------------- #


def _convomem_node_dict(**over):
    data = {"name": "M", "convomem_enabled": True, "convomem_api_key": _KEY}
    data.update(over)
    return {"id": "n1", "type": "convomem", "data": data}


async def test_run_completion_happy_path(monkeypatch):
    captured = {}

    async def fake_capture(config, payload):
        captured["config"] = config
        captured["payload"] = payload
        return {"status": "delivered", "conversationId": "c1"}

    monkeypatch.setattr(completion_mod, "capture", fake_capture)

    snapshot = {
        "convomem_snapshot": {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
            "phone": "+15551234567",
            "channel": "VOICE",
            "escalate": False,
            "disposition": "user_hangup",
        }
    }
    ctx = _completion_context(snapshot, workflow_run_id=99)
    results = await completion_mod.run_completion([_convomem_node_dict()], ctx)

    assert results["convomem_n1"]["status"] == "delivered"
    assert results["convomem_n1"]["message_count"] == 2
    assert captured["payload"]["endConversation"] is True
    assert captured["payload"]["idempotencyKey"] == "dograh-run-99"
    assert captured["payload"]["phoneNumber"] == "+15551234567"
    assert captured["config"].api_key == _KEY


async def test_run_completion_sends_escalate_for_transfer(monkeypatch):
    seen = {}

    async def fake_capture(config, payload):
        seen["payload"] = payload
        return {"status": "delivered"}

    monkeypatch.setattr(completion_mod, "capture", fake_capture)
    snapshot = {
        "convomem_snapshot": {
            "messages": [{"role": "user", "content": "human please"}],
            "phone": "+1",
            "channel": "VOICE",
            "escalate": True,
            "disposition": "transfer_call",
        }
    }
    await completion_mod.run_completion(
        [_convomem_node_dict()], _completion_context(snapshot)
    )
    assert seen["payload"]["escalate"] is True
    assert "endConversation" not in seen["payload"]
    assert seen["payload"]["reason"] == "transfer_call"


async def test_run_completion_skips_disabled_node(monkeypatch):
    async def fail_capture(config, payload):  # pragma: no cover - must not run
        raise AssertionError("capture must not be called when the node is disabled")

    monkeypatch.setattr(completion_mod, "capture", fail_capture)
    snapshot = {
        "convomem_snapshot": {
            "messages": [{"role": "user", "content": "x"}],
            "phone": "+1",
        }
    }
    results = await completion_mod.run_completion(
        [_convomem_node_dict(convomem_enabled=False)], _completion_context(snapshot)
    )
    assert results == {}


async def test_run_completion_reports_missing_snapshot(monkeypatch):
    async def fail_capture(config, payload):  # pragma: no cover - must not run
        raise AssertionError("capture must not be called without a snapshot")

    monkeypatch.setattr(completion_mod, "capture", fail_capture)
    results = await completion_mod.run_completion(
        [_convomem_node_dict()], _completion_context({})
    )
    assert results["convomem_n1"]["error"] == "missing_runtime_snapshot"
