# ABOUTME: Tests for dogwood_evaluator, the auto mode evaluator that asks a VerifyToolCalls Nexus
# operation after recording the run's input events. Pure tests cover the verdict mapping, the
# script parameter and the input events. End-to-end tests run a real agent on the dev server
# against a stand-in handler that answers the way the Dogwood verifier does: json/protobuf payloads
# carrying the response messageTypes.

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta
from typing import Any

import nexusrpc
import nexusrpc.handler
import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalVerdict,
    ReceivedInput,
    ReceivedInputKind,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.dogwood_approvals import (
    Decision,
    RecordInputEventsResponse,
    VerifyToolCallsResponse,
    dogwood_evaluator,
    decide,
    input_events,
    script_parameter,
)
from temporal_agent_harness.harness.code_mode import analyze_script
from temporal_agent_harness.harness.jev_approvals import (
    JEV_CLASSIFY_ACTIVITY,
    JevApprovalRequest,
    JevClassificationAnswer,
    ScriptFeature,
    jev_script_classifier,
)

# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def _response(decision: str, reason: str = "") -> VerifyToolCallsResponse:
    return VerifyToolCallsResponse(decision=Decision.Value(decision), reason=reason)


@pytest.mark.parametrize(
    ("decision", "verdict"),
    [
        ("DECISION_APPROVED", AutoApprovalVerdict.APPROVE),
        ("DECISION_DENIED", AutoApprovalVerdict.DENY),
        ("DECISION_ESCALATE", AutoApprovalVerdict.ESCALATE),
    ],
)
def test_decide(decision: str, verdict: AutoApprovalVerdict) -> None:
    result = decide(_response(decision), details={"action": "p/refund"})
    assert result.verdict is verdict
    assert result.reason
    assert result.details["action"] == "p/refund"
    assert result.details["decision"] == decision


@pytest.mark.parametrize("decision", ["DECISION_RETRY_LATER", "DECISION_UNSPECIFIED"])
def test_no_decision_raises(decision: str) -> None:
    with pytest.raises(RuntimeError, match=decision):
        decide(_response(decision, "rate limited"))


def _verifier_payload(message: str, data: bytes) -> Payload:
    """A payload as the Rust verifiers build it (verifiers/host/src/lib.rs, json_proto_payload)."""
    return Payload(
        metadata={
            "encoding": b"json/protobuf",
            "messageType": f"temporal_untrusted_workers.toolpolicy.v1.{message}".encode(),
        },
        data=data,
    )


def test_decodes_the_verifiers_response_payloads() -> None:
    [response, recorded] = pydantic_data_converter.payload_converter.from_payloads(
        [
            _verifier_payload(
                "VerifyToolCallsResponse",
                b'{"decision":"DECISION_ESCALATE","reason":"tool p/x escalated by Dogwood policy x"}',
            ),
            _verifier_payload("RecordInputEventsResponse", b"{}"),
        ]
    )
    assert isinstance(response, VerifyToolCallsResponse)
    assert response.decision == Decision.Value("DECISION_ESCALATE")
    assert response.reason == "tool p/x escalated by Dogwood policy x"
    assert isinstance(recorded, RecordInputEventsResponse)


def test_input_events() -> None:
    received = [
        ReceivedInput(
            kind=ReceivedInputKind.MESSAGE, name="ask", payload={"text": "hi"}, id="upd-1"
        ),
        ReceivedInput(kind=ReceivedInputKind.TOOL_RESULT, name="refund", payload="ok", id="t1"),
    ]
    events = [
        e.model_dump(mode="json", by_alias=True) for e in input_events(received, action_prefix="p")
    ]
    value = "type.googleapis.com/google.protobuf.Value"
    assert events == [
        {
            "kind": "INPUT_EVENT_KIND_UPDATE",
            "name": "ask",
            "payloads": [{"@type": value, "value": {"text": "hi"}}],
            "updateId": "upd-1",
        },
        {
            "kind": "INPUT_EVENT_KIND_ACTIVITY_RESULT",
            "name": "p/refund/result",
            "payloads": [{"@type": value, "value": "ok"}],
            "updateId": "tool-result/t1",
        },
    ]


