"""Task detail and download integration with disposable native team events."""
import io
import json
import unittest
from email.message import Message
from http.client import HTTPResponse
from unittest.mock import patch

from cater.runtime import Runtime
from cater.web import ActivityRunner, make_handler
from cater.review_delivery_stats import project_review_delivery_stats
import test_team_review_cycle as fixtures
from test_team_web import MemorySocket


class ReviewDeliveryStatsWebTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TeamReviewCycleTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        first = f.publish()
        req = f.request([first])['review_seq']
        f.record(req, 'changes_requested')
        second = f.publish('second version\n')
        req2 = f.request([second], review_seq=req)['review_seq']
        f.record(req2, 'approved')
        f.acknowledge_latest_review()
        f.finish()
        self.delivery = next(e.seq for e in f.rt.log.read(task_id=f.task) if e.type == 'team.objective_completed')
        self.runtimes = []
        def factory():
            rt = Runtime(f.root / 'test.db', roles_path=f.roles, drivers={'team_test': f.driver}, close_drivers_on_close=False)
            self.runtimes.append(rt)
            return rt
        self.runner = ActivityRunner(factory)
        with patch('cater.web.ActivityRunner', return_value=self.runner):
            self.handler = make_handler(f.root / 'test.db', runtime_factory=factory)

    def get(self, suffix=''):
        handler = self.handler.__new__(self.handler)
        handler.path = f'/api/tasks/{self.f.task}{suffix}'
        handler.command, handler.request_version = 'GET', 'HTTP/1.1'
        handler.requestline = f'GET {handler.path} HTTP/1.1'
        handler.headers = Message()
        handler.headers['Host'] = 'localhost'
        handler.rfile, handler.wfile = io.BytesIO(), io.BytesIO()
        handler.do_GET()
        response = HTTPResponse(MemorySocket(handler.wfile.getvalue()))
        response.begin()
        body = response.read()
        self.assertEqual(response.status, 200, body)
        return body

    def test_detail_projects_only_safe_counts_and_download_still_works(self):
        seen = []
        def project(events, **kwargs):
            self.assertTrue(self.runtimes[-1].log.db.in_transaction)
            seen.append((list(events), kwargs))
            return project_review_delivery_stats(events, **kwargs)
        with patch('cater.review_delivery_stats.project_review_delivery_stats', side_effect=project):
            detail = json.loads(self.get())
        self.assertEqual(detail['review_delivery_stats'], {
            'delivery_seq': self.delivery, 'complete': True, 'review_conclusions': 2,
            'changes_requested': 1, 'artifact_set_change_resubmissions': 1})
        self.assertEqual([e.seq for e in seen[0][0]], [e['seq'] for e in detail['events']])
        self.assertEqual(seen[0][1]['task_id'], self.f.task)
        self.assertTrue(self.get(f'/delivery?delivery_seq={self.delivery}').startswith(b'PK'))

    def test_projection_failure_or_incomplete_is_redacted_and_does_not_block_zip(self):
        for outcome in (RuntimeError('secret path token'), {'delivery_seq': self.delivery, 'complete': False, 'chains': ['secret']},
                        {'delivery_seq': self.delivery, 'complete': True, 'review_conclusions': True,
                         'changes_requested': 1, 'artifact_set_change_resubmissions': 1}):
            with self.subTest(outcome=type(outcome).__name__):
                with patch('cater.review_delivery_stats.project_review_delivery_stats', side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome):
                    detail = json.loads(self.get())
                    self.assertEqual(detail['review_delivery_stats'], {'delivery_seq': self.delivery, 'complete': False})
                    self.assertTrue(self.get(f'/delivery?delivery_seq={self.delivery}').startswith(b'PK'))


if __name__ == '__main__':
    unittest.main()
