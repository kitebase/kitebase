"""The guide of a menu entry: one page, every field once, nothing technical."""
from pathlib import Path

import pytest
import yaml

import kitebase.utils
from kitebase import access
from kitebase.app import App
from kitebase.endpoint_help import get_help
from kitebase.help import (BESIDE, INSIDE, SELECTING, guide_root, help_manual, help_merge,
                           render_html, render_manual)
from kitebase.i18n import use_locale
from kitebase.outbound import SERVER, policy_of
from kitebase.plugins import PluginsManager

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMONS = REPO_ROOT.parent / 'commons' / 'plugins'

TYPES = {
    'Code': {'base': 'String', 'length': 10, 'label': 'Codes', 'help_how': 'Letters and digits.'},
    'SubCode': {'base': 'Code'},
}

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
            {'name': 'code', 'type': 'SubCode', 'help': 'The code of the document.'},
            {'name': 'other_code', 'type': 'Code', 'help': 'Another code.'},
            {'name': 'wheel', 'type': 'String', 'length': 7, 'label': 'Colour',
             'help_how': 'Click the wheel.'},
            {'name': 'own_code', 'type': 'Code', 'help_how': 'Only digits here.'},
            {'name': 'scan', 'type': 'String', 'length': 255, 'media': 'file', 'root': 'r',
             'accept': ['pdf', 'jpg'], 'max_size': '20MB'},
            {'name': 'loose', 'type': 'String', 'length': 255, 'media': 'file', 'root': 'r'},
        ],
    },
    # Part of the document
    'Row': {'name': 'rows', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'doc_id', 'type': 'Integer', 'help': 'NEVER the key to the parent.',
         'foreign_key': {'target': 'Doc.id', 'owned': True}},
        {'name': 'owner_id', 'type': 'Integer', 'label': 'Row owner', 'help': 'Who wrote the row.',
         'foreign_key': {'target': 'Owner.id'}},
    ]},
    # A life of its own
    'Note': {'name': 'notes', 'help': 'What someone left on a document.', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'doc_id', 'type': 'Integer', 'foreign_key': {'target': 'Doc.id', 'owned': False}},
        {'name': 'body', 'type': 'String', 'length': 60, 'help': 'The words of the note.'},
    ]},
    # Only a link
    'DocOwner': {'name': 'doc_owners', 'columns': [
        {'name': 'share', 'type': 'String', 'length': 60, 'help': 'How much of it is theirs.'},
    ], 'many_to_many': {
        'target1': {'table': 'Doc.id', 'column': 'doc_id'},
        'target2': {'table': 'Owner.id', 'column': 'owner_id'},
    }},
}

