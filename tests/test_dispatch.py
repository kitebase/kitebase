"""Tests for the command dispatcher — CommandProcessor.send().

Every request of every server goes through here, and until now nothing covered
it: the existing tests call endpoint functions directly. These fix the observable
behaviour — what a caller passes in and what comes back — so the machinery
underneath can be changed without changing what the servers see.

The context tests earn their place: the dispatcher used to run each command in a
brand-new thread, so thread-local state started empty by construction. Serving
requests from a reused pool thread removes that guarantee, and only an explicit
set on every dispatch keeps one user's context out of the next user's request.
"""
import logging
import pytest

from kitebase.db import BaseApp
from kitebase.endpoints import CommandProcessor, endpoint, _ENDPOINTS


@pytest.fixture
def processor():
    """A processor with a few endpoints registered, cleaned up afterwards."""
    registered = []

    def register(name, func):
        _ENDPOINTS[name] = func
        registered.append(name)

    register('echo', lambda params: {'seen': params})
    register('boom', _raise)
    register('shaped', lambda params: {'status': 'error', 'message': 'nope', 'code': 422})
    register('context', lambda params: BaseApp.get_context())

    cp = CommandProcessor()
    cp.endpoints = dict(_ENDPOINTS)
    yield cp

    for name in registered:
        _ENDPOINTS.pop(name, None)
    BaseApp.set_context({})


def _raise(params):
    raise KeyError('missing thing')


def test_result_wraps_the_return_value(processor):
    result = processor.send({'operation': 'echo', 'parameters': {'a': 1}})

    assert result['status'] == 'success'
    assert result['code'] == 200
    assert result['data'] == {'seen': {'a': 1}}


def test_a_shaped_dict_is_passed_through(processor):
    """An endpoint may return its own status/code instead of a plain payload."""
    result = processor.send({'operation': 'shaped'})

    assert result['status'] == 'error'
    assert result['code'] == 422
    assert result['message'] == 'nope'


def test_a_refusal_keeps_what_it_has_to_say(processor):
    """The field errors of a form travel in `data`, on an error as on a success."""
    _ENDPOINTS['refuse'] = lambda params: {
        'status': 'error', 'code': 400, 'message': 'not changed',
        'data': {'errors': {'confirm': 'the two do not match'}}}
    processor.endpoints['refuse'] = _ENDPOINTS['refuse']
    try:
        result = processor.send({'operation': 'refuse', 'parameters': {}})
    finally:
        _ENDPOINTS.pop('refuse', None)

    assert result['status'] == 'error'
    assert result['data'] == {'errors': {'confirm': 'the two do not match'}}


def test_unknown_operation_is_a_404(processor):
    result = processor.send({'operation': 'nowhere'})

    assert result['status'] == 'error'
    assert result['code'] == 404
    assert 'nowhere' in result['message']


def test_an_exception_becomes_a_500_with_its_type(processor):
    result = processor.send({'operation': 'boom'})

    assert result['status'] == 'error'
    assert result['code'] == 500
    assert result['error_type'] == 'KeyError'
    assert 'missing thing' in result['message']


def test_request_id_is_echoed_and_generated(processor):
    given = processor.send({'operation': 'echo', 'request_id': 'abc-123'})
    assert given['request_id'] == 'abc-123'

    auto = processor.send({'operation': 'echo'})
    assert auto['request_id'] and auto['request_id'] != 'abc-123'


def test_the_context_is_visible_to_the_endpoint(processor):
    result = processor.send({'operation': 'context', 'context': {'id': 7, 'username': 'ada'}})

    assert result['data'] == {'id': 7, 'username': 'ada'}


def test_a_command_without_context_does_not_inherit_the_previous_one(processor):
    """The guarantee that a pooled, reused thread makes load-bearing."""
    processor.send({'operation': 'context', 'context': {'id': 7, 'username': 'ada'}})

    result = processor.send({'operation': 'context'})

    assert result['data'] == {}, "context leaked from the previous command"


def test_results_are_not_retained(processor):
    """Nothing may accumulate per request: a dispatcher is not a cache."""
    for _ in range(50):
        processor.send({'operation': 'echo'})

    leftovers = [name for name in ('results', 'pending_commands') if getattr(processor, name, None)]
    assert not leftovers, f"the dispatcher kept state in {leftovers}"


def test_endpoint_decorator_registers_a_dispatchable_operation(processor):
    """Note the decorator registers the function and returns a wrapper, so this
    asserts on behaviour rather than on identity."""
    @endpoint('decorated')
    def _decorated(params):
        return 'ok'

    try:
        processor.endpoints = dict(_ENDPOINTS)
        assert processor.send({'operation': 'decorated'})['data'] == 'ok'
    finally:
        _ENDPOINTS.pop('decorated', None)


# ── What the dispatcher says ───────────────────────────────────────────────
# The client shows an error once and the dialog closes; the log is what
# remains. One line per request, the traceback on a failure, and the request
# id on both so what a person reports can be found again. Parameter values
# never: a `db update` on a user carries the password.

def test_a_request_leaves_one_line_with_who_and_how_long(processor, caplog):
    with caplog.at_level(logging.INFO, logger='kitebase'):
        result = processor.send({'operation': 'echo', 'parameters': {'a': 1},
                                 'context': {'username': 'rossi'}, 'request_id': 'req-1'})
    lines = [r for r in caplog.records if r.name == 'kitebase']
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    assert 'echo by rossi → success 200' in lines[0].getMessage()
    assert 'ms [req-1]' in lines[0].getMessage()
    assert result['request_id'] == 'req-1'


def test_a_failure_logs_the_traceback_under_the_request_id(processor, caplog):
    with caplog.at_level(logging.INFO, logger='kitebase'):
        processor.send({'operation': 'boom', 'request_id': 'req-2'})
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert 'boom by - failed' in errors[0].getMessage() and '[req-2]' in errors[0].getMessage()
    assert errors[0].exc_text and 'KeyError' in errors[0].exc_text


def test_a_refusal_is_a_warning_not_a_failure(processor, caplog):
    with caplog.at_level(logging.INFO, logger='kitebase'):
        processor.send({'operation': 'shaped'})
        processor.send({'operation': 'nowhere'})
    levels = [(r.levelno, r.getMessage().split(' ')[0]) for r in caplog.records if r.name == 'kitebase']
    assert (logging.WARNING, 'shaped') in levels
    assert (logging.WARNING, 'nowhere') in levels
    assert not any(lvl == logging.ERROR for lvl, _ in levels)


def test_parameter_values_are_never_logged(processor, caplog):
    with caplog.at_level(logging.DEBUG, logger='kitebase'):
        processor.send({'operation': 'echo',
                        'parameters': {'table': 'User', 'data': {'password': 'hunter2'}, 'ids': [1, 2]}})
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'hunter2' not in text
    assert 'data{password}' in text and 'ids[2]' in text and 'table' in text
