"""The guide of a menu entry: one page, every field once, nothing technical."""
from pathlib import Path

import pytest
import yaml

import kitebase.utils
from kitebase.db import DB
from kitebase.help import help_merge, render_html
from kitebase.plugins import PluginsManager

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMONS = REPO_ROOT.parent / 'commons' / 'plugins'

TABLES = {
    'Owner': {'name': 'owners', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True,
         'doc': 'TECHNICAL column note'},
        {'name': 'name', 'type': 'String', 'length': 60, 'help': 'The name of the owner.'},
    ]},
    'Doc': {
        'name': 'docs',
        'help': 'A document of the bench.',
        'doc': 'TECHNICAL table note',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 60, 'help': 'What the document is about.'},
            {'name': 'silent', 'type': 'String', 'length': 60},
            {'name': 'closing', 'type': 'String', 'length': 60, 'help': 'Written at the end.'},
            {'name': 'owner_id', 'type': 'Integer', 'label': 'Owner', 'help': 'Who keeps it.',
             'foreign_key': {'target': 'Owner.id'}},
            {'name': 'closed_owner_id', 'type': 'Integer', 'label': 'Closed', 'help': 'No lookup.',
             'foreign_key': {'target': 'Owner.id'}, 'lookup': {'page': False}},
            {'name': 'grid_only', 'type': 'String', 'length': 60, 'help': 'Only in the list.'},
        ],
    },
    'Row': {'name': 'rows', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'doc_id', 'type': 'Integer', 'help': 'NEVER the key to the parent.',
         'foreign_key': {'target': 'Doc.id', 'owned': True}},
        {'name': 'owner_id', 'type': 'Integer', 'label': 'Row owner', 'help': 'Who wrote the row.',
         'foreign_key': {'target': 'Owner.id'}},
    ]},
}

PAGES = {
    'doc_list': {
        'title': 'Documents',
        'help': 'The documents.',
        'doc': 'TECHNICAL page note',
        'content': {
            'type': 'table',
            'source': {'model': 'Doc', 'joins': [{'Owner': 'Doc.owner_id = Owner.id'}]},
            'columns': [
                {'field': 'title'},
                {'field': 'grid_only'},
                {'field': 'owner_id.name', 'title': 'Owner shown'},
                {'field': 'Owner.name as owner_name', 'title': 'Owner joined'},
            ],
            'navigator': {'commands': [
                {'id': 'stamp', 'label': 'Stamp', 'help': 'Stamps it.', 'doc': 'TECHNICAL command note',
                 'scope': 'row', 'endpoint': 'stamp'},
                {'id': 'mute', 'label': 'Mute', 'scope': 'row', 'endpoint': 'mute'},
            ]},
        },
    },
    'doc_form': {
        'title': 'Document',
        'help': 'Written in two steps.',
        'content': {
            'type': 'form',
            'source': {'model': 'Doc'},
            'phases': [
                {'id': 'one', 'label': 'First', 'nodes': ['head']},
                {'id': 'two', 'label': 'Second', 'nodes': ['rows_tabs', 'foot']},
            ],
            'layout': [
                {'id': 'foot', 'type': 'section', 'fields': [{'name': 'closing'}]},
                {'id': 'head', 'type': 'section', 'fields': [
                    {'name': 'title'}, {'name': 'silent'}, {'name': 'owner_id'},
                    {'name': 'closed_owner_id'},
                    {'name': 'helper', 'label': 'Helper', 'transient': True, 'help': 'Not kept.'},
                ]},
                {'id': 'rows_tabs', 'type': 'tabs', 'pages': [{
                    'id': 'rows_tab',
                    'layout': [{
                        'type': 'collection', 'id': 'rows', 'model': 'Row', 'fk': 'doc_id',
                        'view': {'type': 'table', 'columns': [{'field': 'owner_id.name'}],
                                 'navigator': {'commands': [
                                     {'id': 'note', 'label': 'Row note', 'help': 'Notes the row.',
                                      'scope': 'global', 'endpoint': 'note'}]}},
                    }],
                }]},
            ],
        },
    },
    # Reached from the form below, with a menu entry of its own: a reference, not a merge
    'report_list': {
        'title': 'Reports',
        'help': 'NEVER merged into the guide of another entry.',
        'content': {'type': 'table', 'source': {'model': 'Owner'}, 'columns': [{'field': 'name'}]},
    },
    'with_button': {
        'title': 'With a button',
        'content': {
            'type': 'form',
            'source': {'model': 'Owner'},
            'layout': [{'type': 'button', 'label': 'Open', 'opens': {'type': 'form', 'page': 'report_list'}}],
        },
    },
}

