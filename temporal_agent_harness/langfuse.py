"""Link native agent spans to Langfuse experiments without SDK-specific hooks."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider
from temporalio.plugin import SimplePlugin
from temporalio.worker import (
    Interceptor,
    StartActivityInput,
    StartLocalActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

_ITEM_KEY = context.create_key("temporal-agent-harness-experiment-item")
_ROOT_KEY = "langfuse.experiment.item.root_observation_id"


@dataclass
class ExperimentItem:
    """The native agent observation captured within one experiment item scope."""

    attributes: dict[str, str]
    root_attributes: dict[str, str]
    span_context: trace.SpanContext | None = field(default=None, init=False)

    @property
    def trace_id(self) -> str | None:
        return (
            trace.format_trace_id(self.span_context.trace_id)
            if self.span_context
            else None
        )

    @property
    def observation_id(self) -> str | None:
        return (
            trace.format_span_id(self.span_context.span_id)
            if self.span_context
            else None
        )

    def propagation_attributes(self) -> dict[str, str]:
        attributes = dict(self.attributes)
        if self.observation_id:
            attributes[_ROOT_KEY] = self.observation_id
        return attributes


@contextmanager
def experiment_item(
    *,
    experiment_id: str,
    name: str,
    dataset_id: str,
    item_id: str,
    version: str | None = None,
    expected_output: Any = None,
    metadata: Mapping[str, Any] | None = None,
    experiment_metadata: Mapping[str, Any] | None = None,
) -> Iterator[ExperimentItem]:
    """Start an isolated trace scope; the first native agent span becomes its root.

    Expected outputs and curator metadata are span attributes only. They are never
    put in propagated baggage, which can reach external tool services.
    """
    attributes = {
        "langfuse.environment": "experiment",
        "langfuse.experiment.id": experiment_id,
        "langfuse.experiment.name": name,
        "langfuse.experiment.dataset.id": dataset_id,
        "langfuse.experiment.item.id": item_id,
    }
    if version:
        attributes["langfuse.experiment.item.version"] = version
    if experiment_metadata:
        attributes["langfuse.experiment.metadata"] = json.dumps(experiment_metadata)
    root_attributes = {}
    if expected_output is not None:
        root_attributes["langfuse.experiment.item.expected_output"] = json.dumps(
            expected_output
        )
    if metadata:
        root_attributes["langfuse.experiment.item.metadata"] = json.dumps(metadata)
    item = ExperimentItem(attributes, root_attributes)
    item_context = context.set_value(_ITEM_KEY, item, context.Context())
    for key, value in attributes.items():
        item_context = baggage.set_baggage(key, value, item_context)
    token = context.attach(item_context)
    try:
        yield item
    finally:
        context.detach(token)


class _ExperimentSpanProcessor(SpanProcessor):
    def on_start(
        self, span: Span, parent_context: context.Context | None = None
    ) -> None:
        inherited = {
            key: value
            for key, value in baggage.get_all(parent_context).items()
            if key.startswith("langfuse.experiment.") or key == "langfuse.environment"
        }
        span.set_attributes(inherited)
        item = context.get_value(_ITEM_KEY, parent_context)
        if not isinstance(item, ExperimentItem):
            return
        attributes = span.attributes or {}
        is_agent = (
            attributes.get("gen_ai.operation.name") == "invoke_agent"
            or attributes.get("langfuse.observation.type") == "AGENT"
        )
        if item.span_context is None and is_agent:
            item.span_context = span.get_span_context()
            span.set_attributes(item.root_attributes)
        if item.span_context:
            span.set_attributes(item.propagation_attributes())


@contextmanager
def _propagate_item() -> Iterator[None]:
    item = context.get_value(_ITEM_KEY)
    if not isinstance(item, ExperimentItem):
        yield
        return
    active = context.get_current()
    for key, value in item.propagation_attributes().items():
        active = baggage.set_baggage(key, value, active)
    token = context.attach(active)
    try:
        yield
    finally:
        context.detach(token)


class _ExperimentOutbound(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput):
        with _propagate_item():
            return super().start_activity(input)

    def start_local_activity(self, input: StartLocalActivityInput):
        with _propagate_item():
            return super().start_local_activity(input)


class _ExperimentInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_ExperimentOutbound(outbound))


class _ExperimentInterceptor(Interceptor):
    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput):
        return _ExperimentInbound


class LangfuseExperimentPlugin(SimplePlugin):
    """Enable experiment context once per Worker using native OTel agent spans.

    Register before OpenTelemetryPlugin so experiment baggage reaches its
    Activity-header propagation. Use Temporal's replay-safe tracer provider.
    """

    def __init__(self, tracer_provider: TracerProvider):
        tracer_provider.add_span_processor(_ExperimentSpanProcessor())
        super().__init__(
            "LangfuseExperimentPlugin", interceptors=[_ExperimentInterceptor()]
        )
