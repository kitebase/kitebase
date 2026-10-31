"""Context scoping: BaseApp.context() and the context handling of get_session().

The case these cover is the one that stays invisible while kitebase owns the
process: a session opened with a context where no context was set before must
not leave that context behind. It bites as soon as kitebase is a guest —
a background thread, or a host route that reuses the same worker thread for the
next request and would inherit an identity nobody chose.
"""
import pytest
from sqlalchemy import create_engine

from kitebase.db import DB, BaseApp


@pytest.fixture(autouse=True)
def clean_context():
    """Context storage is class-level: leave it empty around every test."""
    BaseApp.set_context(None)
    yield
    BaseApp.set_context(None)


@pytest.fixture
def db():
    """A DB with just an engine — enough for get_session()."""
    app = DB()
    app.engine = create_engine('sqlite:///:memory:')
    return app


# ── BaseApp.context() ────────────────────────────────────────────────────────

def test_context_sets_inside_and_restores_none_outside():
    with BaseApp.context({'username': 'poller'}) as ctx:
        assert ctx == {'username': 'poller'}
        assert BaseApp.get_context() == {'username': 'poller'}

    assert BaseApp.get_context() is None


def test_context_restores_the_previous_context():
    BaseApp.set_context({'username': 'alice'})

    with BaseApp.context({'username': 'bob'}):
        assert BaseApp.get_context() == {'username': 'bob'}

    assert BaseApp.get_context() == {'username': 'alice'}


def test_context_restores_on_exception():
    BaseApp.set_context({'username': 'alice'})

    with pytest.raises(RuntimeError):
        with BaseApp.context({'username': 'bob'}):
            raise RuntimeError('boom')

    assert BaseApp.get_context() == {'username': 'alice'}


def test_context_nests():
    with BaseApp.context({'username': 'outer'}):
        with BaseApp.context({'username': 'inner'}):
            assert BaseApp.get_context() == {'username': 'inner'}
        assert BaseApp.get_context() == {'username': 'outer'}

    assert BaseApp.get_context() is None


# ── get_session() ────────────────────────────────────────────────────────────

def test_get_session_leaves_no_context_behind(db):
    """R3: with no context on the way in, the session's context must not stick."""
    with db.get_session(context={'username': 'poller'}) as session:
        assert session is not None
        assert BaseApp.get_context() == {'username': 'poller'}

    assert BaseApp.get_context() is None


def test_get_session_restores_the_previous_context(db):
    BaseApp.set_context({'username': 'alice'})

    with db.get_session(context={'username': 'batch'}):
        assert BaseApp.get_context() == {'username': 'batch'}

    assert BaseApp.get_context() == {'username': 'alice'}


def test_get_session_without_context_does_not_touch_it(db):
    BaseApp.set_context({'username': 'alice'})

    with db.get_session():
        assert BaseApp.get_context() == {'username': 'alice'}

    assert BaseApp.get_context() == {'username': 'alice'}


def test_get_session_restores_context_on_exception(db):
    with pytest.raises(RuntimeError):
        with db.get_session(context={'username': 'poller'}):
            raise RuntimeError('boom')

    assert BaseApp.get_context() is None


# ── Context fields registered by plugins ─────────────────────────────────────

@pytest.fixture
def context_registry():
    from kitebase import server_utils
    fields = list(server_utils._REGISTERED_CONTEXT_FIELDS)
    owners = dict(server_utils._CONTEXT_FIELD_OWNERS)
    yield server_utils
    server_utils._REGISTERED_CONTEXT_FIELDS[:] = fields
    server_utils._CONTEXT_FIELD_OWNERS.clear()
    server_utils._CONTEXT_FIELD_OWNERS.update(owners)


def test_a_field_registered_again_by_its_owner_is_harmless(context_registry):
    context_registry.add_context_field('branch_id', owner='a')
    context_registry.add_context_field('branch_id', owner='a')
    assert context_registry._REGISTERED_CONTEXT_FIELDS.count('branch_id') == 1


def test_two_owners_of_one_field_collide(context_registry):
    context_registry.add_context_field('branch_id', owner='a')
    with pytest.raises(ValueError, match='both a and b'):
        context_registry.add_context_field('branch_id', owner='b')


@pytest.mark.parametrize('name', ['exp', 'src', 'username'])
def test_a_field_cannot_take_a_name_of_the_token(context_registry, name):
    with pytest.raises(ValueError, match='token uses that name'):
        context_registry.add_context_field(name, owner='a')
