"""Legacy memory limits over real temporary event logs, without model calls."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cater.events import Actor, EventLog
from cater.memory import shared_deliverable_memory


class MemoryBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log = EventLog(Path(temporary.name) / "memory.db")
        self.addCleanup(self.log.close)

    def publish(self, number, **changes):
        return self.log.emit(type="deliverable.published", actor=Actor.agent("dev", "developer"),
            task_id=f"task-{number}", payload={"title": f"Title {number}", "summary": f"Summary {number}",
                "kind": "text", "content": "Private body is not memory", **changes})

    def test_zero_limit_returns_no_items_even_with_published_deliverables(self):
        self.publish(1)
        self.assertEqual(shared_deliverable_memory(self.log.read(), limit=0), [])

    def test_invalid_limit_is_rejected_for_empty_and_nonempty_logs(self):
        for populated in (False, True):
            if populated:
                self.publish(1)
            for limit in (-1, -100, True, False, 1.0, 0.0, "3", None, [], {}):
                with self.subTest(populated=populated, limit=limit), self.assertRaises(ValueError):
                    shared_deliverable_memory(self.log.read(), limit=limit)

    def test_default_is_last_three_publications_in_original_order(self):
        events = [self.publish(number) for number in range(5)]
        memory = shared_deliverable_memory(self.log.read())
        self.assertEqual([row["seq"] for row in memory], [event.seq for event in events[-3:]])
        self.assertEqual([row["summary"] for row in memory], ["Summary 2", "Summary 3", "Summary 4"])

    def test_valid_positive_limit_and_exclusion_keep_legacy_selection(self):
        events = [self.publish(number) for number in range(5)]
        self.assertEqual(shared_deliverable_memory(self.log.read(), limit=1)[0]["seq"], events[-1].seq)
        memory = shared_deliverable_memory(self.log.read(), exclude_task_id="task-4", limit=2)
        self.assertEqual([row["seq"] for row in memory], [events[2].seq, events[3].seq])
        self.assertEqual(len(shared_deliverable_memory(self.log.read(), limit=99)), 5)

    def test_messages_private_bodies_and_empty_summaries_are_not_memory(self):
        self.log.emit(type="msg.said", actor=Actor.agent("dev", "developer"), task_id="task-1",
                      payload={"title": "Private claim", "summary": "Not a publication", "text": "Private message"})
        self.publish(1, title=" ")
        self.publish(2, summary="")
        published = self.publish(3)
        memory = shared_deliverable_memory(self.log.read())
        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0], {"seq": published.seq, "task_id": "task-3", "actor": "dev",
                                  "title": "Title 3", "summary": "Summary 3", "kind": "text"})

    def test_legacy_character_bounds_and_empty_input_are_unchanged(self):
        self.publish(1, title="x" * 200, summary="y" * 400, kind="z" * 100)
        row, = shared_deliverable_memory(self.log.read())
        self.assertEqual((len(row["title"]), len(row["summary"]), len(row["kind"])), (160, 320, 80))
        self.assertEqual(shared_deliverable_memory([]), [])
        self.assertEqual(shared_deliverable_memory([], limit=0), [])


if __name__ == "__main__":
    unittest.main()
