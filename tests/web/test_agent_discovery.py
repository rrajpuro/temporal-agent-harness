"""Scoped discovery, registry conversion and existing app/session consumers."""

from __future__ import annotations

import importlib
import sys
from dataclasses import replace

import pytest
from temporalio import workflow

from temporal_agent_harness.harness.agent_discovery import discover_agent_classes
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.harness.agent_schema import agent_schema, load_agent_class
from temporal_agent_harness.web.agent_starts import resolve_agent_starts
from temporal_agent_harness.web.app import _resolve_registry
from temporal_agent_harness.web.registry import discover_agent_registry, load_agent_registry, load_agent_registries
from temporal_agent_harness.web.session_manager import AgentRegistry, CreateSessionRequest, SessionManagerWorkflow

_HEADER = """
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from pydantic import BaseModel
class Brief(BaseModel):
    topic: str
"""


def _source(name, class_name="Implementation", **overrides):
    options = {"agent_name": name, "description": "Describes the agent."} | overrides
    args = ", ".join(f"{key}={value!r}" for key, value in options.items())
    return _HEADER + f"""
@agent.defn({args})
class {class_name}:
    @agent.init
    def __init__(self, config: AgentConfig, data: Brief) -> None: ...
    @agent.accepts
    async def review(self, message: Brief) -> Brief:
        return message
"""


@pytest.fixture
def package(tmp_path, monkeypatch):
    name = "discovery_fixture"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield root, name
    for module_name in list(sys.modules):
        if module_name == name or module_name.startswith(name + "."):
            sys.modules.pop(module_name)
    importlib.invalidate_caches()


def test_package_discovery_is_scoped_ordered_and_skips_launchers(package):
    root, name = package
    (root / "z_review.py").write_text(_source("code-review", "DifferentPythonName"))
    (root / "a_plan.py").write_text(_source("travel-planner"))
    (root / "worker.py").write_text("raise AssertionError('launcher imported')")
    (root / "server.py").write_text("raise AssertionError('server imported')")
    unrelated = root.parent / "unrelated.py"
    unrelated.write_text(_source("unrelated") + "\nraise AssertionError('unrelated imported')")
    registry = discover_agent_registry(name)
    assert [item.key for item in registry.agents] == ["travel-planner-agent", "code-review-agent"]
    assert registry.agents[1].agent == f"{name}.z_review:DifferentPythonName"
    assert f"{name}.worker" not in sys.modules
    assert f"{name}.server" not in sys.modules
    assert "unrelated" not in sys.modules
    assert discover_agent_registry(name) == registry


def test_module_root_actual_import_target_and_schema(package):
    root, name = package
    (root / "code_review.py").write_text(_source("code-review", "CodeReviewAgentWorkflow"))
    registry = _resolve_registry(None, None, f"{name}.code_review")
    descriptor = registry.agents[0]
    assert descriptor.agent == f"{name}.code_review:CodeReviewAgentWorkflow"
    cls = load_agent_class(descriptor.agent)
    assert discover_agent_classes(f"{name}.code_review") == [cls]
    assert workflow._Definition.must_from_class(cls).name == descriptor.workflow_type
    schema = agent_schema(cls)
    assert schema["agent"] == descriptor.workflow_type
    assert schema["source"] == descriptor.agent
    assert "review" in schema["handlers"]
    starts = resolve_agent_starts(registry)
    assert starts[descriptor.workflow_type].init_data.required is True
    assert "topic" in starts[descriptor.workflow_type].init_data_schema["schema"]["properties"]


@pytest.mark.parametrize("import_statement, decorator", [
    ("from temporal_agent_harness.harness import agent as author", "author.defn"),
    ("import temporal_agent_harness.harness.agent as author", "author.defn"),
    ("from temporal_agent_harness.harness.agent import defn as define", "define"),
])
def test_direct_import_aliases_are_recognized(package, import_statement, decorator):
    root, name = package
    text = _source("code-review").replace("@agent.defn(", f"@{decorator}(")
    (root / "definition.py").write_text(import_statement + "\n" + text)
    assert discover_agent_registry(name).agents[0].key == "code-review-agent"


