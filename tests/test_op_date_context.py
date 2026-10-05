"""op_date in the token: carried only when the user chose a date.

A token outlives the day it was issued on (24 h, renewed by the refresh), so a
date written at login would keep the operator on the day of their last login.
Absent means today, read per request by defaults.op_date().
"""
import jwt

from kitebase import server_utils

SECRET = 'test-secret-long-enough-for-hs256-signing'


class _Processor:
    """Stands in for the command processor: the auth operation succeeds."""

    def send(self, command):
        return {'status': 'success',
                'data': {'context': {'id': 1, 'username': 'admin'}}}


def _decode(token):
    return jwt.decode(token, SECRET, algorithms=['HS256'])


def _login():
    result = server_utils.handle_auth(
        _Processor(), {'username': 'admin', 'password': 'admin'}, SECRET,
        context_fields=['id'])
    return _decode(result['data']['token'])


def _update(context, updates):
    result = server_utils.handle_update_context(context, updates, SECRET)
    return _decode(result['data']['token'])


def test_the_login_token_carries_no_op_date():
    assert 'op_date' not in _login()


def test_a_chosen_op_date_is_carried():
    assert _update(_login(), {'op_date': '2026-03-15'})['op_date'] == '2026-03-15'


def test_a_chosen_op_date_survives_a_later_update():
    context = _update(_login(), {'op_date': '2026-03-15'})
    assert _update(context, {})['op_date'] == '2026-03-15'


def test_none_puts_op_date_back_to_today():
    context = _update(_login(), {'op_date': '2026-03-15'})
    assert 'op_date' not in _update(context, {'op_date': None})


def test_none_does_not_reach_a_field_outside_the_allowlist():
    context = _login()
    assert _update(context, {'id': None})['id'] == 1
