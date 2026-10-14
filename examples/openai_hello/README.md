# Hello-world OpenAI Agents agent

The smallest interesting agent you can build on the harness with the **OpenAI Agents SDK**: it
chats in plain text and can call one tool (`get_weather`). It's here to show the harness
**streaming** path for the OpenAI integration end to end — from the model call, through the
streaming activity, to the live turn stream the web UI consumes.

## What it demonstrates

- **Streaming, not blocking.** The turn runs `Runner.run_streamed(...)` (not `Runner.run`).
  Only the streamed path routes model calls through `invoke_model_activity_streaming`, and it's
  that activity that feeds each raw OpenAI event to the harness observer.
- **The observer seam.** The worker builds the plugin with the two harness hooks:
  ```python
  OpenAIAgentsPlugin(
      model_params=ModelActivityParameters(stream_to_provider=stream_to_provider),
      observer_factory=harness_observer_factory,
  )
  ```
  `stream_to_provider` resolves the in-flight turn's stream context ambiently off the running
  workflow (no runner threading); `harness_observer_factory` turns it into the observer that
  translates raw OpenAI events into harness vocabulary — `model_interaction_started` →
  `reply_delta` … `tool_requested` … `model_interaction_ended`. There are **no run hooks**:
  the model-interaction bracket comes from the observer.
- **Harness-owned tools.** `get_weather` is a normal `@agent.tool_defn`, adapted onto the SDK
  with `as_openai_agent_tool(...)`, so the harness keeps approval + `tool_start`/`tool_end`.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `OpenAIHelloAgent` — the harness agent; one `ask` handler, one `get_weather` tool, driven by `Runner.run_streamed`. |
| `worker.py` | Worker hosting the workflow; wires the OpenAI plugin for the harness streaming seam, plus `AgentHarnessPlugin` for the harness's own converter and activities. |
| `agents.toml` | Supported legacy alternative to discovery, with the same identity as the decorator. |

There is **no per-example client**: like the Monty example, this agent is driven by the shared
example stack — the packaged `SessionManagerWorkflow` worker plus the FastAPI app and web UI
(`temporal-agent-harness serve`), which send messages and stream turns through the harness's built-in
`AgentClient`. `@agent.defn(agent_name="openai-hello", description=..., name="OpenAIHelloAgent",
key="openai-hello", task_queue="openai-hello", label="OpenAI Hello")` describes this agent for
`serve --discover examples.openai_hello`. The explicit overrides preserve its historical
Workflow type, key, queue and label. The Worker reads its default queue from
`agent.agent_definition(OpenAIHelloAgentWorkflow)`, so serving and Worker registration share
one definition. If `OPENAI_HELLO_TASK_QUEUE` changes, configure the serving registry to use that
queue too (see the main README's runtime deployment override example).

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your
Temporal connection profile). Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. serves the packaged UI + API on http://localhost:8000
just worker            # 4. the agent worker
```

`just server` serves the committed UI assets. After editing the UI, use `just app-build`
(with `pnpm` on your PATH) to rebuild them. For UI hot reload, `just ui-dev` runs Vite
with `/api` proxied to the server on :8000.

Open http://localhost:8000, pick **OpenAI Hello**, and chat — e.g. *"What's the weather in
Paris?"*. The reply streams in token by token and the `get_weather` tool call appears live, each
event produced by the observer translating a raw OpenAI event in the streaming activity.

Without `just`, the equivalent commands (from the repo root):

```sh
uv run --group examples temporal-agent-harness session-manager
uv run --group examples temporal-agent-harness serve --discover examples.openai_hello --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.openai_hello.worker
```


## Verify discovery and a live turn

Run from the repository root containing this version of the harness. The commands below use
`just` to load the root `.env.local`; bare `uv run` commands do not load that file automatically.
If `.env.local` does not exist, copy `.env.example` to it, then set `OPENAI_API_KEY`. For this
local smoke test use `TEMPORAL_CONFIG_FILE=temporal.local.toml` and `TEMPORAL_PROFILE=default`,
and leave `OPENAI_HELLO_TASK_QUEUE` unset so the Worker polls the decorator's queue.

First inspect the discovered registry without running Temporal or calling a model:

```sh
just --justfile examples/openai_hello/justfile verify-registry
```

The command imports the decorated class, prints its registry, and asserts that it matches the
retained TOML entry. Expect `workflow_type: OpenAIHelloAgent`, `key: openai-hello`,
`task_queue: openai-hello`, `label: OpenAI Hello`, and
`agent: examples.openai_hello.workflow:OpenAIHelloAgentWorkflow`.

For the live check, use a fresh in-memory Temporal dev server on separate ports. This gives the
session manager a new registry without replacing an existing manager or its sessions. Run each
block in its own terminal, from the same repository root:

```sh
# Terminal 1: fresh Temporal server, with its Web UI on http://localhost:8235
temporal server start-dev --port 7235 --ui-port 8235
```

```sh
# Terminal 2: packaged session-manager Worker
TEMPORAL_ADDRESS=localhost:7235 just --justfile examples/openai_hello/justfile session-manager
```

```sh
# Terminal 3: UI + API, discovering Python agent definitions
TEMPORAL_ADDRESS=localhost:7235 just --justfile examples/openai_hello/justfile server
```

```sh
# Terminal 4: agent Worker on the metadata-derived queue
TEMPORAL_ADDRESS=localhost:7235 just --justfile examples/openai_hello/justfile worker
```

Once the Workers and app are running, inspect the serving registry:

```sh
curl --fail --silent --show-error http://localhost:8000/api/agents | python3 -m json.tool
```

Expect the metadata listed above and `worker.status: ready`. This endpoint verifies both
registry discovery and a poller on the declared queue. If it reports `no_worker`, check the
Worker startup output for `address=localhost:7235` and `taskQueue=openai-hello`.

Open <http://localhost:8000>, select **OpenAI Hello**, create a new session and send
**What's the weather in Paris?** The prompt makes a real OpenAI model call; the weather tool
itself returns a canned result. Expect a streamed reply mentioning **72°F and sunny**, plus
`get_weather` tool events. Send a follow-up such as **Which city did I just ask about?** to
check that the session retains conversation state. The Temporal UI on
<http://localhost:8235> should show `session-manager` and the `OpenAIHelloAgent` child Workflow.

The API and UI must be available on port 8000 for this walkthrough. Stop these four foreground
processes with Ctrl-C when finished; this dev server's history is in memory.

The focused harness tests can also be run separately from the repository root:

```sh
uv run pytest tests/harness/test_agent_definition.py tests/web/test_agent_discovery.py tests/web/test_cli.py
```
