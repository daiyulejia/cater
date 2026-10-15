import json
import unittest

from cater.conversations import dialogue_background
from cater.events import Actor
import test_conversation


class DialogueBackgroundTests(unittest.TestCase):
    def test_large_frozen_background_does_not_block_a_short_message(self):
        fixture = test_conversation.ConversationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rt = fixture.rt
        snapshot = {"version": 1, "source_task_id": "parent", "source_seq": 1,
                    "source_hash": "frozen", "source_goal": "Old project",
                    "sources": [{"id": str(i), "seq": i, "task_id": "parent", "text": "旧要求" * 1000}
                                for i in range(20)],
                    "artifacts": [{"seq": 33, "task_id": "parent", "filename": "app.html", "content": "x" * 140000,
                                   "content_sha256": "immutable", "summary": "Large application"}]}
        child = "task_child"
        rt.log.emit(type="task.created", task_id=child, actor=Actor.human(),
                    payload={"goal": "New goal", "acceptance": ["Keep current scope"], "background": snapshot})
        fixture.service.receive(child, "现在可以聊吗")
        result = fixture.service.resolve_next(child)
        self.assertEqual(result["status"], "finished")
        call = fixture.driver.calls[-1]
        index = call["context"]["background"]
        self.assertEqual(index["representation"], "routing_index")
        self.assertEqual(index["omitted_sources"], 8)
        self.assertTrue(all(s["excerpt_truncated"] for s in index["sources"]))
        self.assertFalse(index["artifacts"][0]["content_included"])
        self.assertLess(len(call["prompt"].encode()), 131072)
        self.assertNotIn("x" * 1000, call["prompt"])
        self.assertEqual(rt.conversation_background(child), snapshot)

    def test_index_keeps_source_identities_with_explicit_omission(self):
        snapshot = {"source_hash": "anchor", "sources": [], "artifacts": [], "source_goal": "先前目标"}
        original = json.dumps(snapshot, sort_keys=True)
        result = dialogue_background(snapshot)
        self.assertEqual(result["source_hash"], "anchor")
        self.assertEqual(result["omitted_sources"], 0)
        self.assertIn("explain_task", result["notice"])
        self.assertEqual(json.dumps(snapshot, sort_keys=True), original)


if __name__ == "__main__":
    unittest.main()