def test_nested_qualified_name_and_reexports(package):
    root, name = package
    text = _source("code-review")
    header, body = text.split("@agent.defn", 1)
    nested = header + "class Container:\n" + "\n".join(
        "    " + line for line in ("@agent.defn" + body).splitlines()
    )
    (root / "nested.py").write_text(nested)
    (root / "reexport.py").write_text("from .nested import Container")
    descriptor = discover_agent_registry(name).agents[0]
    assert descriptor.agent == f"{name}.nested:Container.Implementation"
    assert load_agent_class(descriptor.agent).__qualname__ == "Container.Implementation"
    assert f"{name}.reexport" not in sys.modules


def test_namespace_packages_and_subpackages(package):
    root, name = package
    (root / "__init__.py").unlink()
    sub = root / "reviews"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "workflow.py").write_text(_source("code-review"))
    assert discover_agent_registry(name).agents[0].agent == f"{name}.reviews.workflow:Implementation"


def test_legacy_agents_remain_toml_only(package):
    root, name = package
    text = _source("code-review").replace(
        "agent_name='code-review', description='Describes the agent.'", "name='HistoricalType'"
    )
    (root / "legacy.py").write_text(text)
    with pytest.raises(ValueError, match="No self-describing agents"):
        discover_agent_registry(name)
    assert f"{name}.legacy" not in sys.modules


def test_definition_import_failures_are_not_silently_skipped(package):
    root, name = package
    (root / "broken.py").write_text(_source("code-review") + "\nraise RuntimeError('bad dependency')")
    with pytest.raises(RuntimeError, match="bad dependency"):
        discover_agent_registry(name)


@pytest.mark.parametrize("second", [
    {"agent_name": "code_review_agent"},
    {"agent_name": "other", "key": "code-review-agent"},
    {"agent_name": "other", "name": "CodeReviewAgent"},
])
def test_discovery_rejects_duplicate_identities(package, second):
    root, name = package
    (root / "one.py").write_text(_source("code-review"))
    second = dict(second)
    logical_name = second.pop("agent_name")
    (root / "two.py").write_text(_source(logical_name, **second))
    with pytest.raises(ValueError, match="Duplicate agent"):
        discover_agent_registry(name)


def test_shared_queues_are_allowed_but_repeated_class_targets_are_not(package):
    root, name = package
    (root / "one.py").write_text(_source("one", task_queue="shared"))
    (root / "two.py").write_text(_source("two", task_queue="shared"))
    registry = discover_agent_registry(name)
    assert {item.task_queue for item in registry.agents} == {"shared"}
    descriptor = registry.agents[0]
    with pytest.raises(ValueError, match="Duplicate agent agent"):
        AgentRegistry(agents=[descriptor, replace(descriptor, key="other", workflow_type="Other")])


def test_old_toml_single_and_merged_loading_are_preserved(tmp_path):
    one, two = tmp_path / "one.toml", tmp_path / "two.toml"
    template = """[[agents]]
key = "{key}"
workflow_type = "{kind}"
task_queue = "legacy-queue"
label = "Legacy Label"
description = "Legacy description."
"""
    one.write_text(template.format(key="legacy-one", kind="HistoricalTypeOne"))
    two.write_text(template.format(key="legacy-two", kind="HistoricalTypeTwo"))
    single = load_agent_registry(one)
    assert single.agents[0].workflow_type == "HistoricalTypeOne"
    assert single.agents[0].agent is None
    assert load_agent_registries([one, two]).agents == single.agents + load_agent_registry(two).agents
    two.write_text(template.format(key="legacy-two", kind="HistoricalTypeOne"))
    with pytest.raises(ValueError, match="Duplicate agent workflow_type"):
        load_agent_registries([one, two])
    one.write_text(template.format(key="legacy-one", kind="HistoricalTypeOne") + template.format(key="legacy-two", kind="HistoricalTypeOne"))
    with pytest.raises(ValueError, match="Duplicate agent workflow_type"):
        load_agent_registry(one)


