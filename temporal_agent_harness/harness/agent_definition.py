"""Import-time agent identity metadata; independent of web and Worker runtimes."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DEFINITION_ATTR = "__temporal_agent_definition"


@dataclass(frozen=True)
class AgentDefinition:
    """Identity shared by Worker registration, registry conversion, and UI/session routing.

    ``workflow_type`` is exactly the name registered by ``@workflow.defn``. ``agent`` is
    the decorated class's actual module and qualified name, never inferred from its key.
    """

    agent_name: str
    key: str
    workflow_type: str
    task_queue: str
    label: str
    description: str
    agent: str


def agent_definition(cls: type) -> AgentDefinition | None:
    """Read metadata declared on this class, or ``None`` for a legacy/unmarked class.

    Metadata is not inherited: an undecorated subclass is not a new registered agent.
    This is pure reflection and does not import modules, instantiate an agent, or do I/O.
    """
    return vars(cls).get(_DEFINITION_ATTR)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string.")
    return value.strip()


def _make_agent_definition(
    cls: type,
    *,
    agent_name: str,
    description: str | None,
    name: str | None,
    key: str | None,
    task_queue: str | None,
    label: str | None,
) -> AgentDefinition:
    logical_name = _nonempty(agent_name, "agent_name")
    logical_name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", logical_name)
    logical_name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", logical_name).lower()
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:[\s_-]+[a-z0-9]+)*", logical_name):
        raise ValueError("agent_name must contain ASCII words separated by spaces, '-' or '_'.")
    words = re.split(r"[\s_-]+", logical_name)
    # An optional suffix is semantic, not part of the base identity. Repeated suffixes
    # normalize the same way, so neither Workflow names nor labels can end in AgentAgent.
    while words and words[-1] == "agent":
        words.pop()
    if not words:
        raise ValueError("agent_name must contain a name before the 'agent' suffix.")
    logical_name = "-".join(words)
    resolved_key = _nonempty(key, "key") if key is not None else f"{logical_name}-agent"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", resolved_key):
        raise ValueError("key must contain only letters, digits, '.', '_' or '-'.")
    return AgentDefinition(
        agent_name=logical_name,
        key=resolved_key,
        workflow_type=(
            _nonempty(name, "name") if name is not None
            else "".join(word.capitalize() for word in words) + "Agent"
        ),
        task_queue=_nonempty(task_queue, "task_queue") if task_queue is not None else resolved_key,
        label=_nonempty(label, "label") if label is not None
        else " ".join(word.capitalize() for word in words) + " Agent",
        description=" ".join(_nonempty(description, "description").split()),
        agent=f"{cls.__module__}:{cls.__qualname__}",
    )