MENU = {
    'documents': {'label': 'My documents', 'action': 'stack_push', 'panel': 'doc_list'},
    'reports': {'label': 'My reports', 'action': 'stack_push', 'panel': 'report_list'},
}


def load(path: Path, monkeypatch) -> DB:
    monkeypatch.chdir(path)
    pm = PluginsManager()
    pm.load_config('config.yaml')
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()
    app = DB()
    app.calc_db(pm)
    # No endpoint is loaded here: None keeps the commands that name one
    app.cp = None
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'bench'
    (plugin / 'help' / 'en').mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}))
    (plugin / 'pages.yaml').write_text(yaml.safe_dump({'pages': PAGES, 'menu_items': MENU}))
    (plugin / 'help' / 'en' / 'doc_form.md').write_text(
        'See [[Doc.title|the title]], [[Owner.name]], [[doc_list]] and [[gone_page]].\n\n'
        '<script>alert(1)</script>\n\n`[[doc_list]]` stays code.\n')
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'title': 'Bench app', 'plugins': ['plugins']}))
    return load(tmp_path, monkeypatch)


def fields(doc):
    return {f.key: f for f in doc.all_fields()}


def section_of(doc, key):
    return next(s.title for s in doc.all_sections() if any(f.key == key for f in s.fields))


def outline(doc):
    return [(s.title, [sub.title for sub in s.subs]) for s in doc.sections]


def test_the_title_is_the_label_of_the_menu_entry(app):
    assert help_merge(app, 'doc_list').title == 'My documents'


def test_an_unknown_page_is_refused(app):
    with pytest.raises(ValueError):
        help_merge(app, 'gone_page')


def test_the_walk_follows_the_screens_each_once(app):
    assert help_merge(app, 'doc_list').screens == ['doc_list', 'doc_form', 'row_form']


def test_the_guide_reads_in_the_order_of_screens_and_phases(app):
    assert outline(help_merge(app, 'doc_list')) == [
        ('Documents', []), ('Document', ['First', 'Second'])]


def test_the_table_opens_the_guide_and_each_screen_tells_its_own_text(app):
    doc = help_merge(app, 'doc_list')
    assert doc.intro == ['A document of the bench.']
    assert doc.sections[0].texts == ['The documents.']
    assert doc.sections[1].texts[0] == 'Written in two steps.'
    assert doc.sections[1].texts[1].startswith('See [[Doc.title')


def test_a_field_without_text_is_left_out(app):
    assert 'Doc.silent' not in fields(help_merge(app, 'doc_list'))


def test_a_field_is_told_in_its_phase_whatever_the_layout_order(app):
    doc = help_merge(app, 'doc_list')
    assert section_of(doc, 'Doc.title') == 'First'
    assert section_of(doc, 'Doc.closing') == 'Second'


def test_a_column_only_in_the_list_is_told_in_the_list(app):
    doc = help_merge(app, 'doc_list')
    assert section_of(doc, 'Doc.title') == 'First'
    assert section_of(doc, 'Doc.grid_only') == 'Documents'


def test_a_path_in_a_grid_is_its_foreign_key_and_a_join_is_not_a_field(app):
    by_key = fields(help_merge(app, 'doc_list'))
    assert by_key['Doc.owner_id'].label == 'Owner'
    assert 'Owner.name' not in by_key


def test_the_form_of_a_row_details_the_place_it_opens_from(app):
    doc = help_merge(app, 'doc_list')
    assert 'Row.doc_id' not in fields(doc)
    assert section_of(doc, 'Row.owner_id') == 'Second'


def test_a_field_no_column_holds_is_told_too(app):
    assert fields(help_merge(app, 'doc_list'))['Doc.helper'].text == 'Not kept.'


def test_commands_stay_where_they_are_and_only_with_a_text(app):
    doc = help_merge(app, 'doc_list')
    assert [c.label for c in doc.sections[0].commands] == ['Stamp']
    assert [c.label for c in doc.sections[1].subs[1].commands] == ['Row note']
    assert not doc.sections[1].commands


def test_a_page_with_its_own_menu_entry_becomes_a_reference(app):
    doc = help_merge(app, 'with_button')
    assert doc.see_also == ['report_list']
    assert 'report_list' not in doc.screens
    assert not any('NEVER' in t for s in doc.all_sections() for t in s.texts)


