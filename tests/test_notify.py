"""The port of `notify()`: what a work says while it runs, and when it stops.

The notifier here is a list: what reached it and when. The clock is the
test's, so the once-a-second rule is checked without waiting."""
import logging

import pytest

import kitebase.notify as notify_mod
from kitebase.endpoints import CommandDispatcher, _ENDPOINTS
from kitebase.errors import Cancelled
from kitebase.notify import notify, work


class Recorder(notify_mod.Notifier):
    def __init__(self):
        self.writes = []
        self.closed = []
        self.cancel = False
        self.fail = False

    def write(self, key, state):
        if self.fail:
            raise RuntimeError('the support is down')
        self.writes.append((key, state))
        return self.cancel

    def close(self, key):
        self.closed.append(key)


@pytest.fixture
def recorder():
    snapshot = notify_mod.NOTIFIER.snapshot()
    rec = Recorder()
    notify_mod.NOTIFIER.set(rec, owner='test')
    yield rec
    notify_mod.NOTIFIER.restore(snapshot)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(notify_mod.time, 'monotonic', lambda: now[0])
    return now


def test_without_a_notifier_it_goes_to_the_log(caplog):
    snapshot = notify_mod.NOTIFIER.snapshot()
    notify_mod.NOTIFIER.reset()
    try:
        with caplog.at_level(logging.DEBUG, logger='kitebase.notify'), work('w1'):
            notify('Reading', done=1, total=3, cancellable=True)
    finally:
        notify_mod.NOTIFIER.restore(snapshot)
    assert 'Reading (1/3)' in caplog.text


def test_outside_a_work_nothing_is_written(recorder):
    notify('Alone')
    assert recorder.writes == []


def test_the_first_call_writes_then_at_most_once_a_second(recorder, clock):
    with work('w1'):
        notify('one')
        clock[0] += 0.3
        notify('two')
        clock[0] += 0.8
        notify('three')
    assert [state['text'] for _, state in recorder.writes] == ['one', 'three']
    assert recorder.closed == ['w1']


def test_a_change_of_phase_writes_at_once(recorder, clock):
    with work('w1'):
        notify('reading', cancellable=True)
        clock[0] += 0.1
        notify('writing')
    assert [(s['text'], s['cancellable']) for _, s in recorder.writes] == [
        ('reading', True), ('writing', False)]


def test_a_work_that_said_nothing_is_not_closed(recorder):
    with work('w1'):
        pass
    assert recorder.closed == []


def test_a_cancel_stops_only_a_cancellable_phase(recorder, clock):
    recorder.cancel = True
    with pytest.raises(Cancelled), work('w1'):
        notify('writing')                   # not cancellable: goes on
        clock[0] += 0.1
        notify('next book', cancellable=True)
    assert recorder.closed == ['w1']


def test_a_cancel_seen_earlier_stops_the_next_cancellable_call(recorder, clock):
    """Seen while writing a protected phase, it waits for the first safe point,
    even when that point is not due to be written."""
    with work('w1'):
        notify('writing')
        recorder.cancel = True
        clock[0] += 2
        notify('still writing')              # written: the cancel is seen
        recorder.writes.clear()
        with pytest.raises(Cancelled):
            notify('next book', cancellable=True)


def test_a_failing_notifier_never_fails_the_work(recorder):
    recorder.fail = True
    with work('w1'):
        notify('one', cancellable=True)
    assert recorder.closed == []


def test_a_work_inside_a_work_joins_it(recorder):
    with work('outer'):
        with work('inner'):
            notify('deep')
    assert recorder.writes[0][0] == 'outer'
    assert recorder.closed == ['outer']


def test_the_dispatcher_opens_a_work_per_request(recorder):
    _ENDPOINTS['speaks'] = lambda params: notify('working') or 'done'
    dispatcher = CommandDispatcher()
    try:
        result = dispatcher.send({'operation': 'speaks', 'request_id': 'req-9'})
    finally:
        _ENDPOINTS.pop('speaks', None)
    assert result['data'] == 'done'
    assert recorder.writes == [('req-9', {'text': 'working', 'done': None,
                                          'total': None, 'cancellable': False})]
    assert recorder.closed == ['req-9']


def test_a_cancelled_request_answers_with_its_refusal(recorder):
    recorder.cancel = True
    _ENDPOINTS['stops'] = lambda params: notify('step', cancellable=True)
    dispatcher = CommandDispatcher()
    try:
        result = dispatcher.send({'operation': 'stops'})
    finally:
        _ENDPOINTS.pop('stops', None)
    assert result['status'] == 'error'
    assert result['error_type'] == 'Cancelled'
    assert result['code'] == 409
