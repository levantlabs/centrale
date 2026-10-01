"""Hermetic clock/pane boundaries for lifecycle tests."""
from contextlib import contextmanager
from unittest import mock
import server


class ManualTimer:
    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.cancelled = False
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A callback may already have passed Timer.cancel's check.
        self.callback()


@contextmanager
def settled_codex_pane():
    """Mapping/serialization tests advance the stop clock on an idle pane.

    Race and timing tests use ManualTimer directly in test_orchestrator.
    """
    class SettledTimer(ManualTimer):
        def start(self):
            if self.interval == server.CODEX_STOP_SECONDS:
                self.fire()
    with mock.patch.object(server, '_agent_wait_timer', SettledTimer), \
         mock.patch.object(server, 'capture_session_pane', return_value=['› Ask Codex to do anything']):
        yield
