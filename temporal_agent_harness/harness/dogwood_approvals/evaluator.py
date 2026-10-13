"""``dogwood_evaluator`` — an auto mode evaluator that asks a Dogwood tool policy over Nexus.

The policy service is the ``tool-policy`` Nexus service of the temporal-untrusted-workers
Dogwood verifier (or any handler of the same contract). Dogwood is Cedar plus temporal
conditions over a history of events, so a policy can judge a call by what came before it in the
same run.

Before asking, the evaluator records what the run has received since it last asked
(``RecordInputEvents``), as history:

  * each message the agent accepted, as an update named after its handler (Dogwood's
    ``update/<handler>``);
  * each tool result, as ``<action_prefix>/<tool_name>/result``.

Then each gated call becomes one ``VerifyToolCalls`` tool call named
``<action_prefix>/<tool_name>``, with one ``google.protobuf.Value`` parameter, Dogwood's
``context.input.arg0``:

  * an ordinary tool call sends its model-facing arguments;
  * a Code Mode script sends :func:`script_parameter` — the
    :class:`~temporal_agent_harness.harness.code_mode.ScriptFacts` that
    :func:`~temporal_agent_harness.harness.code_mode.analyze_script` reads off the script, plus
    a classifier's answers when one is configured. A policy can't read a script; it can read
    those.

The host calls a script makes still reach the gate one by one, under their own names and with
their concrete arguments, so each is judged as an ordinary call.

Configuration is the endpoint and how calls are named. Which tools may run, with which
arguments, is the policy's business, so one evaluator serves every agent.

Verdicts follow the verifier's decision: approved approves, denied denies, escalate goes to a
human. Every failure (the operation failing or timing out, a verifier that can't decide right
now or answers no decision, a script that doesn't parse, a classifier error) raises, which the
harness turns into an escalation.

Workflow-safe: it runs in the workflow and calls the operation from there, so the worker needs
no activity for it, and the Nexus endpoint must allow the agent's namespace as a caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel
from pydantic_core import to_jsonable_python
from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalDecision,
    AutoApprovalVerdict,
    ReceivedInput,
    ReceivedInputKind,
)
from temporal_agent_harness.harness.agent_workflow import (
    AUTO_MODE_EVALUATOR_ATTR,
    AutoModeEvaluator,
)
from temporal_agent_harness.harness.code_mode.analysis import (
    ScriptClassifier,
    ScriptFacts,
    analyze_script,
)

with workflow.unsafe.imports_passed_through():
    # Importing registers the response messages with protobuf, which the SDK needs to decode
    # the operations' "json/protobuf" results. Without them the decode fails the workflow task
    # repeatedly instead of failing this evaluator.
    from .toolpolicy_pb2 import (
        DECISION_APPROVED,
        DECISION_DENIED,
        DECISION_ESCALATE,
        Decision,
        RecordInputEventsResponse,
        VerifyToolCallsResponse,
    )

DEFAULT_SERVICE = "tool-policy"
DEFAULT_VERIFY_OPERATION = "VerifyToolCalls"
DEFAULT_RECORD_OPERATION = "RecordInputEvents"
DEFAULT_TIMEOUT = timedelta(seconds=5)

_VALUE_TYPE_URL = "type.googleapis.com/google.protobuf.Value"


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class VerifyCaller(_Wire):
    """Who makes the tool calls: the agent's workflow."""

    namespace: str
    subject: str
    task_queue: str
    workflow_id: str
    run_id: str


class VerifyToolCall(_Wire):
    """One tool call, ``<prefix>/<tool_name>``, with its parameters as protobuf-JSON ``Any``s."""

    name: str
    parameters: list[dict[str, JsonValue]]


class VerifyToolCallsRequest(_Wire):
    """The ``VerifyToolCallsRequest`` of the tool-policy contract, in its JSON mapping."""

    caller: VerifyCaller
    tool_calls: list[VerifyToolCall]


class InputEventKind(StrEnum):
    """The ``InputEventKind`` values the evaluator records."""

    ACTIVITY_RESULT = "INPUT_EVENT_KIND_ACTIVITY_RESULT"
    UPDATE = "INPUT_EVENT_KIND_UPDATE"


