"""Reading and writing a RefSet on a database: `Book.tags` as a list of keys.

The bench is the shape tags will have: a vocabulary whose key comes from a mixin,
a book with a key of its own, and a junction that carries nothing but the pair.
"""
import importlib.util

import pytest
import yaml

import kitebase.refsets
import kitebase.utils
from kitebase.db import DB, Base
from kitebase.endpoint_db import db_operations
from kitebase.endpoint_tree import load_tree, save_tree
from kitebase.plugins import PluginsManager
from kitebase.source import Generator

TYPES = {
    'Vocabulary': {'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'name', 'type': 'String', 'length': 60},
    ]},
}

TABLES = {
    'Book': {
        'name': 'books',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
        ],
    },
    'BookTag': {'name': 'book_tags', 'mixins': ['Vocabulary']},
    'BookTagLink': {
        'name': 'book_tag_links',
        'many_to_many': {
            'target1': {'table': 'Book.id', 'column': 'book_id',
                        'collection': 'tags', 'field': True},
            'target2': {'table': 'BookTag.id', 'column': 'tag_id'},
        },
    },
}

PAGES = {
    'book_form': {'content': {'type': 'form', 'source': {'model': 'Book'}, 'layout': []}},
}


@pytest.fixture(autouse=True)
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'bench'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'types': TYPES, 'tables': TABLES}))
    (plugin / 'pages.yaml').write_text(yaml.safe_dump({'pages': PAGES}))

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()

    instance = DB()
    instance.calc_db(pm)

    source = tmp_path / 'model.py'
    Generator(instance).generate(filename=str(source))
    spec = importlib.util.spec_from_file_location('bench_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    instance.initialize_db(f'sqlite:///{tmp_path / "bench.sqlite"}', module, check_schema=False)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)
    return instance


def insert(app, model_name, **values):
    model_class = app.find_model_class(model_name)
    with app.get_session() as session:
        obj = model_class(**values)
        session.add(obj)
        session.commit()
        return obj.id


def ok(result):
    assert result['status'] == 'success', result.get('message')
    return result['data']


@pytest.fixture
def shelf(app):
    """Two books, three tags; the first book tagged twice, in a stated order."""
    peach, mango, plum = (insert(app, 'BookTag', name=n) for n in ('Peach', 'Mango', 'Plum'))
    tagged = insert(app, 'Book', title='Tagged')
    bare = insert(app, 'Book', title='Bare')
    insert(app, 'BookTagLink', book_id=tagged, tag_id=plum)
    insert(app, 'BookTagLink', book_id=tagged, tag_id=peach)
    return {'tagged': tagged, 'bare': bare, 'peach': peach, 'mango': mango, 'plum': plum}


# ── Read ──────────────────────────────────────────────────────────────────────

def test_a_record_carries_the_keys_it_is_linked_to(app, shelf):
    data = ok(db_operations({'table': 'Book', 'method': 'get', 'id': shelf['tagged']}))
    assert data['tags'] == [shelf['plum'], shelf['peach']]      # as the links were written


def test_a_record_without_links_carries_an_empty_list(app, shelf):
    data = ok(db_operations({'table': 'Book', 'method': 'get', 'id': shelf['bare']}))
    assert data['tags'] == []


def test_the_tree_root_carries_it_too(app, shelf):
    data = ok(load_tree({'page': 'book_form', 'id': shelf['tagged']}))
    assert data['values']['tags'] == [shelf['plum'], shelf['peach']]


def test_the_other_end_carries_nothing(app, shelf):
    data = ok(db_operations({'table': 'BookTag', 'method': 'get', 'id': shelf['plum']}))
    assert 'books' not in data


# ── Write ─────────────────────────────────────────────────────────────────────

def links(app):
    """Every link, as (book, tag) pairs in the order they were written."""
    model_class = app.find_model_class('BookTagLink')
    with app.get_session() as session:
        return [(row.book_id, row.tag_id)
                for row in session.query(model_class).order_by(model_class.id)]


def test_a_new_record_is_written_with_its_links(app, shelf):
    data = ok(db_operations({'table': 'Book', 'method': 'create',
                             'data': {'title': 'New', 'tags': [shelf['mango'], shelf['peach']]}}))
    assert data['tags'] == [shelf['mango'], shelf['peach']]
    assert links(app)[-2:] == [(data['id'], shelf['mango']), (data['id'], shelf['peach'])]


def test_an_update_keeps_what_stays_and_adds_after_it(app, shelf):
    """Plum stays where it was; peach goes; mango comes after plum."""
    tagged = shelf['tagged']
    data = ok(db_operations({'table': 'Book', 'method': 'update', 'id': tagged,
                             'data': {'tags': [shelf['mango'], shelf['plum']]}}))
    assert data['tags'] == [shelf['plum'], shelf['mango']]
    assert [pair for pair in links(app) if pair[0] == tagged] == \
        [(tagged, shelf['plum']), (tagged, shelf['mango'])]


def test_an_update_that_does_not_name_the_field_leaves_the_links_alone(app, shelf):
    ok(db_operations({'table': 'Book', 'method': 'update', 'id': shelf['tagged'],
                      'data': {'title': 'Renamed'}}))
    assert len(links(app)) == 2


def test_an_empty_list_removes_every_link(app, shelf):
    data = ok(db_operations({'table': 'Book', 'method': 'update', 'id': shelf['tagged'],
                             'data': {'tags': []}}))
    assert data['tags'] == [] and links(app) == []


def test_a_key_that_does_not_exist_is_refused_and_nothing_is_written(app, shelf):
    result = db_operations({'table': 'Book', 'method': 'update', 'id': shelf['tagged'],
                            'data': {'title': 'Changed', 'tags': [shelf['mango'], 999]}})
    assert result['status'] == 'error'
    assert '999' in result['data']['errors']['tags']
    assert len(links(app)) == 2
    book = ok(db_operations({'table': 'Book', 'method': 'get', 'id': shelf['tagged']}))
    assert book['title'] == 'Tagged'


def test_the_tree_writes_the_links_of_its_root(app, shelf):
    data = ok(save_tree({'page': 'book_form', 'root': {
        'op': 'create', 'id': -1, 'values': {'title': 'Tree', 'tags': [shelf['plum']]}}}))
    assert (data['id'], shelf['plum']) in links(app)


class Names:
    """A resolver as a plugin would register one: names stand for the tags they name."""

    @staticmethod
    def applies_to(target_class):
        return target_class.__name__ == 'BookTag'

    @staticmethod
    def resolve(session, target_class, ref, values):
        by_name = {tag.name: tag.id for tag in session.query(target_class)}
        return [by_name.get(v, v) if isinstance(v, str) else v for v in values]


def test_a_registered_resolver_turns_values_into_keys(app, shelf, monkeypatch):
    monkeypatch.setattr(kitebase.refsets, '_RESOLVERS', [])
    kitebase.refsets.register_resolver(Names)
    data = ok(db_operations({'table': 'Book', 'method': 'update', 'id': shelf['bare'],
                             'data': {'tags': ['Mango', shelf['plum']]}}))
    assert data['tags'] == [shelf['mango'], shelf['plum']]
