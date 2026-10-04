"""`$auto`: an explicit page that starts from the generated one.

Without it an explicit page replaces the generated one whole, so adding a single
navigator command meant writing every column by hand. With it the page names only
what differs, and the rest keeps following the schema.
"""
import pytest

from kitebase.diagnostics import run_checks
from kitebase.pages import load_page, strip_meta
from kitebase.plugins import PluginsManager


class FakeColumn:
    def __init__(self, name, **attributes):
        self.name = name
        self.attributes = attributes


class FakeTable:
    def __init__(self, *columns):
        self.effective_columns = list(columns)


class FakeApp:
    def __init__(self, pm, tables):
        self.pm = pm
        self.tables = tables


def archive():
    """A fresh command each time: the plugin merge stamps `$plugin` into what it gets."""
    return {'id': 'archive', 'label': 'Archive', 'scope': 'global',
            'key': 'a', 'endpoint': 'archivable'}


def shown(app, page_id):
    """The page as the client receives it: resolved, without `$plugin`."""
    return strip_meta(load_page(app, page_id))


def make_app(pages):
    pm = PluginsManager()
    pm.config = {}
    pm.merge_dicts({'pages': pages}, 'bench')
    tables = {'Author': FakeTable(FakeColumn('id'), FakeColumn('name', label='Name'),
                                  FakeColumn('active'))}
    return FakeApp(pm, tables)


def test_the_generated_page_takes_the_command():
    app = make_app({'author_list': {
        '$auto': True,
        'content': {'navigator': {'commands': [archive()]}},
    }})

    page = shown(app, 'author_list')
    content = page['content']

    # the columns still come from the schema...
    assert [c['field'] for c in content['columns']] == ['id', 'name', 'active']
    assert content['source'] == {'model': 'Author'}
    # ...and the command sits where the generated `navigator: true` was
    assert content['navigator'] == {'commands': [archive()]}
    assert '$auto' not in page and '_auto' not in page


def test_what_the_page_declares_wins():
    app = make_app({'author_list': {
        '$auto': True,
        'title': 'Authors',
        'content': {'columns': [{'field': 'name'}]},
    }})

    page = shown(app, 'author_list')
    assert page['title'] == 'Authors'
    assert [c['field'] for c in page['content']['columns']] == ['name']
    assert page['content']['navigator'] is True


def test_without_auto_the_explicit_page_replaces_the_generated_one():
    app = make_app({'author_list': {
        'content': {'navigator': {'commands': [archive()]}},
    }})

    content = shown(app, 'author_list')['content']
    assert 'columns' not in content and 'source' not in content


def test_the_declared_page_is_not_touched():
    """The merge works on a copy: the next request must find the page as written."""
    app = make_app({'author_list': {
        '$auto': True,
        'content': {'navigator': {'commands': [archive()]}},
    }})

    load_page(app, 'author_list')
    again = shown(app, 'author_list')
    assert app.pm.get('pages.author_list')['$auto'] is True
    assert [c['field'] for c in again['content']['columns']] == ['id', 'name', 'active']


def test_auto_on_an_id_no_table_answers_to_is_an_error():
    app = make_app({'writers': {'$auto': True, 'title': 'Writers'}})

    with pytest.raises(ValueError, match="writers"):
        load_page(app, 'writers')


def test_check_names_the_page_before_anyone_opens_it():
    app = make_app({
        'writers': {'$auto': True},
        'author_list': {'$auto': True},
    })

    codes = {(i['code'], i['path']) for i in run_checks(app)}
    assert ('auto-missing', 'pages.writers') in codes
    assert not any(path == 'pages.author_list' for code, path in codes
                   if code == 'auto-missing')
