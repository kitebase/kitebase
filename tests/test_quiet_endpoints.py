"""An endpoint declared `quiet` (a poll) speaks at DEBUG when it succeeds, and
its access lines are dropped; a refusal is logged as any other."""
import logging

from kitebase.endpoints import CommandDispatcher, _ENDPOINTS, _QUIET, endpoint
from kitebase.errors import Invalid
from kitebase.server_utils import QuietAccess


def _register():
    @endpoint('poll_me', quiet=True)
    def poll_me(params):
        if params.get('fail'):
            raise Invalid('no')
        return 'ok'


def test_quiet_success_is_debug_and_refusal_is_warning(caplog):
    _register()
    dispatcher = CommandDispatcher()
    try:
        with caplog.at_level(logging.DEBUG, logger='kitebase'):
            dispatcher.send({'operation': 'poll_me'})
            dispatcher.send({'operation': 'poll_me', 'parameters': {'fail': True}})
    finally:
        _ENDPOINTS.pop('poll_me', None)
        _QUIET.discard('poll_me')
    levels = [r.levelno for r in caplog.records if 'poll_me' in r.getMessage()
              and '→' in r.getMessage()]
    assert levels == [logging.DEBUG, logging.WARNING]


def test_the_access_filter_drops_only_quiet_successes():
    _QUIET.add('poll_me')
    try:
        def record(line):
            return logging.LogRecord('uvicorn.access', logging.INFO, '', 0, line, None, None)
        f = QuietAccess()
        assert not f.filter(record('1.2.3.4:5 - "POST /kitebase/endpoint/poll_me HTTP/1.1" 200'))
        assert f.filter(record('1.2.3.4:5 - "POST /kitebase/endpoint/poll_me HTTP/1.1" 401'))
        assert f.filter(record('1.2.3.4:5 - "POST /kitebase/endpoint/db HTTP/1.1" 200'))
    finally:
        _QUIET.discard('poll_me')