def test_script_parameter() -> None:
    facts = analyze_script(
        "for x in xs:\n    book(x)\nsearch()\n", {"book", "search", "unused"}
    )
    assert script_parameter(facts, {"unbounded_iteration": 7}) == {
        "calls": ["book", "search"],
        "uses": {
            "book": {"call_sites": 1, "in_loop": True, "concurrent": False, "indirect": False},
            "search": {"call_sites": 1, "in_loop": False, "concurrent": False, "indirect": False},
        },
        "has_while_loop": False,
        "dynamic_code": False,
        "classification": {"unbounded_iteration": 7},
    }
    assert "classification" not in script_parameter(facts)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

ENDPOINT = "dogwood-test-tool-policy"
POLICY_TASK_QUEUE = f"tool-policy-{uuid.uuid4()}"
PREFIX = "probe"

# The stand-in policy: maps each request to a response, and records what it was asked and
# what it was told the run received.
_POLICY: list[Callable[[dict[str, Any]], VerifyToolCallsResponse]] = []
_ASKED: list[dict[str, Any]] = []
_RECORDED: list[dict[str, Any]] = []


@nexusrpc.handler.service_handler(name="tool-policy")
class FakeToolPolicy:
    @nexusrpc.handler.sync_operation(name="VerifyToolCalls")
    async def verify(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> VerifyToolCallsResponse:
        _ASKED.append(request)
        return _POLICY[0](request)

    @nexusrpc.handler.sync_operation(name="RecordInputEvents")
    async def record(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> RecordInputEventsResponse:
        _RECORDED.append(request)
        return RecordInputEventsResponse()


def _answer(response: VerifyToolCallsResponse) -> None:
    _POLICY[:] = [lambda request: response]


def _fail() -> None:
    def fail(request: dict[str, Any]) -> VerifyToolCallsResponse:
        raise nexusrpc.HandlerError("policy store unreachable", type=nexusrpc.HandlerErrorType.BAD_REQUEST)

    _POLICY[:] = [fail]


@agent.activity_tool_defn()
async def refund(amount: int) -> str:
    """Refund a customer."""
    return f"refunded:{amount}"


@agent.tool_defn()
async def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order:{order_id}"


@agent.tool_defn()
async def cancel_order(order_id: str) -> str:
    """Cancel an order."""
    return f"cancelled:{order_id}"


_FEATURES = (
    ScriptFeature(key="unbounded_iteration", question="Unbounded?", yes="Yes.", no="No."),
)


@activity.defn(name=JEV_CLASSIFY_ACTIVITY)
async def fake_jev_classify(request: JevApprovalRequest) -> JevClassificationAnswer:
    return JevClassificationAnswer(
        model="jev-test", request_id="req", answers={"unbounded_iteration": 0.034}
    )


@agent.defn
class DogwoodProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=AutoApprovalCriteria(
                sets={"dogwood": AutoApprovalCriteriaSet(effect="Judged by the Dogwood policy.")},
                default="dogwood",
            ),
            auto_mode_evaluator=dogwood_evaluator(
                endpoint=ENDPOINT,
                action_prefix=PREFIX,
                script_classifier=jev_script_classifier(_FEATURES),
            ),
        )
        self._code_tool = agent.code_mode_tool([lookup_order, cancel_order], name="run_orders")

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund of the amount in the message."""
        try:
            result = await self._runner.run_tool("t1", refund, int(message.text))
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)

    @agent.accepts
    async def script(self, message: TextMessage) -> TextReply:
        """Run the script in the message."""
        try:
            result = await self._runner.run_tool("t1", self._code_tool, script=message.text)
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_extra_args=[
            "--dynamic-config-value",
            'system.system.refreshNexusEndpointsMinWait="0s"',
        ],
    )
    await env.create_nexus_endpoint(ENDPOINT, POLICY_TASK_QUEUE)
    yield env
    await env.shutdown()


async def _run(
    env: WorkflowEnvironment, message_type: str, text: str, *, until: str = AgentEventType.TURN_END
) -> list[Any]:
    """Send one message and collect turn events until ``until``."""
    _ASKED.clear()
    _RECORDED.clear()
    task_queue = f"dogwood-probe-{uuid.uuid4()}"
    # Workers start here rather than in the fixture, which runs on another event loop.
    async with Worker(
        env.client, task_queue=POLICY_TASK_QUEUE, nexus_service_handlers=[FakeToolPolicy()]
    ), Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DogwoodProbeAgent],
        activities=[agent.tool_activity(refund), fake_jev_classify],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            DogwoodProbeAgent.run,
            AgentConfig(),
            id=f"dogwood-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type=message_type, payload={"text": text}),
            result_type=AgentMessageReply,
        )
        events: list[Any] = []
        stream = WorkflowStreamClient.create(env.client, handle.id)
        async with asyncio.timeout(30):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                events.append(item.data.event)
                if item.data.event.type == until:
                    break
        await handle.terminate()
    return events


def _of(events: list[Any], event_type: str) -> list[Any]:
    return [e for e in events if e.type == event_type]


def _reply(events: list[Any]) -> str:
    return _of(events, AgentEventType.MESSAGE_HANDLER_END)[0].output["text"]


async def test_allowed_call_runs_unattended(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "act", "20")

    assert _reply(events) == "refunded:20"
    [request] = _ASKED
    assert request["caller"]["subject"] == "DogwoodProbeAgent"
    assert request["caller"]["workflowId"].startswith("dogwood-probe-")
    assert request["caller"]["runId"]
    assert request["toolCalls"] == [
        {
            "name": "probe/refund",
            "parameters": [
                {"@type": "type.googleapis.com/google.protobuf.Value", "value": {"amount": 20}}
            ],
        }
    ]
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.evaluator == "dogwood_evaluator"
    assert ended.verdict == AutoApprovalVerdict.APPROVE
    assert ended.details["action"] == "probe/refund"

    # Before asking, the verifier was told about the message that started the turn.
    [recorded] = _RECORDED
    assert recorded["caller"] == request["caller"]
    assert recorded["fromHistoryStart"] is True
    [message] = recorded["events"]
    assert message["kind"] == "INPUT_EVENT_KIND_UPDATE"
    assert message["name"] == "act"
    assert message["payloads"][0]["value"] == {"text": "20"}
    assert message["updateId"]


async def test_forbidden_call_is_denied(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_DENIED", "tool probe/refund denied by Dogwood policy refund-limit"))
    events = await _run(env, "act", "5000")
    assert _reply(events) == "denied:tool probe/refund denied by Dogwood policy refund-limit"
    assert not _of(events, AgentEventType.TOOL_START)


async def test_escalated_call_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(
        _response("DECISION_ESCALATE", "tool probe/refund escalated by Dogwood policy refund-review")
    )
    events = await _run(
        env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
    )
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict == AutoApprovalVerdict.ESCALATE
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


async def test_retry_later_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_RETRY_LATER", "rate limited"))
    events = await _run(env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "DECISION_RETRY_LATER" in error.message
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


async def test_policy_failure_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _fail()
    events = await _run(env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert error.evaluator == "dogwood_evaluator"
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


SCRIPT = """\
import asyncio
async def main():
    for order_id in ["A-1", "A-2"]:
        await cancel_order(order_id)
    return await lookup_order("A-1")
asyncio.run(main())
"""


async def test_script_is_judged_by_its_facts_and_host_calls_by_their_arguments(
    env: WorkflowEnvironment,
) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "script", SCRIPT)

    assert "order:A-1" in _reply(events)
    names = [request["toolCalls"][0]["name"] for request in _ASKED]
    assert names == [
        "probe/run_orders",
        "probe/cancel_order",
        "probe/cancel_order",
        "probe/lookup_order",
    ]
    script_value = _ASKED[0]["toolCalls"][0]["parameters"][0]["value"]
    assert script_value == {
        "calls": ["cancel_order", "lookup_order"],
        "uses": {
            "cancel_order": {"call_sites": 1, "in_loop": True, "concurrent": False, "indirect": False},
            "lookup_order": {"call_sites": 1, "in_loop": False, "concurrent": False, "indirect": False},
        },
        "has_while_loop": False,
        "dynamic_code": False,
        "classification": {"unbounded_iteration": 3},
    }
    assert _ASKED[1]["toolCalls"][0]["parameters"][0]["value"] == {"order_id": "A-1"}

    # Each evaluation first hands over only what arrived since the last one: the message before
    # the script is judged, then each host call's result before the next host call is.
    batches = [
        [(e["kind"], e["name"], e["payloads"][0]["value"]) for e in r["events"]] for r in _RECORDED
    ]
    result = "INPUT_EVENT_KIND_ACTIVITY_RESULT"
    assert batches == [
        [("INPUT_EVENT_KIND_UPDATE", "script", {"text": SCRIPT})],
        [(result, "probe/cancel_order/result", "cancelled:A-1")],
        [(result, "probe/cancel_order/result", "cancelled:A-2")],
    ]
    assert [r["fromHistoryStart"] for r in _RECORDED] == [True, False, False]


async def test_unparseable_script_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(
        env, "script", "def broken(:\n", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR
    )
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "syntax" in error.message.lower()
    assert _ASKED == []
