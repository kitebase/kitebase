"""Tests for kitebase.diagnostics.run_checks — post-load descriptor validation.

Uses a PluginsManager populated via merge_dicts (so $plugin attribution is
real) and stub tables exposing only what the checks read: effective_columns
with .name, and .attributes.
"""
import pytest

from kitebase.plugins import PluginsManager
from kitebase.diagnostics import run_checks


class FakeCol:
    def __init__(self, name):
        self.name = name
        self.attributes = {}


class FakeTable:
    def __init__(self, *cols):
        self.effective_columns = [FakeCol(c) for c in cols]
        self.attributes = {}


class FakeApp:
    def __init__(self, pm, tables):
        self.pm = pm
        self.tables = tables


@pytest.fixture
def app():
    pm = PluginsManager()
    pm.merge_dicts({
        'pages': {
            'book_list': {
                'title': 'Books',
                'content': {'$ref': 'views.book_list_view'},
            },
            'broken': {
                'content': {'$ref': 'views.nope'},
            },
        },
        'views': {
            'book_list_view': {
                'type': 'table',
                'source': {'model': 'Book'},
                'columns': [
                    {'field': 'title'},
                    {'field': 'ghost'},
                    {'field': 'Publisher.name'},
                    {'field': 'Publisher.ghost'},
                    {'field': '$props.computed'},
                    {'field': "COUNT(DISTINCT Author.id) as n_authors"},
                ],
                'actions': {'row': [
                    {'id': 'edit', 'action': 'stack_push', 'panel': 'missing_page'},
                    {'id': 'ok', 'action': 'stack_push', 'panel': 'book_list'},
                ]},
            },
            'bad_model_view': {
                'type': 'form',
                'source': {'model': 'Nope'},
                'fields': [{'name': 'x'}],
            },
            'form_view': {
                'type': 'form',
                'source': {'model': 'Book'},
                'fields': [
                    {'name': 'title'},
                    {'group': 'Extra', 'fields': [{'name': 'phantom'}]},
                ],
            },
        },
    }, 'testplugin')

    tables = {
        'Book': FakeTable('id', 'title'),
        'Publisher': FakeTable('id', 'name'),
    }
    return FakeApp(pm, tables)


def by_code(issues, code):
    return [i for i in issues if i['code'] == code]


def test_ref_unresolved(app):
    issues = by_code(run_checks(app), 'ref-unresolved')
    assert len(issues) == 1
    assert issues[0]['severity'] == 'error'
    assert issues[0]['path'] == 'pages.broken.content'
    assert "'views.nope'" in issues[0]['message']


def test_model_missing(app):
    issues = by_code(run_checks(app), 'model-missing')
    assert len(issues) == 1
    assert issues[0]['path'] == 'views.bad_model_view.source.model'
    # No field checks on a view whose model doesn't exist
    fields = by_code(run_checks(app), 'field-unknown')
    assert not any('bad_model_view' in i['path'] for i in fields)


def test_field_unknown(app):
    issues = by_code(run_checks(app), 'field-unknown')
    paths = {i['path'] for i in issues}
    # Direct unknown field, unknown joined column, unknown group field
    assert 'views.book_list_view.columns[ghost]' in paths
    assert 'views.book_list_view.columns[Publisher.ghost]' in paths
    assert 'views.form_view.fields[Extra][phantom]' in paths
    # Valid fields, joined fields, $-values and SQL expressions raise nothing
    assert not any('[title]' in p or '[Publisher.name]' in p or '$props' in p
                   or 'COUNT' in p
                   for p in paths)


def test_panel_missing(app):
    issues = by_code(run_checks(app), 'panel-missing')
    assert len(issues) == 1
    assert "'missing_page'" in issues[0]['message']


def test_panel_auto_generated_is_valid(app):
    # 'book_form' is not an explicit page but auto-resolves from table Book
    app.pm.data['views']['book_list_view']['actions']['row'][0]['panel'] = 'book_form'
    assert not by_code(run_checks(app), 'panel-missing')