NOTES = {
    'type': 'collection', 'id': 'notes', 'model': 'Note', 'fk': 'doc_id',
    'view': {'type': 'table', 'columns': [{'field': 'body'}], 'navigator': {'commands': [
        {'id': 'print', 'label': 'Print note', 'help': 'Prints the note.', 'scope': 'row',
         'endpoint': 'print_note'},
    ]}},
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
                    {'name': 'closed_owner_id'}, {'name': 'code'}, {'name': 'other_code'},
                    {'name': 'scan'}, {'name': 'loose'}, {'name': 'wheel'}, {'name': 'own_code'},
                    {'name': 'in_form', 'label': 'In the form', 'type': 'String', 'transient': True,
                     'help_how': 'Said by the form.'},
                    {'name': 'when', 'label': 'When', 'type': 'DateTime', 'transient': True},
                    {'name': 'helper', 'label': 'Helper', 'transient': True, 'help': 'Not kept.'},
                ]},
                {'id': 'rows_tabs', 'type': 'tabs', 'pages': [
                    {'id': 'rows_tab', 'layout': [{
                        'type': 'collection', 'id': 'rows', 'model': 'Row', 'fk': 'doc_id',
                        'view': {'type': 'table', 'columns': [{'field': 'owner_id.name'}],
                                 'navigator': {'commands': [
                                     {'id': 'note', 'label': 'Row note', 'help': 'Notes the row.',
                                      'scope': 'global', 'endpoint': 'note'}]}},
                    }]},
                    {'id': 'notes_tab', 'label': 'Notes', 'layout': [NOTES]},
                    {'id': 'links_tab', 'label': 'Co-owners', 'layout': [{
                        'type': 'collection', 'id': 'links', 'model': 'DocOwner', 'fk': 'doc_id',
                        'view': {'type': 'table', 'columns': [{'field': 'share'}]},
                    }]},
                ]},
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
    # The dependent parts of a list, placed by their trigger
    'owner_list': {
        'title': 'Owners',
        'help': 'The owners.',
        'content': {
            'type': 'table',
            'source': {'model': 'Owner'},
            'columns': [{'field': 'name'}],
            'navigator': True,
            'actions': {'row': [
                {'id': 'edit', 'action': 'stack_push', 'panel': 'owner_detail', 'help': 'Opens it.'},
            ]},
        },
        'panels': [
            {'id': 'below', 'pos': 'bottom', 'type': 'tabs', 'trigger': {'event': 'row_click'}, 'tabs': [
                {'id': 'docs', 'label': 'Their documents', 'type': 'table', 'source': {'model': 'Doc'},
                 'help': 'What they keep.', 'columns': [{'field': 'grid_only'}]},
                {'id': 'same', 'label': 'Detail', 'type': 'form', 'source': {'model': 'Owner'},
                 'fields': [{'name': 'name'}]},
            ]},
            {'id': 'side', 'pos': 'right', 'type': 'plugin', 'label': 'Side', 'help': 'Always there.'},
        ],
    },
    'owner_detail': {
        'title': 'Owner detail',
        'help': 'The owner with more room.',
        'content': {'type': 'form', 'source': {'model': 'Owner'}, 'fields': [{'name': 'name'}]},
    },
    'doc_faces': {
        'title': 'With a face',
        'content': {
            'type': 'form',
            'source': {'model': 'Doc'},
            'layout': [
                {'type': 'button', 'label': 'More', 'opens': {'type': 'form', 'page': 'doc_more'}},
                {'type': 'button', 'label': 'Owner', 'help': 'Who keeps it, on its form.',
                 'opens': {'type': 'form', 'page': 'owner_detail'}},
                {'type': 'button', 'label': 'Count', 'help': 'Counts them.', 'endpoint': 'count'},
                {'type': 'button', 'label': 'Silent', 'endpoint': 'count'},
            ],
        },
    },
    'doc_more': {
        'title': 'Doc more',
        'help': 'The same document, other fields.',
        'content': {'type': 'form', 'source': {'model': 'Doc'}, 'fields': [{'name': 'closing'}]},
    },
    'doc_overrides': {
        'title': 'Overrides',
        'content': {
            'type': 'form',
            'source': {'model': 'Doc'},
            'layout': [
                {'type': 'tabs', 'id': 't', 'pages': [
                    {'id': 'a', 'label': 'Forced in', 'layout': [{**NOTES, 'help_embed': True}]},
                    {'id': 'b', 'label': 'Forced out', 'layout': [{
                        'type': 'collection', 'id': 'rows', 'model': 'Row', 'fk': 'doc_id',
                        'help_embed': False,
                        'view': {'type': 'table', 'columns': [{'field': 'owner_id.name'}]},
                    }]},
                ]},
            ],
        },
    },
    'by_hand': {
        'title': 'By hand',
        'help_compose': False,
        'content': {'type': 'form', 'source': {'model': 'Doc'}, 'fields': [{'name': 'title'}]},
    },
    'by_hand_empty': {
        'title': 'By hand, no text',
        'help_compose': False,
        'content': {'type': 'form', 'source': {'model': 'Owner'}, 'fields': [{'name': 'name'}]},
    },
}

MENU = {
    'documents': {'label': 'My documents', 'action': 'stack_push', 'panel': 'doc_list'},
    'reports': {'label': 'My reports', 'action': 'stack_push', 'panel': 'report_list'},
}


@pytest.fixture(autouse=True)
def open_guard():
    # devtest loads the acl plugin, which registers its guard for good
    yield
    access._GUARD = access.OPEN


