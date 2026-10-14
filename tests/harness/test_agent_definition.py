"""Self-description is opt-in; Temporal registration stays compatible with legacy agents."""

from __future__ import annotations

import pytest
from temporalio import workflow

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.web.registry import agent_descriptor, registry_from_agents


@agent.defn
class BareAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None: ...


@agent.defn()
class EmptyArgsAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None: ...


@agent.defn(name="HistoricalWorkflowV1")
class LegacyNamedAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None: ...


@agent.defn(agent_name="code-review", description="Reviews code.")
class ActualPythonClass:
    @agent.init
    def __init__(self, config: AgentConfig) -> None: ...


def _shape():
    class Shape:
        @agent.init
        def __init__(self, config: AgentConfig) -> None: ...
    return Shape


@pytest.mark.parametrize("cls, expected", [
    (BareAgent, "BareAgent"),
    (EmptyArgsAgent, "EmptyArgsAgent"),
    (LegacyNamedAgent, "HistoricalWorkflowV1"),
])
def test_legacy_workflow_names_and_opt_in(cls, expected):
    assert workflow._Definition.must_from_class(cls).name == expected
    assert agent.agent_definition(cls) is None
    with pytest.raises(ValueError, match="needs @agent.defn"):
        agent_descriptor(cls)


def test_identity_and_import_target_come_from_the_decorated_class():
    metadata = agent.agent_definition(ActualPythonClass)
    assert isinstance(metadata, agent.AgentDefinition)
    assert metadata.agent_name == "code-review"
    assert metadata.key == metadata.task_queue == "code-review-agent"
    assert metadata.workflow_type == "CodeReviewAgent"
    assert metadata.label == "Code Review Agent"
    assert metadata.description == "Reviews code."
    assert metadata.agent == f"{__name__}:ActualPythonClass"
    assert workflow._Definition.must_from_class(ActualPythonClass).name == metadata.workflow_type
    descriptor = registry_from_agents([ActualPythonClass]).by_key(metadata.key)
    assert descriptor.agent == metadata.agent
    assert descriptor.workflow_type == metadata.workflow_type


@pytest.mark.parametrize("logical_name", [
    "code-review", "Code Review", "code_review_agent", "code-review-agent",
    "CodeReviewAgent", "CodeReviewAgentAgent", "code-review-agent-agent", "  CODE_REVIEW_AGENT  ",
])
def test_normalization_and_optional_agent_suffix(logical_name):
    cls = agent.defn(agent_name=logical_name, description="  Reviews\n code.  ")(_shape())
    metadata = agent.agent_definition(cls)
    assert metadata.agent_name == "code-review"
    assert metadata.key == metadata.task_queue == "code-review-agent"
    assert metadata.workflow_type == "CodeReviewAgent"
    assert metadata.label == "Code Review Agent"
    assert metadata.description == "Reviews code."


def test_multiword_defaults_and_deployment_overrides():
    cls = agent.defn(agent_name="pull request code review", description="Review.")(_shape())
    assert agent.agent_definition(cls).workflow_type == "PullRequestCodeReviewAgent"
    cls = agent.defn(
        agent_name="code-review", description="Review.", name="HistoricalWorkflowV1",
        key="review-prod", task_queue="shared-workers", label="Production Review",
        workflow_options=agent.WorkflowDefnOptions(sandboxed=False),
    )(_shape())
    metadata = agent.agent_definition(cls)
    assert (metadata.key, metadata.task_queue, metadata.label) == (
        "review-prod", "shared-workers", "Production Review",
    )
    definition = workflow._Definition.must_from_class(cls)
    assert definition.name == metadata.workflow_type == "HistoricalWorkflowV1"
    assert definition.sandboxed is False


@pytest.mark.parametrize("logical_name", ["", " ", "agent", "agent-agent", "../review", "1-review", "review.code", "réview", 42])
def test_invalid_logical_names_fail_at_definition(logical_name):
    with pytest.raises(ValueError, match="agent_name"):
        agent.defn(agent_name=logical_name, description="Review.")(_shape())


@pytest.mark.parametrize("override", [
    {"description": None}, {"description": " "}, {"key": " "}, {"key": "bad/key"},
    {"task_queue": ""}, {"label": " "}, {"name": ""},
])
def test_invalid_metadata_fails_at_definition(override):
    options = {"agent_name": "code-review", "description": "Review."} | override
    with pytest.raises(ValueError):
        agent.defn(**options)(_shape())


def test_registry_overrides_require_opt_in():
    with pytest.raises(ValueError, match="require agent_name"):
        agent.defn(description="Review.")


def test_undecorated_subclasses_do_not_inherit_registry_identity():
    class Child(ActualPythonClass):
        pass
    assert agent.agent_definition(Child) is None


def test_local_classes_are_not_importable_registry_targets():
    cls = agent.defn(agent_name="local", description="Local.")(_shape())
    with pytest.raises(ValueError, match="must be importable"):
        agent_descriptor(cls)


def test_explicit_class_conversion_rejects_duplicate_identities():
    with pytest.raises(ValueError, match="Duplicate agent key"):
        registry_from_agents([ActualPythonClass, ActualPythonClass])
