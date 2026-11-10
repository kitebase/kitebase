"""What a descriptor becomes on its way to the client (kitebase.outbound).

One table says what happens to each key: labels are translated, data passes as
written, metadata and what is written for whoever develops are dropped.
"""
import pytest

from kitebase import i18n, outbound
from kitebase.i18n import register_translations
from kitebase.outbound import to_client

register_translations('xx', {'Name': 'Nome', 'Partners': 'Soggetti', 'Organization': 'Organizzazione'})


@pytest.fixture
def xx():
    token = i18n._locale.set('xx')
    yield
    i18n._locale.reset(token)


@pytest.fixture
def policies():
    """The table as it was before the test, whatever the test declares."""
    saved = dict(outbound._policies)
    yield
    outbound._policies.clear()
    outbound._policies.update(saved)


def test_labels_are_translated(xx):
    out = to_client({'title': 'Partners', 'fields': [{'name': 'name', 'label': 'Name'}]})
    assert out == {'title': 'Soggetti', 'fields': [{'name': 'name', 'label': 'Nome'}]}


def test_data_passes_as_written(xx):
    tree = {'source': {'defaults': {'title': 'Name'}}, 'params': {'label': 'Name'}}
    assert to_client(tree) == tree


def test_doc_never_leaves_at_any_depth():
    tree = {
        'doc': 'Why the page is built this way.',
        'content': {'layout': [{'columns': [{'fields': [
            {'name': 'kind', 'doc': 'A closed set owned by the code.', 'label': 'Kind'}]}]}]},
    }
    assert to_client(tree) == {
        'content': {'layout': [{'columns': [{'fields': [{'name': 'kind', 'label': 'Kind'}]}]}]},
    }


def test_doc_inside_data_is_a_value():
    # A column called `doc` in a default is data: the policy judges keys of a
    # descriptor, not what the descriptor hands to a query or a record.
    tree = {'source': {'defaults': {'doc': 'contract.pdf'}}}
    assert to_client(tree) == tree


def test_plugin_attribution_is_dropped_also_inside_data():
    tree = {'$plugin': 'libapp', 'source': {'domain': [{'$plugin': 'libapp', 'field': 'x'}]}}
    assert to_client(tree) == {'source': {'domain': [{'field': 'x'}]}}


def test_a_choice_label_is_translated_only_with_a_value(xx):
    out = to_client({'choices': [{'value': 'organization', 'label': 'Organization'},
                                 {'label': 'Organization'}, 'Name']})
    assert out == {'choices': [{'value': 'organization', 'label': 'Organizzazione'},
                               {'label': 'Organization'}, 'Name']}


def test_what_is_not_json_becomes_none():
    assert to_client({'model': object(), 'items': [object(), 1]}) == {'model': None, 'items': [None, 1]}


def test_the_input_is_not_touched(xx):
    tree = {'label': 'Name', 'doc': 'x', 'fields': [{'label': 'Name'}]}
    to_client(tree)
    assert tree == {'label': 'Name', 'doc': 'x', 'fields': [{'label': 'Name'}]}


def test_a_plugin_declares_a_key_for_the_server(policies):
    outbound.set_policy('internal_note', outbound.SERVER)
    assert to_client({'label': 'A', 'internal_note': 'b'}) == {'label': 'A'}


def test_an_unknown_policy_is_refused(policies):
    with pytest.raises(ValueError, match='unknown policy'):
        outbound.set_policy('x', 'hidden')


# ── The ports ───────────────────────────────────────────────────────────────
# Every port that sends a descriptor goes through the gate, and the dump, which
# is for whoever develops, keeps what the gate drops.

def _app(tmp_path, monkeypatch):
    """A one-plugin app whose table, column, type, schema and view carry a `doc`."""
    import yaml
    import kitebase.utils
    from kitebase.app import App
    from kitebase.plugins import PluginsManager

    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({
        'types': {'Code': {'base': 'String', 'length': 8, 'doc': 'type doc'}},
        'schemas': {'Total': {'doc': {'type': 'String'}, 'amount': {'type': 'Integer', 'doc': 'field doc'}}},
        'tables': {'Book': {'doc': 'table doc', 'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'code', 'type': 'Code', 'label': 'Code', 'doc': 'column doc'}]}},
        'views': {'book_form': {'type': 'form', 'doc': 'view doc', 'source': {'model': 'Book'},
                                'layout': [{'type': 'fields', 'fields': [{'name': 'code', 'doc': 'field doc'}]}]}},
        'pages': {'book': {'title': 'Book', 'doc': 'page doc', 'content': {'$ref': 'views.book_form'}}},
        'menus': {'main': {'label': 'Main', 'doc': 'menu doc'}},
        'menu_items': {'books': {'label': 'Books', 'doc': 'item doc', 'action': 'stack_push', 'panel': 'book'}},
    }))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins'], 'menu': {'default_root': 'main'}}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    db = App()
    db.calc_db(pm)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: db)
    return db


def _docs(obj, path=''):
    """Where a `doc` key is, at any depth."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items()
                for p in ([f'{path}.{k}'] if k == 'doc' else []) + _docs(v, f'{path}.{k}')]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _docs(v, f'{path}[{i}]')]
    return []


def test_no_port_sends_doc(tmp_path, monkeypatch):
    from kitebase.endpoint_db import get_server_config
    from kitebase.endpoint_menu import get_menu
    from kitebase.endpoint_panels import get_page

    _app(tmp_path, monkeypatch)
    page = get_page({'id': 'book'})
    menu = get_menu({'id': 'main'})
    config = get_server_config({})
    assert [r['status'] for r in (page, menu, config)] == ['success'] * 3
    assert _docs(page['data']) == []
    assert _docs(menu['data']) == []
    # A schema field named `doc` is a name, not the key: it stays.
    assert _docs(config['data']) == ['.schemas.Total.doc']


def test_the_dump_keeps_doc(tmp_path, monkeypatch):
    from kitebase.diagnostics import dump_app

    dump = dump_app(_app(tmp_path, monkeypatch))
    found = _docs(dump)
    for where in ('.types.Code.doc', '.views.book_form.doc', '.pages.book.doc', '.menu_items.books.doc'):
        assert where in found
    assert any(p.startswith('.tables.Book') for p in found)
