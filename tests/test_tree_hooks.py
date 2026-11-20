"""The two hooks of trees: before a tree is read, before a save writes it.

A plugin takes part in both verbs without the core knowing why. These tests
play the plugin with a version counter in a dict, the shape the concurrency
plugin has: a key handed out with the tree, checked and renewed at the save.
"""
import pytest

from kitebase import endpoint_tree
from kitebase.endpoint_tree import (before_tree_write, before_tree_load, load_tree,
                                    save_tree, save_trees)
from kitebase.errors import Conflict
from test_save_tree import app, fresh_registry, ok, rows  # noqa: F401


@pytest.fixture
def hooks():
    """The registries as they were, after each test."""
    saved = endpoint_tree._LOAD_HOOKS.snapshot(), endpoint_tree._WRITE_HOOKS.snapshot()
    yield
    endpoint_tree._LOAD_HOOKS.restore(saved[0])
    endpoint_tree._WRITE_HOOKS.restore(saved[1])


@pytest.fixture
def versions(hooks):
    """A version per root, read before the data and checked at the save."""
    store = {}
    calls = []

    @before_tree_load
    def hand_out(model, record_id):
        calls.append(('load', model, record_id))
        return {'$version': store.setdefault((model, record_id), 1)}

    @before_tree_write
    def check(write):
        calls.append(('write', write.model, write.id, write.action))
        if write.action == 'create':
            return None
        key = (write.model, write.id)
        if write.meta.get('$version') != store.get(key):
            raise Conflict('Somebody saved this record in the meantime')
        store[key] += 1
        return {'$version': store[key]}

    return store, calls


def create_book(title='Dune'):
    return ok(save_tree({'page': 'book_form',
                         'root': {'op': 'create', 'id': -1, 'values': {'title': title}}}))


def test_a_loaded_tree_carries_what_the_load_hooks_say(app, versions):
    book = create_book()['id']
    tree = ok(load_tree({'page': 'book_form', 'id': book}))
    assert tree['$version'] == 1
    assert 'version' not in tree['values']


def test_the_load_hooks_run_before_the_data_is_read(app, versions, monkeypatch):
    _, calls = versions
    book = create_book()['id']
    calls.clear()
    real = endpoint_tree._load_tree

    def spy(*args, **kwargs):
        calls.append(('data',))
        return real(*args, **kwargs)

    monkeypatch.setattr(endpoint_tree, '_load_tree', spy)
    load_tree({'page': 'book_form', 'id': book})
    assert calls == [('load', 'Book', book), ('data',)]


def test_a_save_carries_the_root_metadata_to_the_commit_hook_and_back(app, versions):
    book = create_book()['id']
    tree = ok(load_tree({'page': 'book_form', 'id': book}))

    saved = ok(save_tree({'page': 'book_form',
                          'root': {'op': 'update', 'id': book, '$version': tree['$version'],
                                   'values': {'title': 'Dune Messiah'}}}))
    assert saved['root']['$version'] == tree['$version'] + 1
    assert rows(app, 'Book')[0]['title'] == 'Dune Messiah'


def test_a_conflict_rolls_back_the_whole_tree_and_answers_with_it_fresh(app, versions):
    book = create_book()['id']
    first = ok(load_tree({'page': 'book_form', 'id': book}))
    second = ok(load_tree({'page': 'book_form', 'id': book}))

    ok(save_tree({'page': 'book_form',
                  'root': {'op': 'update', 'id': book, '$version': first['$version'],
                           'values': {'title': 'Children of Dune'}}}))

    with pytest.raises(Conflict) as refused:
        save_tree({'page': 'book_form',
                   'root': {'op': 'update', 'id': book, '$version': second['$version'],
                            'values': {'title': 'God Emperor'},
                            'children': {'chapters': [
                                {'op': 'create', 'id': -2, 'values': {'title': 'One'}}]}}})

    assert refused.value.code == 409
    fresh = refused.value.data['root']
    assert fresh['values']['title'] == 'Children of Dune'
    assert fresh['$version'] == first['$version'] + 1
    # Nothing of the refused save reached the database, the rows below included
    assert rows(app, 'Book')[0]['title'] == 'Children of Dune'
    assert rows(app, 'Chapter') == []


