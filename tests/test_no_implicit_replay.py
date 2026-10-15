import json
import sys
import tempfile
import unittest
from pathlib import Path

from cater.drivers import CliDriver, DriverUnavailable
from cater.runtime import Runtime


class NoImplicitReplayTests(unittest.TestCase):
    def test_legacy_runtime_does_not_repeat_an_unknown_side_effect(self):
        class Driver:
            calls = 0

            def complete(self, **kwargs):
                self.calls += 1
                raise DriverUnavailable("response lost after execution")

        driver = Driver()
        with Runtime(drivers={"product": driver}, driver_attempts=8) as rt:
            task_id = rt.create_task("One invocation")
            role = rt._role_by_id("product")
            response = rt._complete_with_containment(task_id=task_id, task=rt.task(task_id),
                role=role, driver=driver, prompt="Act once")
            self.assertTrue(response.meta["invocation_started"])
            self.assertTrue(response.meta["contained"])
            self.assertEqual(driver.calls, 1)
            failures = list(rt.log.iter_events(task_id=task_id, event_types=("driver.failed",)))
            self.assertEqual([e.payload["attempt"] for e in failures], [1])

    def test_cli_crash_after_write_does_not_replay_and_next_request_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "calls.ndjson"
            code = ("import json,sys,os\n"
                    "for line in sys.stdin:\n"
                    " r=json.loads(line)\n"
                    " with open(sys.argv[1],'a',encoding='utf-8') as f: f.write(json.dumps(r)+'\\n')\n"
                    " if r.get('prompt')=='crash': os._exit(7)\n"
                    " print(json.dumps({'id':r['id'],'ok':True,'text':'completed'}),flush=True)\n")
            driver = CliDriver("crash", [sys.executable, "-u", "-c", code, str(record)],
                               timeout=10, restart_limit=5)
            try:
                with self.assertRaises(DriverUnavailable):
                    driver.complete(role="worker", prompt="crash", context={})
                self.assertEqual(len(record.read_text(encoding="utf-8").splitlines()), 1)
                self.assertEqual(driver.complete(role="worker", prompt="next", context={}).text, "completed")
                calls = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([r["prompt"] for r in calls], ["crash", "next"])
                self.assertNotEqual(calls[0]["id"], calls[1]["id"])
            finally:
                driver.close()


if __name__ == "__main__":
    unittest.main()