def test_view_orphan(app):
    issues = by_code(run_checks(app), 'view-orphan')
    orphans = {i['path'] for i in issues}
    assert 'views.bad_model_view' in orphans
    assert 'views.form_view' in orphans
    assert 'views.book_list_view' not in orphans  # referenced by book_list


def test_plugin_attribution(app):
    for issue in run_checks(app):
        assert issue['plugin'] == 'testplugin'


def test_merge_issues_included(app):
    app.pm.add_issue('warning', 'merge-anchor-missing', 'pages.x',
                     "anchor 'y' not found", 'p2')
    issues = by_code(run_checks(app), 'merge-anchor-missing')
    assert len(issues) == 1


def test_merge_overlap_collected():
    pm = PluginsManager()
    pm.merge_dicts({'config': {'theme': 'light'}}, 'p1')
    pm.merge_dicts({'config': {'theme': 'dark'}}, 'p2')
    overlaps = [i for i in pm.issues if i['code'] == 'merge-overlap']
    assert len(overlaps) == 1
    assert overlaps[0]['path'] == 'config.theme'
    assert overlaps[0]['plugin'] == 'p2'


def test_add_issue_dedupes():
    pm = PluginsManager()
    pm.add_issue('info', 'x', 'a.b', 'msg', 'p')
    pm.add_issue('info', 'x', 'a.b', 'msg', 'p')
    assert len(pm.issues) == 1


def test_generic_descriptor_sections_are_checked():
    """Sections other than pages/views (e.g. menus/menu_items) are validated
    uniformly: $ref and stack_push targets are checked wherever they appear."""
    pm = PluginsManager()
    pm.merge_dicts({
        'menus': {'main': {'label': 'Main'}},
        'menu_items': {
            'books': {'label': 'Books', 'action': 'stack_push', 'panel': 'book_list'},
            'ghost': {'label': 'Ghost', 'action': 'stack_push', 'panel': 'no_such_page'},
            'bad_ref': {'content': {'$ref': 'views.nope'}},
        },
    }, 'menuplugin')
    app = FakeApp(pm, {'Book': FakeTable('id', 'title')})

    issues = run_checks(app)
    # stack_push to an auto-generatable page (book_list ← Book) is fine;
    # to an unknown page it is flagged.
    panel_missing = by_code(issues, 'panel-missing')
    assert {i['path'] for i in panel_missing} == {'menu_items.ghost.panel'}
    assert any(i['path'] == 'menu_items.bad_ref.content' and i['plugin'] == 'menuplugin'
               for i in by_code(issues, 'ref-unresolved'))


def test_db_sections_not_walked_as_descriptors():
    """tables/types/schemas are DB/type sections, not descriptor trees."""
    from kitebase.diagnostics import _descriptor_sections
    pm = PluginsManager()
    pm.merge_dicts({'tables': {}, 'types': {}, 'schemas': {}, 'menus': {}}, 'p')
    assert _descriptor_sections(pm) == ['menus']


# ── collection nodes rest on an ownership declaration ─────────────────────────
#
# A collection is a composition: its rows are parts of the parent. The generator
# cannot read that off the node — it runs on the schema, before any page exists —
# so ownership is declared on the foreign key and the agreement is checked here.

def collections_app(node, table_attrs=None, fk=None):
    pm = PluginsManager()
    pm.merge_dicts({'pages': {'book_form': {
        'content': {
            'type': 'form',
            'source': {'model': 'Book'},
            'layout': [{'type': 'collection', 'id': 'chapters', **node}],
        },
    }}}, 'testplugin')

    child = FakeTable('id', 'title', 'book_id')
    child.attributes = table_attrs or {}
    child.effective_columns[2].attributes = {'foreign_key': fk} if fk else {}

    return FakeApp(pm, {'Book': FakeTable('id', 'title'), 'Chapter': child})


NODE = {'model': 'Chapter', 'fk': 'book_id'}


UNSTATED = 'collection-ownership-unstated'


