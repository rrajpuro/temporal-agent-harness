"""Write the Dogwood action schema for this example to ``policies/schema.cedarschema``.

Run from the repo root after changing the agent's tools (or `just schema` here):

    uv run python -m examples.auto_mode_dogwood.schema

The verifier validates ``policies/travel.dw`` against the schema at startup, so a policy that
names a tool or parameter the agent doesn't have fails there rather than silently never matching.
"""

from __future__ import annotations

from pathlib import Path

from temporal_agent_harness.harness.dogwood_approvals.schema import dogwood_schema
from temporal_agent_harness.harness.jev_approvals import DEFAULT_SCRIPT_FEATURES

from ..monty import activities
from .trip_form import ask_trip_details
from .workflow import (
    ACTION_PREFIX,
    SCRIPT_TOOLS,
    AutoModeDogwoodTravelAgentWorkflow,
    build_code_tool,
)

SCHEMA_FILE = Path(__file__).parent / "policies" / "schema.cedarschema"
HOST_FUNCTIONS = frozenset(tool.__name__ for tool in SCRIPT_TOOLS)


def render() -> str:
    """The schema: the script tool, the trip form, each travel tool its host calls reach the
    policy as, their results, and the agent's messages. The board tools are pre-approved and
    their results are read by no policy, so they are left out."""
    return dogwood_schema(
        [build_code_tool(), ask_trip_details, *activities.ALL_TOOLS],
        action_prefix=ACTION_PREFIX,
        script_features=DEFAULT_SCRIPT_FEATURES,
        agent_class=AutoModeDogwoodTravelAgentWorkflow,
    )


if __name__ == "__main__":
    SCHEMA_FILE.write_text(render())
    print(f"wrote {SCHEMA_FILE}")