def load(path: Path, monkeypatch) -> App:
    monkeypatch.chdir(path)
    pm = PluginsManager()
    pm.load_config('config.yaml')
    pm.load_plugins()
    app = App()
    app.calc_db(pm)
    # No endpoint is loaded here: None keeps the commands that name one
    app.dispatcher = None
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'bench'
    (plugin / 'help' / 'en').mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'types': TYPES, 'tables': TABLES}))
    (plugin / 'pages.yaml').write_text(yaml.safe_dump(
        {'pages': PAGES, 'menus': {'main': {'label': 'Bench'}}, 'menu_items': MENU}))
    (plugin / 'help' / 'en' / 'doc_form.md').write_text(
        'See [[Doc.title|the title]], [[Owner.name]], [[doc_list]] and [[gone_page]].\n\n'
        '<script>alert(1)</script>\n\n`[[doc_list]]` stays code.\n')
    (plugin / 'help' / 'en' / 'by_hand.md').write_text('Written by hand.\n')
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'title': 'Bench app', 'plugins': ['plugins']}))
    return load(tmp_path, monkeypatch)


def fields(doc):
    return {f.key: f for f in doc.all_fields()}


def group_of(doc, key):
    return next(g.title for g in doc.groups if any(f.key == key for f in g.fields))


def group_named(doc, title):
    return next(g for g in doc.groups if g.title == title)


def items(doc, part):
    return {i.title: i for i in doc.parts.get(part, [])}


def labels(commands):
    return [c.label for c in commands]


def test_the_title_is_the_label_of_the_menu_entry(app):
    assert help_merge(app, 'doc_list').title == 'My documents'


def test_an_unknown_page_is_refused(app):
    with pytest.raises(ValueError):
        help_merge(app, 'gone_page')


def test_the_walk_follows_the_screens_each_once(app):
    assert help_merge(app, 'doc_list').screens == ['doc_list', 'doc_form', 'row_form', 'docowner_form']


def test_the_introduction_says_what_the_table_and_the_screens_are(app):
    intro = help_merge(app, 'doc_list').intro
    assert intro[:3] == ['A document of the bench.', 'The documents.', 'Written in two steps.']
    assert intro[3].startswith('See [[Doc.title')


def test_the_commands_of_the_list_stand_on_their_own_and_only_with_a_text(app):
    assert labels(help_merge(app, 'doc_list').commands) == ['Stamp']


def test_a_field_without_text_is_listed_all_the_same(app):
    doc = help_merge(app, 'doc_list')
    assert fields(doc)['Doc.silent'].text == ''
    page = render_html(app, doc).html
    assert '<dt id="field-Doc.silent">Silent</dt>\n<dt' in page


def test_the_key_of_a_table_is_listed_only_with_a_text(app):
    by_key = fields(help_merge(app, 'doc_list'))
    assert 'Doc.id' not in by_key
    by_key = fields(help_merge(app, 'row_list'))
    assert 'Row.id' not in by_key


def test_a_hidden_column_of_a_list_is_not_a_field(app):
    app.plugins.get('pages')['doc_list']['content']['columns'].append({'field': 'closing', 'visible': False})
    app.plugins.get('pages')['doc_list']['content']['navigator'] = {'form_id': 'owner_detail'}
    assert 'Doc.closing' not in fields(help_merge(app, 'doc_list'))


def test_the_fields_read_in_the_order_of_the_phases_whatever_the_layout(app):
    doc = help_merge(app, 'doc_list')
    assert group_of(doc, 'Doc.title') == 'First'
    assert group_of(doc, 'Doc.closing') == 'Second'
    titled = [g.title for g in doc.groups if g.title]
    assert titled.index('First') < titled.index('Second')


def test_a_column_only_in_the_list_has_no_group(app):
    assert group_of(help_merge(app, 'doc_list'), 'Doc.grid_only') is None


def test_a_path_in_a_grid_is_its_foreign_key_and_a_join_is_not_a_field(app):
    by_key = fields(help_merge(app, 'doc_list'))
    assert by_key['Doc.owner_id'].label == 'Owner'
    assert 'Owner.name' not in by_key


def test_a_field_no_column_holds_is_told_too(app):
    assert fields(help_merge(app, 'doc_list'))['Doc.helper'].text == 'Not kept.'


