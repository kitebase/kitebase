"""The shape every registry has: a name belongs to the module that took it."""
import types

import pytest

from kitebase import registry
from kitebase.endpoints import _ACCESS, _ENDPOINTS, endpoint
from kitebase.registry import Collision, Registry, Slot


@pytest.fixture
def names():
    """Registries made by a test leave the catalog with it."""
    before = dict(registry._CATALOG)
    yield
    registry._CATALOG.clear()
    registry._CATALOG.update(before)


def _from(module: str, name: str = 'fn'):
    """A function as the module `module` would define it."""
    def fn(*args):
        return module
    fn.__module__, fn.__name__ = module, name
    return fn


# ── Registry ─────────────────────────────────────────────────────────────────

def test_an_entry_is_read_like_a_dictionary(names):
    reg = Registry('t.read')
    first = reg.add('a', _from('one'))

    assert reg['a'] is first
    assert list(reg) == ['a'] and len(reg) == 1 and 'a' in reg
    assert reg.get('missing') is None
    assert reg.owner('a') == 'one'


def test_the_same_module_replaces_its_own_entry(names):
    """A module imported again registers again: that is not a collision."""
    reg = Registry('t.reload')
    reg.add('a', _from('one'))
    again = reg.add('a', _from('one'))

    assert reg['a'] is again
    assert reg.replaced('a') == []


def test_another_module_is_refused(names):
    reg = Registry('t.refuse', 'thing')
    reg.add('a', _from('one'))

    with pytest.raises(Collision) as raised:
        reg.add('a', _from('two'))

    message = str(raised.value)
    assert "thing 'a' is already registered by one" in message
    assert 'two cannot register it too' in message
    assert reg.owner('a') == 'one'


def test_replace_is_said_and_remembered(names):
    reg = Registry('t.replace')
    reg.add('a', _from('one'))
    second = reg.add('a', _from('two'), replace=True)

    assert reg['a'] is second
    assert reg.owner('a') == 'two'
    assert reg.replaced('a') == ['one']


def test_assignment_is_a_registration(names):
    reg = Registry('t.assign')
    reg['a'] = _from('one')

    assert reg.owner('a') == 'one'
    with pytest.raises(Collision):
        reg['a'] = _from('two')


def test_a_removed_name_is_free_again(names):
    reg = Registry('t.remove')
    reg.add('a', _from('one'))
    del reg['a']
    reg.add('a', _from('two'))

    assert reg.owner('a') == 'two' and reg.replaced('a') == []


def test_a_value_without_a_module_belongs_to_who_registers_it(names):
    reg = Registry('t.plain')
    reg.add('a', 42)

    assert reg.owner('a') == __name__


def test_snapshot_and_restore(names):
    reg = Registry('t.snapshot')
    reg.add('a', _from('one'))
    saved = reg.snapshot()
    reg.add('a', _from('two'), replace=True)
    reg.add('b', _from('two'))

    reg.restore(saved)

    assert list(reg) == ['a'] and reg.owner('a') == 'one' and reg.replaced('a') == []


def test_registries_are_listed_by_name(names):
    reg = Registry('t.listed')

    assert registry.catalog()['t.listed'] is reg
    assert registry.catalog()['endpoints'] is _ENDPOINTS
    with pytest.raises(ValueError):
        Registry('t.listed')


# ── Slot ─────────────────────────────────────────────────────────────────────

def test_a_slot_holds_one_value_or_the_default(names):
    slot = Slot('t.slot', 'default', 'policy')
    assert slot.get() == 'default' and not slot.taken

    policy = types.SimpleNamespace(__module__='one')
    slot.set(policy)
    assert slot.get() is policy and slot.owner == 'one'

    with pytest.raises(Collision) as raised:
        slot.set(types.SimpleNamespace(__module__='two'))
    assert 'policy is already registered by one' in str(raised.value)

    slot.reset()
    assert slot.get() == 'default' and not slot.taken


# ── Endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture
def endpoints():
    saved, access = _ENDPOINTS.snapshot(), dict(_ACCESS)
    yield
    _ENDPOINTS.restore(saved)
    _ACCESS.clear()
    _ACCESS.update(access)


def test_two_modules_cannot_answer_the_same_call(endpoints):
    endpoint('t_books')(_from('books.api'))

    with pytest.raises(Collision) as raised:
        endpoint('t_books')(_from('library.api'))

    assert "endpoint 't_books' is already registered by books.api" in str(raised.value)
    assert _ENDPOINTS['t_books']() == 'books.api'


def test_an_endpoint_is_replaced_by_who_says_so(endpoints):
    endpoint('t_books')(_from('books.api'))
    endpoint('t_books', replace=True)(_from('library.api'))

    assert _ENDPOINTS['t_books']() == 'library.api'
    assert _ENDPOINTS.replaced('t_books') == ['books.api']
