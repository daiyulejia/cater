"""A delivery archive must contain exactly the recorded reviewed versions."""

import io
import json
import unittest
import zipfile

from cater.events import Actor
from test_team_supervision import TeamSupervisionTests


class TeamDeliverySnapshotTests(TeamSupervisionTests):
    def finish_note(self):
        self.driver.steps[self.manager] = [
            self.native_turn('', ('publish_artifact', {'filename': 'note.txt', 'content': 'Delivered bytes',
                             'summary': 'A note', 'format': 'text'})),
            self.native_turn('', ('finish_work', {'summary': 'Note ready', 'verification': 'Text checked only'})),
            self.native_turn('Ready.'),
        ]
        self.rt.team.receive(self.task, 'Prepare a simple note', request_id='snapshot-note')
        self.run_team()
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        return self.events('team.objective_completed')[-1]

    def test_archive_uses_completed_snapshot_not_later_same_name_publication(self):
        delivered = self.finish_note()
        ref, = delivered.payload['artifacts']
        self.rt.log.emit(type='deliverable.published', task_id=self.task,
            actor=Actor.agent(self.manager, self.rt.organization.leader.role),
            payload={'filename': 'note.txt', 'content': 'Unaccepted later bytes', 'format': 'text',
                     'content_sha256': '0' * 64, 'summary': 'Later unrelated record', 'source': 'team'})
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read('note.txt'), b'Delivered bytes')
            manifest = json.loads(archive.read('__cater__/manifest.json'))
            self.assertEqual(manifest['files'][0]['seq'], ref['seq'])
            self.assertEqual(manifest['files'][0]['sha256'], ref['content_sha256'])

    def test_missing_snapshot_reference_is_not_silently_replaced(self):
        delivered = self.finish_note()
        self.rt.log.emit(type='team.objective_completed', task_id=self.task,
            actor=Actor.system('team'), payload={**delivered.payload, 'artifacts': [{
                **delivered.payload['artifacts'][0], 'seq': 999999}]})
        with self.assertRaises(ValueError):
            self.rt.team.archive(self.task)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamDeliverySnapshotTests(name)
        for name in TeamDeliverySnapshotTests.__dict__ if name.startswith('test_'))