def test_an_owned_table_is_told_with_the_record_and_its_commands_with_it(app):
    doc = help_merge(app, 'doc_list')
    assert 'Row.doc_id' not in fields(doc)
    assert group_of(doc, 'Row.owner_id') == 'Second'
    assert labels(group_named(doc, 'Second').commands) == ['Row note']


def test_a_table_that_only_links_is_told_with_the_record(app):
    doc = help_merge(app, 'doc_list')
    assert group_of(doc, 'DocOwner.share') == 'Co-owners'


def test_a_table_with_a_life_of_its_own_is_a_reference_with_its_local_commands(app):
    doc = help_merge(app, 'doc_list')
    assert 'Note.body' not in fields(doc)
    note = items(doc, INSIDE)['Notes']
    assert note.ref == 'note_list'
    assert note.text == 'What someone left on a document.'
    assert labels(note.commands) == ['Print note']


def test_help_embed_overrides_the_rule_both_ways(app):
    doc = help_merge(app, 'doc_overrides')
    assert group_of(doc, 'Note.body') == 'Forced in'
    assert labels(group_named(doc, 'Forced in').commands) == ['Print note']
    assert 'Row.owner_id' not in fields(doc)
    assert items(doc, INSIDE)['Forced out'].ref == 'row_list'


def test_a_page_on_the_same_model_is_a_face_of_the_record(app):
    doc = help_merge(app, 'doc_faces')
    face = group_named(doc, 'More')
    assert face.texts == ['The same document, other fields.']
    assert [f.key for f in face.fields] == ['Doc.closing']


def test_a_button_to_another_model_is_a_reference_with_its_text(app):
    owner = items(help_merge(app, 'doc_faces'), INSIDE)['Owner']
    assert owner.ref == 'owner_detail'
    assert owner.text == 'Who keeps it, on its form.'


def test_a_button_that_calls_is_told_only_with_a_text(app):
    inside = items(help_merge(app, 'doc_faces'), INSIDE)
    assert inside['Count'].text == 'Counts them.'
    assert 'Silent' not in inside


def test_a_page_with_its_own_menu_entry_becomes_a_reference(app):
    doc = help_merge(app, 'with_button')
    assert items(doc, INSIDE)['Open'].ref == 'report_list'
    assert 'report_list' not in doc.screens
    assert not any('NEVER' in t for t in doc.intro)


def test_a_row_action_leads_to_the_screen_it_opens(app):
    doc = help_merge(app, 'owner_list')
    assert 'owner_detail' in doc.screens
    assert labels(doc.commands) == ['Edit']
    assert 'The owner with more room.' in doc.intro


def test_a_panel_says_what_it_depends_on(app):
    doc = help_merge(app, 'owner_list')
    selecting = items(doc, SELECTING)
    assert selecting['Their documents'].text == 'What they keep.'
    assert selecting['Their documents'].ref == 'doc_list'
    assert items(doc, BESIDE)['Side'].text == 'Always there.'


def test_a_panel_on_the_same_record_points_to_its_fields(app):
    assert items(help_merge(app, 'owner_list'), SELECTING)['Detail'].anchor == 'fields'


def test_a_panel_on_another_table_does_not_tell_its_fields(app):
    assert 'Doc.grid_only' not in fields(help_merge(app, 'owner_list'))


def test_help_compose_false_leaves_the_written_text_alone(app):
    doc = help_merge(app, 'by_hand')
    assert doc.intro == ['Written by hand.']
    assert not doc.groups and not doc.how
    assert doc.warnings == []


def test_help_compose_false_without_a_text_is_said(app):
    assert any('by_hand_empty' in w for w in help_merge(app, 'by_hand_empty').warnings)


def test_help_compose_and_help_embed_stay_on_the_server():
    assert policy_of('help_compose') == SERVER
    assert policy_of('help_embed') == SERVER


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
    assert '<a href="#top" data-help="doc_list">Documents</a>' in page.html
    assert '<a href="owner_list.html" data-help="Owner.name">Name</a>' in page.html
    assert '<a href="note_list.html" data-help="note_list">Notes</a>' in page.html
    assert page.external == ['note_list', 'owner_list']


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
    assert [h.key for h in help_merge(app, 'doc_list').how] == [
        'FK', 'Code', 'wheel', 'own_code', 'in_form', 'DateTime']


