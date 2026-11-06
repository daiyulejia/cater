"""Offline regressions for free-text routing and publication admission."""
import unittest
import threading
from unittest.mock import patch

from cater import artifact_store
from cater.events import Actor
from cater.runtime import Runtime
from test_conversation import DialogueDriver
import test_review_direction as fixtures


class CommandAndPublicationTests(unittest.TestCase):
    def test_other_connection_direction_wins_before_publication_transaction(self):
        helper = fixtures.ReviewDirectionTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        _, f = helper.setup_review()
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: run.call('publish_artifact', filename='race.txt',
            content='fixture', summary='fixture', format='text')]
        execute = f.rt.team._execute
        commands = []
        def inject(task_id, role, name, arguments, workspace, binding, tool_start, assignment, **kwargs):
            if name == 'publish_artifact':
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver}, close_drivers_on_close=False) as other:
                    commands.append(other.command(f.task, 'New objective', goal='Changed objective'))
            return execute(task_id, role, name, arguments, workspace, binding, tool_start, assignment, **kwargs)
        with patch.object(f.rt.team, '_execute', side_effect=inject):
            list(f.rt.team._run_message(f.task, *claimed))
        started, = [e for e in f.events('team.tool_started') if e.payload.get('run_id') == claimed[2].payload['run_id']]
        finished, = [e for e in f.events('team.tool_finished') if e.payload.get('started_seq') == started.seq]
        self.assertGreater(commands[0].seq, started.seq)
        self.assertEqual(finished.payload['result']['outcome'], 'not_started')
        self.assertFalse([e for e in f.events('deliverable.published') if e.payload.get('filename') == 'race.txt'])

    def test_concurrent_direction_commit_serializes_with_publication_event(self):
        helper = fixtures.ReviewDirectionTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        _, f = helper.setup_review()
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: run.call('publish_artifact', filename='race.txt',
            content='fixture', summary='fixture', format='text')]
        begun, finished, errors, commands = threading.Event(), threading.Event(), [], []
        def change():
            try:
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver}, close_drivers_on_close=False) as other:
                    begun.set()
                    commands.append(other.command(f.task, 'New objective', goal='Changed objective'))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()
        original = f.rt.team._publication_guard
        thread = threading.Thread(target=change)
        def inside(*args, **kwargs):
            original(*args, **kwargs)
            thread.start()
            self.assertTrue(begun.wait(5))
            self.assertFalse(finished.wait(.1), 'Direction write must wait for publication transaction')
        try:
            with patch.object(f.rt.team, '_publication_guard', side_effect=inside):
                list(f.rt.team._run_message(f.task, *claimed))
        finally:
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        published, = [e for e in f.events('deliverable.published') if e.payload.get('filename') == 'race.txt']
        self.assertGreater(commands[0].seq, published.seq)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_historical_events_keep_original_direction_interpretation(self):
        with Runtime() as rt:
            task = rt.create_task('fixture')
            event = rt.log.emit(type='human.commanded', task_id=task, actor=Actor.human(),
                                payload={'instruction': 'legacy', 'intent': 'direction'})
            self.assertEqual(rt.work.scope(task)['direction_seq'], event.seq)

    def test_temporary_unclassified_record_remains_visible_to_text_gateway(self):
        driver = DialogueDriver()
        with Runtime(drivers={driver.name: driver}) as rt:
            task = rt.create_task('fixture')
            original = rt.work.scope(task)['direction_seq']
            event = rt.log.emit(type='human.commanded', task_id=task, actor=Actor.human(),
                payload={'instruction': '请考虑补充一个需求', 'intent': 'unclassified',
                         'direction_semantics': 'structured_fields_v1', 'next_action': 'none'})
            rt._complete_with_containment(task_id=task, task=rt.task(task), role=rt.roles[0],
                                          driver=driver, prompt='fixture')
            self.assertEqual(rt.work.scope(task)['direction_seq'], original)
            self.assertIn(event.payload['instruction'], driver.calls[-1]['prompt'])

    def test_publish_scope_race_before_and_after_domain_commit(self):
        for when in ('before', 'after'):
            with self.subTest(when=when):
                helper = fixtures.ReviewDirectionTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                _, f = helper.setup_review()
                claimed = f.rt.team._claim(f.task)
                def script(run):
                    run.call('publish_artifact', filename='race.txt', content='checked fixture',
                             summary='fixture', format='text')
                f.driver.scripts[f.manager] = [script]
                guard = f.rt.team._publication_guard
                materialize = artifact_store.materialize_team_artifact
                def before(*args, **kwargs):
                    result = guard(*args, **kwargs)
                    f.rt.command(f.task, 'New objective', goal='Changed objective')
                    return result
                def after(*args, **kwargs):
                    f.rt.command(f.task, 'New objective', goal='Changed objective')
                    return materialize(*args, **kwargs)
                patcher = (patch.object(f.rt.team, '_publication_guard', side_effect=before) if when == 'before'
                           else patch('cater.artifact_store.materialize_team_artifact', side_effect=after))
                with patcher:
                    list(f.rt.team._run_message(f.task, *claimed))
                publications = [e for e in f.events('deliverable.published') if e.payload.get('filename') == 'race.txt']
                receipts = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'publish_artifact'
                            and e.payload.get('run_id') == claimed[2].payload['run_id']]
                self.assertEqual(len(receipts), 1)
                if when == 'before':
                    self.assertEqual(publications, [])
                    self.assertEqual(receipts[0].payload['result']['outcome'], 'not_started')
                else:
                    self.assertEqual(len(publications), 1)
                    self.assertEqual(receipts[0].payload['result']['status'], 'ok')
                    saved, = [e for e in f.events('team.artifact_saved') if e.payload.get('artifact_seq') == publications[0].seq]
                    self.assertTrue(saved.payload['path'])
                self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
