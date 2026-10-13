"""Auto mode for tool approvals: let a Dogwood tool policy, served over Nexus, decide.

``dogwood_evaluator(endpoint=...)`` returns an ``auto_mode_evaluator`` for
:class:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner` that asks the
``VerifyToolCalls`` operation of the temporal-untrusted-workers Dogwood verifier about each
gated call, after recording with ``RecordInputEvents`` the messages and tool results the run
received since it last asked, so a policy can judge a call by the run's history (see
:mod:`.evaluator`). Which tools may run, and with which arguments, lives in the policy files, so
the evaluator is configured per agent, never rewritten.

Code Mode scripts are judged by the facts
:func:`~temporal_agent_harness.harness.code_mode.analyze_script` reads off them, plus an optional
classifier's answers (``jev_script_classifier()`` is the builtin).
:func:`.schema.dogwood_schema` writes the action schema for an agent's tools and messages, so
the policy and what the evaluator sends can't drift apart.

Workflow-safe throughout, and no worker-side activity: the operations are called from the
workflow.
"""

from .evaluator import (
    DEFAULT_RECORD_OPERATION,
    DEFAULT_SERVICE,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_OPERATION,
    InputEvent,
    InputEventKind,
    RecordInputEventsRequest,
    VerifyCaller,
    VerifyToolCall,
    VerifyToolCallsRequest,
    dogwood_evaluator,
    decide,
    input_events,
    script_parameter,
)
from .toolpolicy_pb2 import Decision, RecordInputEventsResponse, VerifyToolCallsResponse

__all__ = [
    "DEFAULT_RECORD_OPERATION",
    "DEFAULT_SERVICE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_VERIFY_OPERATION",
    "Decision",
    "InputEvent",
    "InputEventKind",
    "RecordInputEventsRequest",
    "RecordInputEventsResponse",
    "VerifyCaller",
    "VerifyToolCall",
    "VerifyToolCallsRequest",
    "VerifyToolCallsResponse",
    "dogwood_evaluator",
    "decide",
    "input_events",
    "script_parameter",
]
