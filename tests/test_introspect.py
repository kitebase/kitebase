"""
What the inspector asks of a loaded application: provenance of each column,
plugin by plugin, with the lines of the file; the chain of types; the server
that answers on localhost only, to the host it is bound to.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from kitebase import inspector, introspect

BASE_MODEL = """\
types:
  Code:
    base: String
    length: 8

tables:
  Book:
    label: Book
    columns:
      - name: id
        type: Integer
        primary_key: true
        autoincrement: true

      - name: title
        type: String
        length: 80

      # the shelf code, written by the shop
      - name: code
        type: Code

pages:
  book_detail:
    title: Book
    panels:
      - id: authors
        pos: right
      - id: notes
        pos: bottom

roles:
  shop.desk:
    label: Desk
    grants: [Book.read, Loan.read]
"""

SHOP_MODEL = """\
tables:
  Book:
    columns:
      - name: title
        nullable: false
      - name: token
        type: String
        api_key: "key-91f3"

pages:
  book_detail:
    panels:
      - id: authors
        width: 320
"""


def _app(tmp_path, monkeypatch):
    import kitebase.utils
    from kitebase.db import DB
    from kitebase.plugins import PluginsManager

    for name, deps, model in (('base', [], BASE_MODEL), ('shop', ['base'], SHOP_MODEL)):
        plugin = tmp_path / 'plugins' / name
        plugin.mkdir(parents=True)
        (plugin / 'config.yaml').write_text(yaml.safe_dump(
            {'name': name, 'version': '0.0.1', 'depends_on': deps}))
        (plugin / 'model.yaml').write_text(model)
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()
    db = DB()
    db.calc_db(pm)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: db)
    return db


def _column(table, name):
    return next(c for c in table['columns'] if c['name'] == name)


def test_a_column_says_which_plugins_write_it_and_where(tmp_path, monkeypatch):
    book = introspect.table(_app(tmp_path, monkeypatch), 'Book')
    title = _column(book, 'title')
    assert [(l['plugin'], l['line'], l['end']) for l in title['layers']] == [
        ('base', 15, 17), ('shop', 4, 5)]
    assert title['layers'][1]['text'] == '      - name: title\n        nullable: false\n'
    assert title['layers'][1]['declared'] == {'name': 'title', 'nullable': False}
    # As it runs: the merge of the two
    assert title['attributes']['nullable'] is False and title['attributes']['length'] == 80
    assert book['plugins'] == ['base', 'shop']


def test_a_snippet_stops_where_the_node_ends(tmp_path, monkeypatch):
    # Neither the blank line nor the comment that introduces the next column
    title = _column(introspect.table(_app(tmp_path, monkeypatch), 'Book'), 'title')
    assert title['layers'][0]['text'].rstrip().endswith('length: 80')


def test_the_types_a_column_takes_the_rest_from(tmp_path, monkeypatch):
    code = _column(introspect.table(_app(tmp_path, monkeypatch), 'Book'), 'code')
    assert [t['name'] for t in code['types']] == ['Code', 'String']
    assert code['types'][0]['layers'][0]['plugin'] == 'base'
    assert code['types'][1]['builtin'] is True


def test_no_secret_leaves_with_a_layer(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    text = json.dumps(introspect.table(app, 'Book'), default=str)
    assert 'key-91f3' not in text
    assert 'key-91f3' not in introspect.source(app, 'shop', 'model.yaml')['text']


def test_only_the_files_of_a_plugin_are_read(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        introspect.source(app, 'shop', '../../config.yaml')


def test_everything_is_plain_json(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    for answer in (introspect.roots(app), introspect.node(app, 'tables.Book'),
                   introspect.app_summary(app), introspect.tables(app),
                   introspect.table(app, 'Book'), introspect.catalog(app), introspect.endpoints()):
        json.dumps(answer)


# ── The merged tree ──────────────────────────────────────────────────────────

def test_a_node_of_any_root_says_who_writes_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    node = introspect.node(app, 'pages.book_detail.panels[authors]')
    assert [(l['plugin'], l['line']) for l in node['layers']] == [('base', 27), ('shop', 13)]
    assert node['value'] == {'id': 'authors', 'pos': 'right', 'width': 320}
    panels = introspect.node(app, 'pages.book_detail.panels')
    assert [(c['key'], c['plugins']) for c in panels['children']] == [
        ('authors', ['base', 'shop']), ('notes', ['base'])]
    roots = {r['name']: r for r in introspect.roots(app)}
    assert roots['pages']['plugins'] == ['base', 'shop'] and roots['roles']['count'] == 1


def test_a_path_is_read_along_the_tree(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    # a key with dots in it
    desk = introspect.node(app, 'roles.shop.desk')
    assert [t['key'] for t in desk['trail']] == ['roles', 'shop.desk']
    # a position names the item when its list has an identity
    assert introspect.node(app, 'tables.Book.columns[1]')['path'] == 'tables.Book.columns[title]'
    # a list of values: the lines are those of the node that holds it
    grant = introspect.node(app, 'roles.shop.desk.grants[1]')
    assert grant['value'] == 'Loan.read' and grant['layers_of'] == 'roles.shop.desk.grants'
    assert [l['plugin'] for l in grant['layers']] == ['base']
    for wrong in ('pages.nope', 'roles.shop', 'tables.Book.columns[9]'):
        with pytest.raises(KeyError):
            introspect.node(app, wrong)


def test_a_page_comes_resolved_and_no_subject_without_a_policy(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    resolved = introspect.node(app, 'pages.book_detail')['resolved']
    # No guard registered: nobody to see it through, nothing taken out
    assert resolved['subjects'] == [] and resolved['subject'] is None
    assert [p['id'] for p in resolved['value']['panels']] == ['authors', 'notes']
    assert introspect.node(app, 'pages.book_detail.panels')['resolved'] is None
    with pytest.raises(KeyError):
        introspect.node(app, 'pages.book_detail', 'shop.desk')


def test_a_plugin_adds_to_the_nodes_of_its_root(tmp_path, monkeypatch, request):
    app = _app(tmp_path, monkeypatch)
    saved = introspect._DETAILS.snapshot()
    request.addfinalizer(lambda: introspect._DETAILS.restore(saved))
    introspect._DETAILS.clear()

    def detail(app, key):
        return [{'title': 'Seen', 'links': [{'text': key, 'path': f'roles.{key}'}],
                 'columns': ['api_key'], 'rows': [[{'text': 'x'}]], 'api_key': 'key-91f3'}]

    introspect.add_detail('roles', detail)
    introspect.add_detail('roles', detail)          # the same, again: a module loaded twice
    with pytest.raises(ValueError):                 # another module: one detail per root
        exec("introspect.add_detail('roles', lambda app, key: [])",
             {'__name__': 'other.plugin', 'introspect': introspect})
    blocks = introspect.node(app, 'roles.shop.desk')['detail']
    assert blocks[0]['links'] == [{'text': 'shop.desk', 'path': 'roles.shop.desk'}]
    assert 'key-91f3' not in json.dumps(blocks)
    assert introspect.node(app, 'roles')['detail'] is None
    assert introspect.node(app, 'roles.shop.desk.grants')['detail'] is None


def test_no_secret_leaves_with_a_node(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    text = json.dumps(introspect.node(app, 'tables.Book.columns[token]'), default=str)
    assert 'key-91f3' not in text


# ── The server ─────────────────────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    ui = tmp_path / 'ui'
    ui.mkdir()
    (ui / 'index.html').write_text('<!doctype html><title>inspector</title>')
    srv = inspector.make_server(app, port=0, ui=ui)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], app
    srv.shutdown()
    srv.server_close()


def _get(port, path, host=None):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                 headers={'Host': host or f'localhost:{port}'})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_server_answers_the_questions(server):
    port, _ = server
    status, body = _get(port, '/api/table/Book')
    assert status == 200 and json.loads(body)['name'] == 'Book'
    assert _get(port, '/api/table/Nope')[0] == 404
    assert _get(port, '/api/nothing')[0] == 404
    status, body = _get(port, '/api/node?path=roles.shop.desk')
    assert status == 200 and json.loads(body)['path'] == 'roles.shop.desk'
    assert _get(port, '/api/node?path=pages.nope')[0] == 404


def test_another_host_is_refused(server):
    # A page that rebinds its own name to 127.0.0.1 still sends its name
    port, _ = server
    assert _get(port, '/api/app', host=f'evil.example:{port}')[0] == 403
    assert _get(port, '/api/app', host='localhost:1')[0] == 403


def test_a_path_out_of_the_page_gets_the_page(server):
    port, _ = server
    status, body = _get(port, '/../../config.yaml')
    assert status == 200 and b'inspector' in body


def test_a_changed_file_makes_the_application_old(server):
    port, app = server
    assert json.loads(_get(port, '/api/version')[1])['stale'] is False
    model = app.pm.plugins['shop'].data_files[0]
    later = time.time() + 5
    os.utime(model, (later, later))
    assert json.loads(_get(port, '/api/version')[1])['stale'] is True


# ── The compiled page ──────────────────────────────────────────────────────────

def test_the_page_is_compiled_when_missing_or_older_than_its_sources(tmp_path):
    source, ui = tmp_path / 'inspector', tmp_path / 'ui'
    (source / 'src').mkdir(parents=True)
    (source / 'package.json').write_text('{}')
    (source / 'src' / 'App.svelte').write_text('')
    assert inspector.build_state(source, ui) == 'missing'
    ui.mkdir()
    page = ui / 'index.html'
    page.write_text('')
    later = time.time() + 5
    os.utime(page, (later, later))
    assert inspector.build_state(source, ui) == 'ok'
    os.utime(source / 'src' / 'App.svelte', (later + 5, later + 5))
    assert inspector.build_state(source, ui) == 'stale'
    assert inspector.build_state(tmp_path / 'none', tmp_path / 'nothing') == 'no-source'