def test_a_screen_with_nothing_to_tell_leaves_no_heading(app):
    assert help_merge(app, 'with_button').sections == []


def test_nothing_technical_reaches_the_page(app):
    rendered = render_html(app, help_merge(app, 'doc_list'))
    assert 'TECHNICAL' not in rendered.html
    assert 'NEVER' not in rendered.html


def test_a_foreign_key_links_to_its_list_unless_the_lookup_is_closed(app):
    by_key = fields(help_merge(app, 'doc_list'))
    assert by_key['Doc.owner_id'].link == 'owner_list'
    assert by_key['Doc.closed_owner_id'].link is None


def test_links_point_inside_the_document_or_beside_it(app):
    page = render_html(app, help_merge(app, 'doc_list'))
    assert '<a href="#field-Doc.title" data-help="Doc.title">the title</a>' in page.html
    assert '<a href="#page-doc_list" data-help="doc_list">Documents</a>' in page.html
    assert '<a href="owner_list.html" data-help="Owner.name">Name</a>' in page.html
    assert page.external == ['owner_list']


def test_a_link_to_nothing_stays_text_with_a_warning(app):
    page = render_html(app, help_merge(app, 'doc_list'))
    assert 'gone_page' in page.html
    assert 'href="gone_page' not in page.html
    assert any('gone_page' in w for w in page.warnings)


def test_raw_html_in_a_text_is_shown_not_run(app):
    page = render_html(app, help_merge(app, 'doc_list')).html
    assert '<script>' not in page
    assert '&lt;script&gt;' in page


def test_a_link_inside_code_stays_code(app):
    assert '<code>[[doc_list]]</code>' in render_html(app, help_merge(app, 'doc_list')).html


def test_each_kind_of_field_explains_how_to_fill_it_once(app):
    assert help_merge(app, 'doc_list').how == ['foreign_key']


def test_the_header_carries_the_title_of_the_application(app):
    assert '<header class="app">Bench app</header>' in render_html(app, help_merge(app, 'doc_list')).html


def test_the_index_names_every_section(app):
    page = render_html(app, help_merge(app, 'doc_list')).html
    index = page[page.index('<nav'):page.index('</nav>')]
    for title in ('Documents', 'Document', 'First', 'Second', 'Filling in the fields'):
        assert f'>{title}</a>' in index


def test_an_application_replaces_a_template_and_can_extend_the_original(app):
    folder = Path(app.pm.app_root) / 'guide'
    folder.mkdir()
    (folder / 'skeleton.html').write_text(
        '{% extends "core/skeleton.html" %}{% block header %}<header>Ours</header>{% endblock %}')
    (folder / 'style.css').write_text('body { color: red; }')
    app.pm.config['help_templates'] = 'guide'
    page = render_html(app, help_merge(app, 'doc_list')).html
    assert '<header>Ours</header>' in page
    assert 'body { color: red; }' in page
    assert 'field-Doc.title' in page


def test_a_template_folder_that_is_not_there_is_named(app):
    app.pm.config['help_templates'] = 'nowhere'
    with pytest.raises(ValueError, match='nowhere'):
        render_html(app, help_merge(app, 'doc_list'))


@pytest.mark.skipif(not COMMONS.is_dir(), reason=f'devtest needs the kitebase-commons checkout at {COMMONS}')
def test_the_orders_of_devtest_read_as_one_page(monkeypatch):
    app = load(REPO_ROOT / 'devtest', monkeypatch)
    doc = help_merge(app, 'purchaseorder_list')
    assert doc.title == 'Orders'
    assert doc.screens == ['purchaseorder_list', 'purchaseorder_form', 'purchaseorderline_form']
    assert outline(doc) == [
        ('Purchase orders', []), ('Purchase order', ['Order', 'Lines', 'Footer'])]
    keys = [f.key for f in doc.all_fields()]
    assert len(keys) == len(set(keys))
    assert sorted(doc.how) == ['date', 'foreign_key']
    assert [c.label for c in doc.sections[0].commands] == ['Import from spreadsheet', 'Duplicate']
    assert [c.label for c in doc.sections[1].subs[1].commands] == [
        'Discount on lines', 'Sort by publication date']
    page = render_html(app, doc)
    assert page.warnings == []
    # What the model says for whoever develops
    assert 'primary key' not in page.html
    assert 'Not duplicated' not in page.html
    assert 'A bench for the document form' not in page.html