def test_how_to_fill_comes_from_the_nearest_type_that_says_it(app):
    how = next(h for h in help_merge(app, 'doc_list').how if h.key == 'Code')
    assert (how.title, how.text) == ('Codes', 'Letters and digits.')


def test_a_column_says_how_it_is_filled_in(app):
    how = {h.key: h for h in help_merge(app, 'doc_list').how}
    assert (how['wheel'].title, how['wheel'].text) == ('Colour', 'Click the wheel.')


def test_the_column_wins_over_its_type(app):
    how = {h.key: h for h in help_merge(app, 'doc_list').how}
    assert how['own_code'].text == 'Only digits here.'


def test_a_field_says_how_it_is_filled_in(app):
    how = {h.key: h for h in help_merge(app, 'doc_list').how}
    assert how['in_form'].title == 'In the form'


def test_the_types_of_the_core_say_it_without_any_yaml(app):
    how = {h.key: h for h in help_merge(app, 'doc_list').how}
    assert how['FK'].title == 'Choosing from another table'
    assert how['DateTime'].title == 'Dates'


def test_help_how_is_not_the_help_of_the_field(app):
    assert fields(help_merge(app, 'doc_list'))['Doc.code'].text == 'The code of the document.'


def test_help_how_stays_on_the_server():
    assert policy_of('help_how') == SERVER


def test_a_media_field_says_what_it_takes(app):
    doc = help_merge(app, 'doc_list')
    assert fields(doc)['Doc.scan'].limits == 'pdf, jpg, up to 20 MB'
    assert fields(doc)['Doc.title'].limits == ''
    assert '<p class="limits">pdf, jpg, up to 20 MB</p>' in render_html(app, doc).html


def test_a_media_field_without_limits_says_nothing(app):
    doc = help_merge(app, 'doc_list')
    assert fields(doc)['Doc.loose'].limits == ''
    assert '<dt id="field-Doc.loose">Loose</dt>\n<dt' in render_html(app, doc).html


def test_the_header_carries_the_title_of_the_application(app):
    assert '<header class="app">Bench app</header>' in render_html(app, help_merge(app, 'doc_list')).html


def test_the_index_names_every_part(app):
    page = render_html(app, help_merge(app, 'doc_list')).html
    index = page[page.index('<nav'):page.index('</nav>')]
    for title in ('Commands', 'Fields', 'First', 'Second', 'Inside the form', 'Filling in the fields'):
        assert f'>{title}</a>' in index


def test_an_application_replaces_a_template_and_can_extend_the_original(app):
    folder = Path(app.plugins.app_root) / 'guide'
    folder.mkdir()
    (folder / 'skeleton.html').write_text(
        '{% extends "core/skeleton.html" %}{% block header %}<header>Ours</header>{% endblock %}')
    (folder / 'style.css').write_text('body { color: red; }')
    app.plugins.config['help_templates'] = 'guide'
    page = render_html(app, help_merge(app, 'doc_list')).html
    assert '<header>Ours</header>' in page
    assert 'body { color: red; }' in page
    assert 'field-Doc.title' in page


def test_a_template_folder_that_is_not_there_is_named(app):
    app.plugins.config['help_templates'] = 'nowhere'
    with pytest.raises(ValueError, match='nowhere'):
        render_html(app, help_merge(app, 'doc_list'))


@pytest.mark.skipif(not COMMONS.is_dir(), reason=f'devtest needs the kitebase-commons checkout at {COMMONS}')
def test_the_orders_of_devtest_read_as_one_page(monkeypatch):
    app = load(REPO_ROOT / 'devtest', monkeypatch)
    doc = help_merge(app, 'purchaseorder_list')
    assert doc.title == 'Orders'
    assert doc.screens == ['purchaseorder_list', 'purchaseorder_form', 'purchaseorderline_form']
    assert [g.title for g in doc.groups] == ['Order', 'Lines', 'Footer']
    keys = [f.key for f in doc.all_fields()]
    assert len(keys) == len(set(keys))
    assert sorted(h.key for h in doc.how) == ['Date', 'FK']
    assert labels(doc.commands) == ['Import from spreadsheet', 'Duplicate']
    assert labels(group_named(doc, 'Lines').commands) == ['Discount on lines', 'Sort by publication date']
    assert not doc.parts
    page = render_html(app, doc)
    assert page.warnings == []
    # What the model says for whoever develops
    assert 'primary key' not in page.html
    assert 'Not duplicated' not in page.html
    assert 'A bench for the document form' not in page.html


