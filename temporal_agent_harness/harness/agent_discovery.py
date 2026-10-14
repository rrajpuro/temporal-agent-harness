"""Scoped, deterministic source discovery of self-describing agent classes.

Only source modules with recognizable ``@agent.defn(agent_name=...)`` decorators are
imported. Discovery is an application-startup concern, never a Workflow operation.
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
from collections.abc import Iterable, Iterator
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path
from types import ModuleType

from temporal_agent_harness.harness.agent_definition import agent_definition


def _root_spec(root: str) -> ModuleSpec:
    if not isinstance(root, str) or not root or any(not part.isidentifier() for part in root.split(".")):
        raise ValueError(f"Expected a dotted Python module/package discovery root, got {root!r}.")
    # Resolve each component without importing parent package initializers merely to list
    # files. Include cwd like `python -m` and the schema CLI do for consumer projects.
    search_path: list[str] | None = [os.getcwd(), *sys.path]
    spec = None
    prefix = ""
    for part in root.split("."):
        prefix = f"{prefix}.{part}" if prefix else part
        # Use the local component name for filesystem lookup. A qualified namespace
        # spec asks sys.modules for its parent __path__, requiring an import; listing
        # sources must not execute parent initializers.
        spec = PathFinder.find_spec(part, search_path)
        if spec is None:
            raise ValueError(f"No module/package named {root!r}.")
        locations = spec.submodule_search_locations
        search_path = list(locations) if locations is not None else None
        if prefix != root and search_path is None:
            raise ValueError(f"{prefix!r} is not a package in discovery root {root!r}.")
    assert spec is not None
    return spec


def _has_agent_decorator(source: Path) -> bool:
    tree = ast.parse(source.read_bytes(), filename=str(source))
    namespaces: set[str] = set()
    decorators: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "temporal_agent_harness.harness":
                namespaces.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "agent"
                )
            elif node.module in (
                "temporal_agent_harness.harness.agent",
                "temporal_agent_harness.harness.agent_workflow",
            ):
                decorators.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "defn"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "temporal_agent_harness.harness.agent":
                    namespaces.add(alias.asname or alias.name)

    def dotted(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = dotted(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    def classes(nodes: Iterable[ast.stmt]) -> Iterator[ast.ClassDef]:
        # Module-level/nested classes only; factories and runtime launch functions are
        # not discovery sites. Temporal needs importable classes, not local classes.
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                yield node
                yield from classes(node.body)
            elif isinstance(node, (ast.If, ast.With, ast.Try)):
                yield from classes(node.body)
                yield from classes(getattr(node, "orelse", []))
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        yield from classes(handler.body)
                    yield from classes(node.finalbody)

    names = decorators | {f"{namespace}.defn" for namespace in namespaces}
    return any(
        isinstance(decorator, ast.Call)
        and dotted(decorator.func) in names
        and any(keyword.arg == "agent_name" for keyword in decorator.keywords)
        for cls in classes(tree.body) for decorator in cls.decorator_list
    )


def _module_agents(module: ModuleType) -> list[type]:
    found: dict[str, type] = {}
    visited: set[int] = set()

    def visit(values: Iterable[object]) -> None:
        for value in values:
            if (
                not isinstance(value, type)
                or value.__module__ != module.__name__
                or id(value) in visited
            ):
                continue
            visited.add(id(value))
            if agent_definition(value) is not None:
                found[value.__qualname__] = value
            visit(vars(value).values())

    visit(vars(module).values())
    return [found[name] for name in sorted(found)]


def discover_agent_classes(root: str) -> list[type]:
    """Discover self-describing classes below one importable package or source module.

    Scan only that root, in module/qualified-name order. Import only files whose AST has
    a direct harness ``agent.defn(agent_name=...)`` (including import aliases), then read
    metadata from the actual classes defined there. Re-exports are not additional agents.
    Legacy decorators and dynamic decorator factories require TOML/explicit registration.

    This is not a sandbox: selected modules, their dependencies and package initializers
    execute ordinary Python imports and MUST be free of startup/network side effects.
    Keep Workers, clients and launchers behind functions/main guards. Import errors fail
    discovery rather than silently publishing an incomplete registry.
    """
    spec = _root_spec(root)
    sources: dict[str, Path] = {}
    if spec.submodule_search_locations is None:
        if not spec.origin or not spec.origin.endswith(".py"):
            raise ValueError(f"Discovery root {root!r} must have Python source.")
        sources[root] = Path(spec.origin)
    else:
        for location in sorted(spec.submodule_search_locations):
            base = Path(location).resolve()
            for source in sorted(base.rglob("*.py")):
                if not source.resolve().is_relative_to(base):
                    continue
                parts = list(source.relative_to(base).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                if any(not part.isidentifier() or part == "__pycache__" for part in parts):
                    continue
                module_name = ".".join([root, *parts])
                if module_name in sources and sources[module_name] != source:
                    raise ValueError(f"Ambiguous discovery module {module_name!r}.")
                sources[module_name] = source
    if "" not in sys.path and os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    agents: list[type] = []
    for module_name, source in sorted(sources.items()):
        if not _has_agent_decorator(source):
            continue
        module = importlib.import_module(module_name)
        if not module.__file__ or Path(module.__file__).resolve() != source.resolve():
            raise ValueError(
                f"Discovery module {module_name!r} resolved outside its scanned source {source}."
            )
        agents.extend(_module_agents(module))
    if not agents:
        raise ValueError(f"No self-describing agents found in {root!r}.")
    return agents
