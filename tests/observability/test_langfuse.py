import asyncio
import unittest

from opentelemetry import baggage, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporal_agent_harness.langfuse import LangfuseExperimentPlugin, experiment_item


class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        LangfuseExperimentPlugin(self.provider)
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer(__name__)

    def tearDown(self):
        self.provider.shutdown()

    def item(self, item_id="one"):
        return experiment_item(
            experiment_id="experiment",
            name="test",
            dataset_id="dataset",
            item_id=item_id,
            expected_output={"secret_reference": "approve"},
            metadata={"curator_notes": "private"},
        )

    def test_native_root_and_descendants_keep_references_out_of_baggage(self):
        with self.item() as item:
            with self.tracer.start_as_current_span("setup"):
                self.assertIsNone(item.observation_id)
            with self.tracer.start_as_current_span(
                "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                root_id = item.observation_id
                with self.tracer.start_as_current_span(
                    "nested", attributes={"gen_ai.operation.name": "invoke_agent"}
                ):
                    self.assertEqual(item.observation_id, root_id)
                carrier = {}
                W3CBaggagePropagator().inject(carrier)
                self.assertNotIn("secret_reference", str(carrier))
                self.assertNotIn("curator_notes", str(carrier))
        root = next(s for s in self.exporter.get_finished_spans() if s.name == "agent")
        self.assertIsNone(root.parent)
        self.assertEqual(
            root.attributes["langfuse.experiment.item.root_observation_id"], root_id
        )
        self.assertIn(
            "secret_reference",
            root.attributes["langfuse.experiment.item.expected_output"],
        )
        child = next(
            s for s in self.exporter.get_finished_spans() if s.name == "nested"
        )
        self.assertEqual(
            child.attributes["langfuse.experiment.item.root_observation_id"], root_id
        )
        self.assertNotIn("langfuse.experiment.item.expected_output", child.attributes)
        self.assertEqual(item.trace_id, trace.format_trace_id(root.context.trace_id))

    async def test_concurrent_items_get_distinct_roots(self):
        async def run(item_id):
            with self.item(item_id) as item:
                await asyncio.sleep(0)
                with self.tracer.start_as_current_span(
                    "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
                ):
                    await asyncio.sleep(0)
                return item.observation_id

        ids = await asyncio.gather(run("one"), run("two"))
        self.assertEqual(len(set(ids)), 2)
        roots = self.exporter.get_finished_spans()
        self.assertEqual(
            {s.attributes["langfuse.experiment.item.id"] for s in roots}, {"one", "two"}
        )
        self.assertTrue(all(s.parent is None for s in roots))

    def test_exception_restores_context_and_leaves_ordinary_runs_untagged(self):
        with self.tracer.start_as_current_span("parent") as parent:
            with self.assertRaises(ValueError), self.item():
                with self.tracer.start_as_current_span(
                    "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
                ):
                    raise ValueError("synthetic failure")
            self.assertEqual(
                trace.get_current_span().get_span_context(), parent.get_span_context()
            )
            self.assertFalse(baggage.get_all())
        with self.tracer.start_as_current_span(
            "ordinary", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            pass
        ordinary = next(
            s for s in self.exporter.get_finished_spans() if s.name == "ordinary"
        )
        self.assertFalse(
            any(k.startswith("langfuse.experiment.") for k in ordinary.attributes)
        )
        failed = next(
            s for s in self.exporter.get_finished_spans() if s.name == "agent"
        )
        self.assertEqual(failed.status.status_code.name, "ERROR")