@pytest.mark.skipif(not COMMONS.is_dir(), reason=f'devtest needs the kitebase-commons checkout at {COMMONS}')
def test_the_loans_of_a_book_are_a_reference(monkeypatch):
    app = load(REPO_ROOT / 'devtest', monkeypatch)
    doc = help_merge(app, 'book_list')
    assert items(doc, INSIDE)['Loans'].ref == 'loan_manager'
    assert 'Loan.due_date' not in fields(doc)
    assert group_of(doc, 'BookAuthor.notes') == 'Authors'
    assert {'Image', 'File'} <= {h.key for h in doc.how}
    assert fields(doc)['Book.sample'].limits == 'pdf, up to 20 MB'
    by_author = help_merge(app, 'author_list')
    assert 'Files' in {h.key for h in by_author.how}
    assert fields(by_author)['Author.documents'].limits.endswith('up to 20 MB')


# ── In the application: get_help, the root of a stack, the user's view ──────

class Denying(access.Guard):
    def __init__(self, denied=()):
        self.denied = set(denied)

    def allows(self, permission, context):
        return permission not in self.denied

    def allows_undeclared(self, endpoint, context):
        return True


@pytest.fixture
def served(app, monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: app)

    def deny(*permissions):
        access._GUARD = access.OPEN
        access.set_guard(Denying(permissions))
    yield deny
    access._GUARD = access.OPEN


def test_the_guide_of_a_stack_is_the_one_that_tells_its_top(app):
    assert guide_root(app, ['doc_list', 'doc_form']) == 'doc_list'
    assert guide_root(app, ['doc_faces', 'doc_more']) == 'doc_faces'
    # A page reached from the record that is not one of its screens
    assert guide_root(app, ['doc_list', 'owner_detail']) == 'owner_detail'
    # A page with a menu entry of its own is a reference, not a screen
    assert guide_root(app, ['with_button', 'report_list']) == 'report_list'
    assert guide_root(app, ['doc_list', 'nowhere', 'doc_form']) == 'doc_form'


def test_get_help_opens_where_the_user_is(served):
    result = get_help({'stack': ['doc_list', 'doc_form'], 'focus': 'Doc.title'})
    assert result['status'] == 'success'
    assert result['data']['root'] == 'doc_list'
    assert result['data']['title'] == 'My documents'
    assert result['data']['anchor'] == 'field-Doc.title'
    assert 'id="field-Doc.title"' in result['data']['html']
    # Without a focus, the screen on top of the stack
    assert get_help({'stack': ['doc_list', 'doc_form']})['data']['anchor'] == 'fields'


def test_get_help_follows_a_link_of_a_guide(served):
    result = get_help({'ref': 'Doc.title'})
    assert result['data']['root'] == 'doc_list'
    assert result['data']['anchor'] == 'field-Doc.title'
    assert get_help({'ref': 'report_list'})['data']['root'] == 'report_list'
    assert get_help({'ref': 'Doc.nothing'})['code'] == 404


def test_get_help_asks_for_something_to_tell(served):
    assert get_help({})['code'] == 400
    assert get_help({'stack': 'doc_list'})['code'] == 400


def test_get_help_asks_what_opening_the_page_asks(served):
    served('Doc.read')
    with pytest.raises(access.AccessDenied):
        get_help({'stack': ['doc_list']})


def test_the_guide_of_a_user_leaves_out_what_they_may_not_open(app, served):
    served('Owner.read')
    page = get_help({'stack': ['doc_list']})['data']['html']
    # The foreign key and the page with a menu entry on owners stay text
    assert 'data-help="owner_list"' not in page
    # The command line tells everything
    complete = render_html(app, help_merge(app, 'doc_list')).html
    assert 'data-help="owner_list"' in complete
    # A button to a page they may not open is not even mentioned
    assert 'Open' in items(help_merge(app, 'with_button'), INSIDE)
    assert not help_merge(app, 'with_button', for_user=True).parts