def test_the_write_hooks_run_before_any_row_is_written(app, versions, monkeypatch):
    _, calls = versions
    book = create_book()['id']
    tree = ok(load_tree({'page': 'book_form', 'id': book}))
    calls.clear()
    real = endpoint_tree._save_node

    def spy(*args, **kwargs):
        calls.append(('rows',))
        return real(*args, **kwargs)

    monkeypatch.setattr(endpoint_tree, '_save_node', spy)
    ok(save_tree({'page': 'book_form',
                  'root': {'op': 'update', 'id': book, '$version': tree['$version'],
                           'values': {'title': 'x'}}}))
    assert calls == [('write', 'Book', book, 'update'), ('rows',)]


def test_a_deleted_root_reaches_the_write_hook_by_its_key(app, versions):
    _, calls = versions
    book = create_book()['id']
    tree = ok(load_tree({'page': 'book_form', 'id': book}))
    calls.clear()
    ok(save_tree({'page': 'book_form',
                  'root': {'op': 'delete', 'id': book, '$version': tree['$version']}}))
    assert calls == [('write', 'Book', book, 'delete')]


def test_save_trees_passes_every_root_through_the_write_hook(app, versions):
    _, calls = versions
    ok(save_trees('book_form', [
        {'op': 'create', 'id': -1, 'values': {'title': 'A'}},
        {'op': 'create', 'id': -1, 'values': {'title': 'B'}},
    ]))
    assert calls == [('write', 'Book', -1, 'create')] * 2


def test_a_hook_may_only_add_dollar_keys(app, hooks):
    @before_tree_load
    def careless(model, record_id):
        return {'title': 'overwritten'}

    book = create_book()['id']
    with pytest.raises(ValueError, match="without '\\$'"):
        load_tree({'page': 'book_form', 'id': book})


def test_two_hooks_may_not_return_the_same_key(app, versions):
    @before_tree_load
    def another(model, record_id):
        return {'$version': 'mine'}

    book = create_book()['id']
    with pytest.raises(ValueError, match=r"hand_out and .*another both return '\$version'"):
        load_tree({'page': 'book_form', 'id': book})


def test_two_hooks_with_different_keys_add_up(app, versions):
    @before_tree_load
    def presence(model, record_id):
        return {'$presence': []}

    book = create_book()['id']
    tree = ok(load_tree({'page': 'book_form', 'id': book}))
    assert tree['$version'] == 1 and tree['$presence'] == []


def test_without_hooks_a_tree_has_no_metadata(app, hooks):
    book = create_book()['id']
    tree = ok(load_tree({'page': 'book_form', 'id': book}))
    assert not [k for k in tree if k.startswith('$')]


# ── A form without collections is a tree of one node ─────────────────────────

def test_a_flat_form_carries_the_version_and_meets_the_conflict(app, versions):
    book = create_book()['id']
    first = ok(load_tree({'page': 'book_quick_form', 'id': book}))
    second = ok(load_tree({'page': 'book_quick_form', 'id': book}))
    assert first['children'] == {}

    ok(save_tree({'page': 'book_quick_form',
                  'root': {'op': 'update', 'id': book, '$version': first['$version'],
                           'values': {'title': 'Children of Dune'}}}))
    with pytest.raises(Conflict) as refused:
        save_tree({'page': 'book_quick_form',
                   'root': {'op': 'update', 'id': book, '$version': second['$version'],
                            'values': {'isbn': '978'}}})
    assert refused.value.data['root']['values']['title'] == 'Children of Dune'
    assert rows(app, 'Book')[0]['isbn'] is None


def test_a_typed_key_reaches_the_hooks_and_comes_back_fresh(app, versions):
    _, calls = versions
    ok(save_tree({'page': 'shelf_form',
                  'root': {'op': 'create', 'id': 'A1', 'values': {'code': 'A1'}}}))
    first = ok(load_tree({'page': 'shelf_form', 'id': 'A1'}))
    ok(save_tree({'page': 'shelf_form',
                  'root': {'op': 'update', 'id': 'A1', '$version': first['$version'],
                           'values': {'room': 'Hall'}}}))
    with pytest.raises(Conflict) as refused:
        save_tree({'page': 'shelf_form',
                   'root': {'op': 'update', 'id': 'A1', '$version': first['$version'],
                            'values': {'room': 'Attic'}}})
    assert ('write', 'Shelf', 'A1', 'update') in calls
    assert refused.value.data['root']['values']['room'] == 'Hall'
