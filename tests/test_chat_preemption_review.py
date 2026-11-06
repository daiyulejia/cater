import unittest
import test_team_supervision

class PreemptionHandoffTest(unittest.TestCase):
    def test_direct_question_does_not_strand_interrupted_assignment(self):
        f = test_team_supervision.TeamSupervisionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        team = f.rt.team
        def interrupted(context, history, tools):
            team.receive(f.task, '@' + f.worker + ' What is your role?', request_id='preempt-question')
            return f.native_turn('obsolete response')
        def resume(context, history, tools):
            assignment, = [e for e in team._messages(f.task)
                           if e.actor.id == f.manager and e.payload.get('kind') == 'assignment']
            return f.native_turn('', ('continue_work', {'assignment_seq': assignment.seq,
                'instruction': 'Continue the interrupted assignment and return the result'}), ('wait_for_reply', {}))
        f.driver.steps = {
            f.manager: [f.native_turn('', ('delegate', {'recipient': f.worker, 'instruction': 'Prepare and return the result'}), ('wait_for_reply', {})),
                        resume, f.native_turn('Result received.')],
            f.worker: [interrupted, f.native_turn('I am the worker.'),
                       f.native_turn('', ('return_work', {'text': 'Result completed after resuming.'}))],
        }
        team.receive(f.task, 'Prepare the result', request_id='initial-request')
        f.run_team()
        messages = team._messages(f.task)
        assignment, = [e for e in messages if e.actor.id == f.manager and e.payload.get('kind') == 'assignment']
        self.assertTrue(any(e.actor.id == f.worker and e.payload.get('recipient') == f.manager
                            and e.payload.get('reply_to') == assignment.seq
                            and e.payload.get('kind') == 'progress' for e in messages),
                        'The manager was never informed that the assignment was interrupted')
        self.assertTrue(any(e.actor.id == f.worker and e.payload.get('kind') == 'report'
                            and e.payload.get('reply_to') == assignment.seq for e in messages))
        self.assertTrue(any(e.payload.get('text') == 'I am the worker.'
                            and e.payload.get('audience') == 'customer' for e in f.events('msg.said')))
        self.assertFalse(any(e.payload.get('text') == 'obsolete response' for e in f.events('msg.said')))

if __name__ == '__main__': unittest.main()