def test_the_guide_is_in_the_language_of_the_request(served):
    with use_locale('it'):
        page = get_help({'stack': ['doc_list']})['data']['html']
    assert '<html lang="it">' in page


# ── The manual ──────────────────────────────────────────────────────────────

@pytest.fixture
def written(app, tmp_path):
    """Texts of the manual: the application's, and a plugin's beside them."""
    root = tmp_path / 'help' / 'en'
    (root / 'appendix').mkdir(parents=True)
    (root / 'index.md').write_text('About this bench. See [[appendix/keys|the keys]].\n')
    (root / 'appendix' / 'keys.md').write_text('# The keys\n\n| Key | Does |\n|---|---|\n| F1 | help |\n')
    (root / 'menu.documents.md').write_text('Added by the application.\n')
    plugin = tmp_path / 'plugins' / 'bench' / 'help' / 'en'
    (plugin / 'menu.documents.md').write_text('Written by the plugin.\n')
    return app


def test_the_manual_follows_the_menu_with_a_card_for_each_entry(written):
    manual = help_manual(written)
    assert [s.title for s in manual.sections] == ['My documents', 'My reports']
    assert [s.doc.root for s in manual.sections] == ['doc_list', 'report_list']
    assert manual.intro[0].startswith('About this bench')
    # A plugin and the application both add to the introduction of an entry
    assert manual.sections[0].texts == ['Written by the plugin.', 'Added by the application.']
    assert [a.title for a in manual.appendices] == ['The keys']


def test_the_pages_the_cards_lead_to_are_told_at_the_end(written):
    manual = help_manual(written)
    # The foreign keys of the documents lead to the owners, which no entry opens
    assert 'owner_list' in [d.root for d in manual.others]


def test_the_manual_is_one_file_its_links_anchors_inside_it(written):
    page = render_manual(written, help_manual(written), today='2026-11-07').html
    assert 'id="doc_list-top"' in page and 'id="report_list-top"' in page
    assert 'id="doc_list-field-Doc.title"' in page
    assert 'href="#owner_list-top"' in page
    assert '<a href="#appendix-keys">the keys</a>' in page
    assert '<table>' in page
    assert '.html"' not in page
    assert '2026-11-07' in page


def test_a_screen_is_told_once_in_the_manual(written):
    # A second entry on the same page points to the first card
    written.plugins.data['menu_items']['again'] = {'label': 'Zeta, again', 'action': 'stack_push',
                                              'panel': 'doc_list'}
    manual = help_manual(written)
    again = next(s for s in manual.sections if s.title == 'Zeta, again')
    assert again.doc is None and again.ref == 'doc_list'
    # A screen another card tells is a reference, like a page with an entry
    doc = help_merge(written, 'doc_list', told={'doc_form'})
    assert 'doc_form' not in doc.screens and doc.see_also == ['doc_form']


def test_the_manual_of_a_user_holds_what_they_may_open(written, served):
    served('Owner.read')
    manual = help_manual(written, for_user=True)
    assert [s.title for s in manual.sections] == ['My documents']
    assert 'owner_list' not in [d.root for d in manual.others]
    # The appendices are for everyone
    assert [a.title for a in manual.appendices] == ['The keys']


def test_the_home_has_the_head_of_the_manual(written, served):
    result = get_help({'stack': []})
    page = result['data']['html']
    assert result['status'] == 'success'
    assert 'About this bench.' in page
    # Each entry leads to its guide, which the client asks for
    assert 'data-help="doc_list"' in page
    assert 'id="doc_list-top"' not in page


def test_the_manual_downloads_as_one_page(written, served):
    from kitebase.endpoint_help import help_manual_file
    result = help_manual_file({})
    assert result.mime.startswith('text/html') and result.disposition == 'inline'
    assert b'id="doc_list-top"' in result.content


def test_an_application_stylesheet_extends_the_core(written, tmp_path):
    folder = tmp_path / 'help' / 'templates'
    folder.mkdir()
    (folder / 'style.css').write_text('{% include "core/style.css" %}\nh1 { color: red; }\n')
    written.plugins.config['help_templates'] = 'help/templates'
    page = render_html(written, help_merge(written, 'doc_list')).html
    assert 'h1 { color: red; }' in page and '--link:' in page