@pytest.mark.parametrize("root", ["", "bad-root", "../agents", "missing_package_for_discovery", 42])
def test_invalid_or_missing_roots_report_errors(root):
    with pytest.raises(ValueError):
        discover_agent_registry(root)


def test_app_rejects_multiple_registry_sources():
    with pytest.raises(ValueError, match="Pass only one"):
        _resolve_registry(AgentRegistry(), None, "agents")


async def test_session_manager_routes_the_derived_type_and_queue(package, monkeypatch):
    root, name = package
    (root / "workflow.py").write_text(_source("code-review", task_queue="deployment-queue"))
    registry = discover_agent_registry(name)
    descriptor = registry.agents[0]
    calls = []
    async def fake_start(workflow_type, **kwargs):
        calls.append((workflow_type, kwargs))
    monkeypatch.setattr(workflow, "start_child_workflow", fake_start)
    monkeypatch.setattr(workflow, "time", lambda: 0.0)
    manager = SessionManagerWorkflow(registry)
    config = AgentConfig()
    await manager.create_session(CreateSessionRequest(
        agent_workflow_type=descriptor.workflow_type, config=config,
        data={"topic": "Changes"}, session_id="test-session",
    ))
    assert manager.available_agents() == registry
    assert calls == [("CodeReviewAgent", {
        "args": [config, {"topic": "Changes"}], "id": "test-session", "task_queue": "deployment-queue",
    })]


def test_nested_namespace_root_does_not_need_preimported_parents(package):
    root, name = package
    (root / "__init__.py").unlink()
    sub = root / "definitions"
    sub.mkdir()
    (sub / "workflow.py").write_text(_source("code-review"))
    assert name not in sys.modules
    assert discover_agent_registry(f"{name}.definitions").agents[0].key == "code-review-agent"


def test_empty_root_does_not_import_package_initializer(package):
    root, name = package
    (root / "__init__.py").write_text("raise AssertionError('initializer executed')")
    with pytest.raises(ValueError, match="No self-describing agents"):
        discover_agent_registry(name)
    assert name not in sys.modules


def test_serve_app_passes_the_derived_registry_to_builtin_app(package, monkeypatch):
    from temporal_agent_harness.web.serve import create_app
    root, name = package
    (root / "workflow.py").write_text(_source("code-review"))
    captured = {}
    def fake_create(**kwargs):
        captured.update(kwargs)
        return "app"
    monkeypatch.setattr("temporal_agent_harness.web.create_agent_harness_app", fake_create)
    assert create_app(discovery_root=name) == "app"
    assert captured["registry"].agents[0].workflow_type == "CodeReviewAgent"
    with pytest.raises(ValueError, match="not both"):
        create_app("agents.toml", discovery_root=name)


async def test_embedded_app_discovers_before_connecting_and_passes_same_registry(package, monkeypatch):
    from temporal_agent_harness.web.app import create_agent_harness_app
    root, name = package
    (root / "workflow.py").write_text(_source("code-review"))
    captured = {}
    async def fake_connect(**kwargs):
        assert f"{name}.workflow" in sys.modules
        return object()
    async def fake_ensure(client, **kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr("temporal_agent_harness.web.app.Client.connect", fake_connect)
    monkeypatch.setattr("temporal_agent_harness.web.app.ClientConfig.load_client_connect_config", lambda: {})
    monkeypatch.setattr("temporal_agent_harness.web.app._ensure_session_manager_workflow", fake_ensure)
    app = create_agent_harness_app(discovery_root=name)
    async with app.router.lifespan_context(app):
        registry = captured["registry"]
        assert registry.agents[0].key == "code-review-agent"
        assert set(app.state.agent_starts) == {registry.agents[0].workflow_type}
        assert app.state.agent_starts[registry.agents[0].workflow_type].init_data.required
