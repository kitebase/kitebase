"""Validators — the `validate:` a column declares, applied on the way in.

`validate` was written in YAML (the Email type names `email_validator`) long
before anything read it. Covered here: the registry, the rule that runs before
the transform and reports every failing field at once, the empty value that is
not validated, and the answer of the CRUD endpoints, which carries the errors by
field so a form can show each one under its field.
"""
from types import SimpleNamespace

import pytest

from kitebase import transforms
from kitebase.diagnostics import _check_validators
from kitebase.endpoint_db import handle_create, write_values


class StubColumn:
    def __init__(self, name, **attributes):
        self.name = name
        self.attributes = attributes


class StubTable:
    def __init__(self, columns):
        self.effective_columns = columns


def people():
    return StubTable([
        StubColumn('name'),
        StubColumn('email', validate='email_validator'),
        StubColumn('password', secret=True, validate='long_enough', on_write='password_hash'),
    ])


@pytest.fixture(autouse=True)
def long_enough():
    """A validator of the kind a plugin registers, looking at a neighbour too."""
    def rule(value, values):
        if len(value) < 8:
            return 'too short'
        if values.get('name') and values['name'].lower() in value.lower():
            return 'contains the name'
        return None
    transforms.register_validator('long_enough', rule)
    yield
    transforms._VALIDATORS.pop('long_enough', None)


def test_a_value_that_passes_is_written_and_then_transformed():
    result = write_values(people(), {'email': 'a@b', 'password': 'long-enough'})

    assert result['email'] == 'a@b'
    assert transforms.is_hashed(result['password'])      # the rule ran on the plain value


def test_every_failing_field_is_reported_at_once():
    with pytest.raises(transforms.ValidationError) as caught:
        write_values(people(), {'email': 'nobody', 'password': 'short'})

    assert set(caught.value.errors) == {'email', 'password'}
    assert caught.value.errors['password'] == 'too short'


def test_a_rule_can_look_at_the_other_values():
    with pytest.raises(transforms.ValidationError) as caught:
        write_values(people(), {'name': 'Mario', 'password': 'mario-2026'})

    assert caught.value.errors == {'password': 'contains the name'}


def test_an_empty_value_is_not_validated():
    assert write_values(people(), {'email': '', 'password': ''}) == {'email': ''}


def test_an_unknown_validator_is_refused():
    table = StubTable([StubColumn('code', validate='nobody_registered_this')])
    with pytest.raises(ValueError, match="unknown validator: 'nobody_registered_this'"):
        write_values(table, {'code': 'X'})


@pytest.mark.parametrize('address, valid', [
    ('mario@example.com', True),
    ('poller@localhost', True),       # loose on purpose: real addresses pass
    ('mario', False),
    ('mario rossi@example.com', False),
    ('a@b@c', False),
])
def test_the_email_validator_is_built_in(address, valid):
    assert (transforms.get_validator('email_validator')(address, {}) is None) is valid


def test_create_answers_with_the_errors_by_field():
    """What a form reads: `data.errors`, one message per field."""
    result = handle_create(None, None, {'data': {'email': 'nobody'}}, db_table=people())

    assert result['status'] == 'error' and result['code'] == 400
    assert result['data'] == {'errors': {'email': transforms.email_validator('nobody', {})}}


def test_check_reports_a_validator_nobody_registered():
    app = SimpleNamespace(tables={'Thing': StubTable([
        StubColumn('email', validate='email_validator'),
        StubColumn('code', validate='nobody_registered_this'),
    ])})
    issues = []
    _check_validators(app, issues)

    assert [(i['severity'], i['code'], i['path']) for i in issues] == [
        ('error', 'validator-unknown', 'tables.Thing.code')]