class InputEvent(_Wire):
    """One thing the run received, as ``RecordInputEvents`` takes it.

    The verifier deduplicates an event by its update ID, whatever its kind. A tool result has
    no history event ID of its own to offer, so it carries ``tool-result/<tool call ID>`` there.
    """

    kind: InputEventKind
    name: str
    payloads: list[dict[str, JsonValue]]
    update_id: str


class RecordInputEventsRequest(_Wire):
    """The ``RecordInputEventsRequest`` of the tool-policy contract, in its JSON mapping."""

    caller: VerifyCaller
    events: list[InputEvent]
    from_history_start: bool


def dogwood_evaluator(
    *,
    endpoint: str,
    service: str = DEFAULT_SERVICE,
    verify_operation: str = DEFAULT_VERIFY_OPERATION,
    record_operation: str | None = DEFAULT_RECORD_OPERATION,
    action_prefix: str | None = None,
    subject: str | None = None,
    script_classifier: ScriptClassifier | None = None,
    timeout: timedelta = DEFAULT_TIMEOUT,
) -> AutoModeEvaluator:
    """Build an auto mode evaluator that asks the tool-policy service on ``endpoint``.

    Args:
        endpoint: The Nexus endpoint serving the tool policy.
        service: The Nexus service name.
        verify_operation: The operation that decides a tool call.
        record_operation: The operation that receives the run's input events, or ``None`` to
            record nothing, for a verifier without it.
        action_prefix: Tool calls are named ``<action_prefix>/<tool_name>``, the action a policy
            names, and tool results ``<action_prefix>/<tool_name>/result``. Defaults to the agent's task queue, which
            matches the temporal-untrusted-workers proxy's ``<taskQueue>/<activityType>``. Set
            it when the task queue varies by deployment, so the policy doesn't have to.
        subject: The caller's subject, the policy's ``principal``
            (``Temporal::Worker::"<subject>"``).
            Defaults to the agent's workflow type.
        script_classifier: Asked about each Code Mode script after
            :func:`~temporal_agent_harness.harness.code_mode.analyze_script`; its answers go to
            the policy as ``classification``. Omit to send the script's facts alone.
        timeout: How long to wait for each operation before escalating.
    """
    # How many of each run's received inputs the verifier has been given. Keyed by run, so an
    # evaluator shared by several runs keeps them apart.
    recorded: dict[str, int] = {}

    async def evaluate(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        info = workflow.info()
        prefix = action_prefix or info.task_queue
        caller = VerifyCaller(
            namespace=info.namespace,
            subject=subject or info.workflow_type,
            task_queue=info.task_queue,
            workflow_id=info.workflow_id,
            run_id=info.run_id,
        )
        if ctx.code_mode is None:
            # Tool arguments may be pydantic models; the policy sees their JSON.
            parameter: dict[str, JsonValue] = to_jsonable_python(ctx.tool_input)
        else:
            facts = analyze_script(ctx.code_mode.script, ctx.code_mode.host_functions)
            classification = (
                await script_classifier(ctx, facts) if script_classifier is not None else None
            )
            parameter = script_parameter(facts, classification)
        client = workflow.create_nexus_client(service=service, endpoint=endpoint)
        # Concurrent evaluations may send the same events; the verifier deduplicates them.
        start = recorded.get(info.run_id, 0)
        if record_operation is not None and len(ctx.received) > start:
            await client.execute_operation(
                record_operation,
                RecordInputEventsRequest(
                    caller=caller,
                    events=input_events(ctx.received[start:], action_prefix=prefix),
                    from_history_start=start == 0,
                ).model_dump(mode="json", by_alias=True),
                output_type=RecordInputEventsResponse,
                schedule_to_close_timeout=timeout,
                cancellation_type=workflow.NexusOperationCancellationType.ABANDON,
                summary=f"record {len(ctx.received) - start} input events",
            )
            recorded[info.run_id] = max(recorded.get(info.run_id, 0), len(ctx.received))
        call = VerifyToolCall(
            name=f"{prefix}/{ctx.tool_name}",
            parameters=[_value(parameter)],
        )
        request = VerifyToolCallsRequest(caller=caller, tool_calls=[call])
        response = await client.execute_operation(
            verify_operation,
            request.model_dump(mode="json", by_alias=True),
            output_type=VerifyToolCallsResponse,
            schedule_to_close_timeout=timeout,
            # The verifiers can't cancel a verification, and a superseded evaluation has
            # nothing left to wait for.
            cancellation_type=workflow.NexusOperationCancellationType.ABANDON,
            summary=call.name,
        )
        return decide(
            response,
            details={
                "endpoint": endpoint,
                "service": service,
                "action": call.name,
                "parameter": parameter,
                "criteria_set": ctx.criteria_set_name,
                "criteria_version": ctx.criteria_version,
            },
        )

    setattr(evaluate, AUTO_MODE_EVALUATOR_ATTR, "dogwood_evaluator")
    return evaluate


def input_events(received: Sequence[ReceivedInput], *, action_prefix: str) -> list[InputEvent]:
    """``received`` as ``RecordInputEvents`` events.

    A message is an update named after its handler, which Dogwood records as the action
    ``update/<handler>``. A tool result is an activity result named
    ``<action_prefix>/<tool_name>/result``: its own action, since a result's shape isn't the
    call's.
    """
    events: list[InputEvent] = []
    for item in received:
        if item.kind is ReceivedInputKind.MESSAGE:
            kind, name, update_id = InputEventKind.UPDATE, item.name, item.id
        else:
            kind = InputEventKind.ACTIVITY_RESULT
            name, update_id = f"{action_prefix}/{item.name}/result", f"tool-result/{item.id}"
        events.append(
            InputEvent(kind=kind, name=name, payloads=[_value(item.payload)], update_id=update_id)
        )
    return events


def _value(value: JsonValue) -> dict[str, JsonValue]:
    return {"@type": _VALUE_TYPE_URL, "value": value}


def script_parameter(
    facts: ScriptFacts, classification: Mapping[str, int] | None = None
) -> dict[str, JsonValue]:
    """What the policy sees for a Code Mode script: ``context.input.arg0``.

    ``calls`` repeats the keys of ``uses`` as a list, which the policy reads as a set, so it can
    ask ``["search_flights", ...].containsAll(context.input.arg0.calls)``.
    ``classification`` is left out when there is none, so a policy that needs it fails its
    ``has`` check.
    """
    calls: list[JsonValue] = [name for name in sorted(facts.uses)]
    uses: dict[str, JsonValue] = {
        name: use.model_dump(mode="json") for name, use in facts.uses.items()
    }
    parameter: dict[str, JsonValue] = {
        "calls": calls,
        "uses": uses,
        "has_while_loop": facts.has_while_loop,
        "dynamic_code": facts.dynamic_code,
    }
    if classification is not None:
        answers: dict[str, JsonValue] = dict(classification)
        parameter["classification"] = answers
    return parameter


def decide(
    response: VerifyToolCallsResponse, *, details: Mapping[str, Any] | None = None
) -> AutoApprovalDecision:
    """Map the policy's decision onto a verdict.

    Approved approves, denied denies, and escalate goes to a human: the verifier escalates a
    call when every policy that denied it is annotated ``@decision("escalate")``. Any other
    answer (retry later, no decision, a value this file doesn't know) raises, which also
    escalates, and publishes the evaluation as failed.
    """
    decision = (
        Decision.Name(response.decision)
        if response.decision in Decision.values()
        else str(response.decision)
    )
    if response.decision == DECISION_APPROVED:
        verdict = AutoApprovalVerdict.APPROVE
        reason = response.reason or "approved by the tool policy"
    elif response.decision == DECISION_DENIED:
        verdict = AutoApprovalVerdict.DENY
        reason = response.reason or "denied by the tool policy"
    elif response.decision == DECISION_ESCALATE:
        verdict = AutoApprovalVerdict.ESCALATE
        reason = response.reason or "the tool policy asks a person"
    else:
        raise RuntimeError(f"the tool policy did not decide ({decision}): {response.reason}")
    return AutoApprovalDecision(
        verdict=verdict,
        reason=reason,
        details={**(details or {}), "decision": decision, "policy_reason": response.reason},
    )
