import unittest

from cater.runtime import Runtime
from test_work import WorkDriver, item


class DemoReplanningTests(unittest.TestCase):
    def test_local_demo_rebinds_changed_directions_instead_of_waiting_forever(self):
        with Runtime() as rt:
            task = rt.create_task("整理一个页面说明")
            rt.intake(task)
            rt.collaborate(task)
            previous = rt.work.view(task)
            rt.command(task, "内容里补上适用对象")
            rt.collaborate(task)
            current = rt.work.view(task)
            self.assertTrue(current["scope_current"])
            self.assertNotEqual(current["plan_seq"], previous["plan_seq"])
            self.assertEqual(current["planning"]["mode"], "local_demo")
            self.assertIn("内容里补上适用对象", str(current["customer_directions"]))

    def test_real_worker_without_planner_gets_actionable_missing_capability(self):
        driver = WorkDriver()
        with Runtime(drivers={"dev": driver, "test": driver}) as rt:
            task = rt.create_task("原任务")
            rt.plan_work(task, [item("draft")])
            rt.command(task, "补充新的客户范围")
            result = rt.collaborate(task)
            self.assertEqual(result["brief"]["checkpoint"]["state"], "planning_unavailable")
            self.assertIn("配置店长模型", result["brief"]["dispute"])
            self.assertEqual(driver.calls, [])
            rt.plan_work(task, [item("revised")])
            rt.collaborate(task)
            self.assertTrue(rt.work.view(task)["stage_accepted"])


if __name__ == "__main__":
    unittest.main()
