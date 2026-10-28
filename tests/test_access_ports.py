"""Every port that reaches the data asks the guard, and a refusal stops it.

Access control is worth exactly as much as the port that skips the check, so
these tests are about completeness rather than policy: a guard that records
what it is asked stands in for any real one. Each port is called the way a
client calls it, through the dispatcher, and the test fixes which permissions
it asked for — then a guard that says no, and nothing must have been written.

The last test is the discipline in code: an endpoint of the core that does not
declare its access fails here, before anybody has to remember to look.
"""
import importlib.util

import pytest
import yaml

import kitebase.utils
from kitebase import access
from kitebase.db import DB, Base
from kitebase.endpoints import _ACCESS, _ENDPOINTS, CommandProcessor, endpoint
from kitebase.plugins import PluginsManager
from kitebase.source import Generator

TABLES = {
    'Author': {
        'name': 'authors',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'name', 'type': 'String', 'length': 60},
        ],
    },
    'Book': {
        'name': 'books',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
            {'name': 'author_id', 'type': 'Integer', 'foreign_key': {'target': 'Author.id'}},
        ],
    },
    'Chapter': {
        'name': 'chapters',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
            {'name': 'book_id', 'type': 'Integer',
             'foreign_key': {'target': 'Book.id', 'owned': True}},
        ],
    },
}

PAGES = {
    'book_form': {'content': {
        'type': 'form', 'source': {'model': 'Book'},
        'layout': [{'type': 'collection', 'id': 'chapters', 'model': 'Chapter',
                    'fk': 'book_id'}],
    }},
}


class Recorder(access.Guard):
    """Answers from a set of denied permissions, and remembers every question."""

    def __init__(self, denied=()):
        self.denied = set(denied)
        self.asked = []

    def allows(self, permission, context):
        self.asked.append(permission)
        return permission not in self.denied

    def allows_undeclared(self, endpoint, context):
        self.asked.append(f'undeclared:{endpoint}')
        return f'undeclared:{endpoint}' not in self.denied


@pytest.fixture(autouse=True)
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()
    access._GUARD = access.OPEN


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'bench'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}))
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
    spec = importlib.util.spec_from_file_location('access_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    instance.initialize_db(f'sqlite:///{tmp_path / "bench.sqlite"}', module, check_schema=False)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)

    with instance.get_session() as session:
        author = instance.models['Author'](name='Calvino')
        session.add(author)
        session.flush()
        book = instance.models['Book'](title='Il barone rampante', author_id=author.id)
        session.add(book)
        session.flush()
        session.add(instance.models['Chapter'](title='I', book_id=book.id))
        session.commit()
    return instance


def guarded(app, denied=()):
    guard = Recorder(denied)
    access.set_guard(guard)
    return guard


def send(operation, parameters):
    return CommandProcessor().send({'operation': operation, 'parameters': parameters})


def count(app, model):
    with app.get_session() as session:
        return session.query(app.models[model]).count()


# ── Reading: every table the statement reads ─────────────────────────────────

def test_a_query_asks_for_each_table_it_joins(app):
    guard = guarded(app)
    result = send('query', {'query': {
        'table': 'Book', 'select': ['Book.title', 'Author.name'],
        'joins': [{'Author': 'Author.id = Book.author_id'}]}})

    assert result['status'] == 'success'
    assert set(guard.asked) == {'Book.read', 'Author.read'}


def test_a_filter_through_a_relation_asks_for_the_table_it_reaches(app):
    guard = guarded(app)
    result = send('query', {'query': {
        'table': 'Book', 'filters': {'conditions': [{'author.name': 'Calvino'}]}}})

    assert result['status'] == 'success'
    assert 'Author.read' in guard.asked


def test_resolve_skips_behaviors_not_permissions(app):
    guard = guarded(app, denied={'Book.read'})
    result = send('query', {'query': {'table': 'Book', 'resolve': True}})

    assert result['code'] == 403
    assert 'Book.read' in result['message']


def test_a_joined_table_denied_refuses_the_whole_query(app):
    guarded(app, denied={'Author.read'})
    result = send('query', {'query': {
        'table': 'Book', 'joins': [{'Author': 'Author.id = Book.author_id'}]}})

    assert result['code'] == 403


def test_the_count_is_checked_too(app):
    guarded(app, denied={'Book.read'})
    result = send('query', {'query': {'table': 'Book'}, 'count': True})

    assert result['code'] == 403


def test_db_get_asks_read_by_id_and_as_a_list(app):
    guard = guarded(app)
    assert send('db', {'table': 'Book', 'method': 'get', 'id': 1})['status'] == 'success'
    assert send('db', {'table': 'Book', 'method': 'get'})['status'] == 'success'
    assert guard.asked == ['Book.read', 'Book.read']


def test_export_file_reads_through_the_query(app):
    guarded(app, denied={'Book.read'})
    result = send('export_file', {
        'format': 'csv', 'query': {'table': 'Book'}, 'ids': [1],
        'columns': [{'field': 'title'}]})

    assert result['code'] == 403


# ── Writing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('method,params,permission', [
    ('create', {'data': {'title': 'Palomar'}}, 'Book.create'),
    ('update', {'id': 1, 'data': {'title': 'x'}}, 'Book.update'),
    ('delete', {'id': 1}, 'Book.delete'),
])
def test_db_writes_ask_their_action(app, method, params, permission):
    guard = guarded(app)
    send('db', {'table': 'Book', 'method': method, **params})
    assert guard.asked == [permission]


