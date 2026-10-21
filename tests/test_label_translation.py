"""Labels written in the YAML reach the client translated to the user's locale.

The YAML is English, the key of `_()`; a page, a menu and the column labels of
the server config pass from `translate_labels` on their way out. What a
descriptor hands to the data (a filter, a default, a choice's value) stays as
written.
"""
import pytest

import kitebase.utils
from kitebase import i18n
from kitebase.endpoint_menu import get_menu
from kitebase.endpoint_panels import get_page
from kitebase.i18n import register_translations, translate_labels
from kitebase.pages import command_label
from kitebase.plugins import PluginsManager

register_translations('xx', {
    'Partners': 'Soggetti',
    'Name': 'Nome',
    'Search': 'Cerca',
    'Organization': 'Organizzazione',
    'Archive': 'Archivia',
    'Print balance': 'Stampa saldo',
    'Masters': 'Anagrafiche',
    'Books': 'Libri',
})


@pytest.fixture
def xx():
    """The current locale is 'xx' for the test, and back after it."""
    token = i18n._locale.set('xx')
    yield
    i18n._locale.reset(token)


class FakeApp:
    def __init__(self, pm):
        self.pm = pm
        self.tables = {}


def make_app(data, plugin='plug'):
    pm = PluginsManager()
    pm.config = {'menu': {'default_root': 'main'}}
    pm.merge_dicts(data, plugin)
    return FakeApp(pm)


@pytest.fixture
def patch_app(monkeypatch):
    def _set(app):
        monkeypatch.setattr(kitebase.utils, 'get_app', lambda: app)
    return _set


def test_labels_titles_and_placeholders_are_translated(xx):
    tree = {'title': 'Partners', 'content': {'fields': [
        {'name': 'name', 'label': 'Name', 'placeholder': 'Search'}]}}
    out = translate_labels(tree)
    assert out['title'] == 'Soggetti'
    assert out['content']['fields'][0] == {'name': 'name', 'label': 'Nome', 'placeholder': 'Cerca'}
    assert tree['title'] == 'Partners'  # a copy: the merged YAML stays English


def test_a_choice_is_translated_only_with_a_value(xx):
    out = translate_labels({'choices': [{'value': 'organization', 'label': 'Organization'},
                                        {'label': 'Organization'}]})
    assert out['choices'] == [{'value': 'organization', 'label': 'Organizzazione'},
                              {'label': 'Organization'}]


def test_what_goes_to_the_data_stays_as_written(xx):
    tree = {'source': {'model': 'Book', 'query': {'filters': [['title', '=', 'Name']]}},
            'domain': {'label': 'Name'}, 'defaults': {'title': 'Name'},
            'params': {'label': 'Name'}, 'values': {'title': 'Name'}}
    assert translate_labels(tree) == tree


def test_without_a_translation_the_english_stays(xx):
    assert translate_labels({'label': 'Untranslated'}) == {'label': 'Untranslated'}


def test_english_is_the_key():
    assert translate_labels({'label': 'Name'}) == {'label': 'Name'}


def test_command_label_spells_the_id():
    assert command_label('print_balance') == 'Print balance'
    assert command_label('print__balance_') == 'Print balance'
    assert command_label('print') == 'Print'


def test_get_page_translates_and_names_the_commands(patch_app, xx):
    patch_app(make_app({'pages': {'partner_list': {
        'title': 'Partners',
        'content': {'type': 'table', 'source': {'model': 'Partner'},
                    'columns': [{'field': 'name', 'title': 'Name'}],
                    'navigator': {'commands': [
                        {'id': 'archive', 'label': 'Archive'},
                        {'id': 'print_balance'}]}}}}}))
    res = get_page({'id': 'partner_list'})
    page = res['data']
    assert page['title'] == 'Soggetti'
    assert page['content']['columns'][0]['title'] == 'Nome'
    assert [c['label'] for c in page['content']['navigator']['commands']] == \
        ['Archivia', 'Stampa saldo']
    assert page['content']['source'] == {'model': 'Partner'}


def test_the_menu_is_translated_and_sorted_by_what_the_user_reads(patch_app, xx):
    patch_app(make_app({
        'menus': {'main': {'label': 'Masters'}},
        'menu_items': {
            'books': {'label': 'Books', 'action': 'stack_push', 'panel': 'book_list'},
            'search': {'label': 'Search', 'action': 'stack_push', 'panel': 'search'},
        }}))
    data = get_menu({'id': 'main'})['data']
    assert data['label'] == 'Anagrafiche'
    # Books before Search in English, Cerca before Libri in Italian.
    assert [n['label'] for n in data['items']] == ['Cerca', 'Libri']
