"""Resource owner for the two Web tests using a nested team fixture."""
from functools import partial
import tempfile

from test_runtime import LiveTestThreadTimeout
from test_team_review_cycle import TeamReviewCycleTests


def _has_live_thread(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, LiveTestThreadTimeout):
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend((current.__cause__, current.__context__))
    return False


class NestedTeamWebResources:
    def __init__(self):
        self.directory = None
        self.callbacks = []

    def temporary_directory(self):
        if self.directory is not None:
            raise RuntimeError('nested fixture already owns a temporary directory')
        self.directory = tempfile.TemporaryDirectory()
        # The nested fixture registers cleanup; this owner deletes it last.
        class Lease:
            name = self.directory.name

            def cleanup(self):
                pass
        return Lease()

    def callback(self, function, *args, **kwargs):
        self.callbacks.append(partial(function, *args, **kwargs))

    def close(self):
        failures = []
        while self.callbacks:
            try:
                self.callbacks.pop()()
            except BaseException as error:
                failures.append(error)
        directory, self.directory = self.directory, None
        if directory is not None:
            if any(_has_live_thread(error) for error in failures):
                directory._finalizer.detach()
                for error in failures:
                    if _has_live_thread(error):
                        error.add_note(f'Test temporary directory retained while a thread may be alive: {directory.name}')
            else:
                try:
                    directory.cleanup()
                except BaseException as error:
                    directory._finalizer.detach()
                    error.add_note(f'Test temporary directory cleanup failed; remaining files retained: {directory.name}')
                    failures.append(error)
        if failures:
            raise BaseExceptionGroup('nested team Web cleanup failed', failures)


class NestedTeamFixture(TeamReviewCycleTests):
    def __init__(self, resources):
        super().__init__()
        self.resources = resources

    def _team_temporary_directory(self):
        return self.resources.temporary_directory()

    def addCleanup(self, function, /, *args, **kwargs):
        self.resources.callback(function, *args, **kwargs)
