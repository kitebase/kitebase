"""Context scoping: kitebase.context.use() and the context handling of get_session().

The case these cover is the one that stays invisible while kitebase owns the
process: a session opened with a context where no context was set before must
not leave that context behind. It bites as soon as kitebase is a guest —
a background thread, or a host route that reuses the same worker thread for the
next request and would inherit an identity nobody chose.
"""
import pytest
from sqlalchemy import create_engine

from kitebase import context
from kitebase.app import App


@pytest.fixture(autouse=True)
def clean_context():
    """The context outlives a test on its thread: leave it empty around every test."""
    context.replace(None)
    yield
    context.replace(None)


@pytest.fixture
def db():
    """An app with just an engine — enough for get_session()."""
    app = App()
    app.engine = create_engine('sqlite:///:memory:')
    return app


# ── use() ────────────────────────────────────────────────────────────────

def test_context_sets_inside_and_restores_none_outside():
    with context.use({'username': 'poller'}) as ctx:
        assert ctx == {'username': 'poller'}
        assert context.current() == {'username': 'poller'}

    assert context.current() is None


def test_context_restores_the_previous_context():
    context.replace({'username': 'alice'})

    with context.use({'username': 'bob'}):
        assert context.current() == {'username': 'bob'}

    assert context.current() == {'username': 'alice'}


def test_context_restores_on_exception():
    context.replace({'username': 'alice'})

    with pytest.raises(RuntimeError):
        with context.use({'username': 'bob'}):
            raise RuntimeError('boom')

    assert context.current() == {'username': 'alice'}


def test_context_nests():
    with context.use({'username': 'outer'}):
        with context.use({'username': 'inner'}):
            assert context.current() == {'username': 'inner'}
        assert context.current() == {'username': 'outer'}

    assert context.current() is None


# ── get_session() ────────────────────────────────────────────────────────────

def test_get_session_leaves_no_context_behind(db):
    """R3: with no context on the way in, the session's context must not stick."""
    with db.get_session(context={'username': 'poller'}) as session:
        assert session is not None
        assert context.current() == {'username': 'poller'}

    assert context.current() is None


def test_get_session_restores_the_previous_context(db):
    context.replace({'username': 'alice'})

    with db.get_session(context={'username': 'batch'}):
        assert context.current() == {'username': 'batch'}

    assert context.current() == {'username': 'alice'}


def test_get_session_without_context_does_not_touch_it(db):
    context.replace({'username': 'alice'})

    with db.get_session():
        assert context.current() == {'username': 'alice'}

    assert context.current() == {'username': 'alice'}


def test_get_session_restores_context_on_exception(db):
    with pytest.raises(RuntimeError):
        with db.get_session(context={'username': 'poller'}):
            raise RuntimeError('boom')

    assert context.current() is None


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