def test_a_denied_write_writes_nothing(app):
    guarded(app, denied={'Book.create', 'Book.delete'})

    assert send('db', {'table': 'Book', 'method': 'create',
                       'data': {'title': 'Palomar'}})['code'] == 403
    assert send('db', {'table': 'Book', 'method': 'delete', 'id': 1})['code'] == 403
    assert count(app, 'Book') == 1


# ── The aggregate decides for its rows ───────────────────────────────────────

def test_load_tree_asks_for_the_root_only(app):
    guard = guarded(app)
    result = send('load_tree', {'page': 'book_form', 'id': 1})

    assert result['status'] == 'success'
    assert result['data']['children']['chapters']
    assert set(guard.asked) == {'Book.read'}


def test_save_tree_asks_for_the_root_only(app):
    guard = guarded(app)
    result = send('save_tree', {'page': 'book_form', 'root': {
        'op': 'create', 'id': -1, 'values': {'title': 'Palomar'},
        'children': {'chapters': [{'op': 'create', 'id': -2, 'values': {'title': 'I'}}]}}})

    assert result['status'] == 'success', result.get('message')
    # Re-reading what was written asks nothing more: the write answered for it.
    assert guard.asked == ['Book.create']


def test_a_change_below_the_root_modifies_the_root(app):
    guard = guarded(app, denied={'Book.update'})
    result = send('save_tree', {'page': 'book_form', 'root': {
        'id': 1, 'values': {},
        'children': {'chapters': [{'op': 'create', 'id': -2, 'values': {'title': 'II'}}]}}})

    assert result['code'] == 403
    assert guard.asked == ['Book.update']
    assert count(app, 'Chapter') == 1


# ── Endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture
def declared():
    """Two endpoints of a plugin: one with a permission, one that says nothing."""
    calls = []

    @endpoint('bench.renew', access='bench.renew')
    def renew(params):
        calls.append('renew')
        return {}

    @endpoint('bench.silent')
    def silent(params):
        calls.append('silent')
        return {}

    yield calls
    for name in ('bench.renew', 'bench.silent'):
        _ENDPOINTS.pop(name, None)
        _ACCESS.pop(name, None)


def test_a_declared_permission_is_checked_before_the_endpoint_runs(app, declared):
    guarded(app, denied={'bench.renew'})
    result = send('bench.renew', {})

    assert result['code'] == 403
    assert declared == []


def test_an_undeclared_endpoint_is_left_to_the_guard(app, declared):
    guard = guarded(app, denied={'undeclared:bench.silent'})

    assert send('bench.silent', {})['code'] == 403
    assert guard.asked == ['undeclared:bench.silent']
    assert declared == []


def test_without_a_guard_everything_is_open(app, declared):
    assert access._GUARD is access.OPEN
    assert send('bench.silent', {})['status'] == 'success'
    assert send('db', {'table': 'Book', 'method': 'delete', 'id': 1})['status'] == 'success'


def test_a_second_guard_is_an_error():
    access.set_guard(Recorder())
    with pytest.raises(ValueError):
        access.set_guard(Recorder())


def test_the_guard_is_handed_the_app_it_serves(tmp_path, monkeypatch):
    seen = []

    class Binding(Recorder):
        def bind(self, app):
            seen.append(app)

    access.set_guard(Binding())
    plugin = tmp_path / 'plugins' / 'bench'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)
    pm = PluginsManager()
    pm.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()
    instance = DB()
    instance.calc_db(pm)

    assert seen == [instance]


def test_every_core_endpoint_declares_its_access():
    """A new endpoint of the core says what it requires, or this fails."""
    undeclared = sorted(name for name, func in _ENDPOINTS.items()
                        if func.__module__.startswith('kitebase.')
                        and _ACCESS.get(name) is access.UNDECLARED)
    assert undeclared == []