def test_a_collection_that_says_nothing_about_ownership_is_asked():
    issues = by_code(run_checks(collections_app(NODE, fk={'target': 'Book.id'})),
                     UNSTATED)
    assert len(issues) == 1
    assert issues[0]['severity'] == 'warning'      # a question, not a refusal
    assert issues[0]['path'] == 'pages.book_form.content.layout[chapters].fk'
    assert 'Chapter.book_id' in issues[0]['message']


def test_a_collection_on_an_owned_key_passes():
    app = collections_app(NODE, fk={'target': 'Book.id', 'owned': True})
    assert by_code(run_checks(app), UNSTATED) == []


def test_an_explicit_false_settles_the_question():
    """Reviews of a book: edited inside its form, and outliving it on purpose."""
    app = collections_app(NODE, fk={'target': 'Book.id', 'owned': False})
    assert by_code(run_checks(app), UNSTATED) == []


def test_a_junction_needs_no_declaration():
    """Owned by both ends by default, so the flag is not written anywhere."""
    app = collections_app(NODE, fk={'target': 'Book.id'}, table_attrs={'many_to_many': {
        'target1': {'table': 'Book.id', 'column': 'book_id'},
        'target2': {'table': 'Author.id', 'column': 'author_id'},
    }})
    assert by_code(run_checks(app), UNSTATED) == []


def test_a_collection_whose_fk_is_not_a_key_at_all():
    issues = by_code(run_checks(collections_app(NODE)), 'collection-fk-not-a-key')
    assert len(issues) == 1


def test_a_collection_naming_a_column_that_does_not_exist():
    app = collections_app({'model': 'Chapter', 'fk': 'nowhere_id'})
    issues = by_code(run_checks(app), 'field-unknown')
    assert any(i['path'].endswith('layout[chapters].fk') for i in issues)


def test_an_unquoted_on_is_reported_not_a_crash():
    """YAML 1.1 reads `on:` as True: the join condition is lost, and check says why."""
    import yaml
    joins = yaml.safe_load('- Publisher: {type: left, on: "Book.publisher_id = Publisher.id"}')
    pm = PluginsManager()
    pm.merge_dicts({'pages': {'book_list': {'content': {
        'type': 'table', 'source': {'model': 'Book', 'joins': joins}}}}}, 'bench')

    issues = run_checks(FakeApp(pm, {'Book': FakeTable('id')}))
    found = by_code(issues, 'key-not-string')
    assert len(found) == 1 and 'on/off/yes/no' in found[0]['message']


def key_collision_app(*fields):
    pm = PluginsManager()
    pm.merge_dicts({'pages': {'book_list': {'content': {
        'type': 'table', 'source': {'model': 'Book'},
        'columns': [{'field': f} for f in fields]}}}}, 'bench')
    tables = {'Book': FakeTable('id', 'name'), 'Publisher': FakeTable('id', 'name')}
    return run_checks(FakeApp(pm, tables))


def test_a_joined_field_named_like_a_column_is_a_collision():
    """`Publisher.name` and `name` reach the grid under one key: wrong values, no error."""
    found = by_code(key_collision_app('name', 'Publisher.name'), 'field-key-collision')
    assert len(found) == 1 and 'Publisher.name' in found[0]['message']


def test_an_alias_keeps_them_apart():
    assert not by_code(key_collision_app('name', 'Publisher.name as publisher_name'),
                       'field-key-collision')


def test_an_alias_can_collide_too():
    found = by_code(key_collision_app('name', 'Publisher.name as name'), 'field-key-collision')
    assert len(found) == 1


def rank_app(rank):
    pm = PluginsManager()
    pm.merge_dicts({'pages': {}}, 'bench')
    table = FakeTable('id', 'name')
    table.effective_columns[1].attributes['query_rank'] = rank
    return run_checks(FakeApp(pm, {'Book': table}))


@pytest.mark.parametrize('rank', ['top', 'normal', 'low', 'more', 'none'])
def test_the_query_ranks_on_the_scale_pass(rank):
    assert not by_code(rank_app(rank), 'query-rank-unknown')


def test_a_query_rank_off_the_scale_is_named():
    found = by_code(rank_app('hight'), 'query-rank-unknown')
    assert len(found) == 1 and found[0]['path'] == 'tables.Book.name'
