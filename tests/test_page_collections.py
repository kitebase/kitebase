"""The seam: what a page declares may be written.

A save arrives with a page id, and the server reads its own descriptor to learn
which collections exist, in which table, and through which foreign key. These
tests cover the reading — the walk over a resolved page and the map it produces —
because everything the save endpoint refuses, it refuses on the strength of it.

Two consumers, one walk: `get_page` uses it to fill `view.source.model` from the
node that declares it, `save_tree` uses the map. They cannot diverge, and the
tests check both ends of that claim.
"""
import pytest

import coframe.utils
from coframe.plugins import PluginsManager
from coframe.pages import Collection, page_aggregate, resolve_collections
from coframe.endpoint_panels import get_page


class FakeApp:
    def __init__(self, pm):
        self.pm = pm
        self.tables = {}


def make_app(pages, plugin='bench'):
    pm = PluginsManager()
    pm.config = {}
    pm.merge_dicts({'pages': pages}, plugin)
    return FakeApp(pm)


def form_page(*layout, model='Book'):
    """A page whose content is a form carrying `layout`."""
    return {
        'title': 'Book',
        'content': {
            'type': 'form',
            'source': {'model': model},
            'layout': list(layout),
        },
    }


def authors_node(**overrides):
    node = {
        'type': 'collection',
        'id': 'authors',
        'model': 'BookAuthor',
        'fk': 'book_id',
        'view': {'type': 'table', 'columns': [{'field': 'author_id'}]},
    }
    node.update(overrides)
    return node


# ── The walk ────────────────────────────────────────────────────────────────

def test_node_fills_the_model_of_its_view():
    """The table is a fact of persistence: the node declares it, the view gets it."""
    page = form_page(authors_node())
    collections = resolve_collections(page, 'book_form')

    node = page['content']['layout'][0]
    assert node['view']['source']['model'] == 'BookAuthor'
    assert collections['authors'].model == 'BookAuthor'
    assert collections['authors'].fk == 'book_id'


def test_a_view_that_reads_another_table_is_an_error_naming_both():
    page = form_page(authors_node(view={'type': 'table', 'source': {'model': 'Author'}}))

    with pytest.raises(ValueError) as exc:
        resolve_collections(page, 'book_form')

    assert 'BookAuthor' in str(exc.value) and 'Author' in str(exc.value)


def test_a_view_that_agrees_is_left_alone():
    page = form_page(authors_node(view={'type': 'table', 'source': {'model': 'BookAuthor'}}))
    assert resolve_collections(page, 'book_form')['authors'].model == 'BookAuthor'


def test_the_error_names_the_plugin_that_wrote_the_node():
    """Errors run before metadata is stripped, so they can still say where to look."""
    page = form_page(authors_node(**{'fk': None, '$plugin': 'library'}))

    with pytest.raises(ValueError) as exc:
        resolve_collections(page, 'book_form')

    assert 'library' in str(exc.value) and 'book_form' in str(exc.value)


@pytest.mark.parametrize('missing', ['id', 'model', 'fk'])
def test_a_node_without_its_essentials_is_refused(missing):
    node = authors_node()
    del node[missing]

    with pytest.raises(ValueError):
        resolve_collections(form_page(node), 'book_form')


def test_two_collections_with_the_same_id_collide():
    page = form_page(authors_node(), authors_node(model='Loan', fk='book_id'))

    with pytest.raises(ValueError, match='Duplicate'):
        resolve_collections(page, 'book_form')


def test_a_node_is_found_however_deep_the_layout_puts_it():
    """A collection is a layout node like `section` or `row`: the layout decides."""
    page = form_page({
        'type': 'tabs',
        'tabs': [
            {'label': 'Data', 'layout': [{'type': 'section', 'columns': []}]},
            {'label': 'Authors', 'layout': [authors_node()]},
        ],
    })

    assert 'authors' in resolve_collections(page, 'book_form')


def test_the_view_of_a_node_is_not_searched_for_more_nodes():
    """A collection under a collection is declared in the row form, not in the grid."""
    page = form_page(authors_node(view={
        'type': 'table',
        'layout': [{'type': 'collection', 'id': 'smuggled',
                    'model': 'User', 'fk': 'x_id'}],
    }))

    assert set(resolve_collections(page, 'book_form')) == {'authors'}


def test_the_row_form_defaults_to_the_model_form():
    # Lower case: the same convention is spelled that way by the list that opens
    # a form, and a declared page is matched by exact id.
    collections = resolve_collections(form_page(authors_node()), 'book_form')
    assert collections['authors'].form == 'bookauthor_form'

    declared = resolve_collections(form_page(authors_node(form='ba_row')), 'book_form')
    assert declared['authors'].form == 'ba_row'


def test_domain_and_defaults_travel_with_the_node():
    node = authors_node(domain=[{'usage': 'shipping'}], defaults={'usage': 'shipping'})
    coll = resolve_collections(form_page(node), 'partner_form')['authors']

    assert coll.domain == [{'usage': 'shipping'}]
    assert coll.defaults == {'usage': 'shipping'}


# ── The map ─────────────────────────────────────────────────────────────────

def test_the_aggregate_carries_the_root_model_and_its_collections():
    app = make_app({'book_form': form_page(authors_node())})
    aggregate = page_aggregate(app, 'book_form')

    assert aggregate.model == 'Book'
    assert isinstance(aggregate.collections['authors'], Collection)


def test_the_tree_belongs_to_the_page_and_not_to_the_table():
    """Two forms on one table: with the node it is an aggregate, without it a row.

    The flat case is the general one with no collections — the recursion stopping
    at the first step — which is why the same endpoint serves both.
    """
    app = make_app({
        'book_form': form_page(authors_node()),
        'book_quick_form': form_page({'type': 'section', 'columns': []}),
    })

    assert set(page_aggregate(app, 'book_form').collections) == {'authors'}

    flat = page_aggregate(app, 'book_quick_form')
    assert flat.model == 'Book'
    assert flat.collections == {}


def test_a_page_that_names_no_model_cannot_be_written():
    app = make_app({'odd_form': {'content': {'type': 'form'}}})

    with pytest.raises(ValueError, match='no model'):
        page_aggregate(app, 'odd_form')


def test_an_unknown_page_is_not_a_tree():
    with pytest.raises(ValueError, match='not found'):
        page_aggregate(make_app({}), 'nowhere_form')


def test_the_tree_spans_pages_through_the_row_form():
    """Grandchildren come from the row form of a collection — recursion, one map."""
    app = make_app({
        'book_form': form_page(authors_node(form='ba_row')),
        'ba_row': form_page({'type': 'collection', 'id': 'remarks',
                             'model': 'Remark', 'fk': 'ba_id'}, model='BookAuthor'),
    })

    authors = page_aggregate(app, 'book_form').collections['authors']
    assert authors.collections['remarks'].model == 'Remark'


def test_a_row_form_nobody_wrote_stops_the_descent_quietly():
    """Which page opens a row is a question of interface, not a reason to refuse."""
    app = make_app({'book_form': form_page(authors_node(form='not_written_yet'))})

    assert page_aggregate(app, 'book_form').collections['authors'].collections == {}


def test_a_row_form_that_exists_and_is_broken_still_raises():
    app = make_app({
        'book_form': form_page(authors_node(form='ba_row')),
        'ba_row': form_page({'type': 'collection', 'id': 'remarks', 'model': 'Remark'},
                            model='BookAuthor'),
    })

    with pytest.raises(ValueError, match='fk'):
        page_aggregate(app, 'book_form')


def test_recursion_stops_where_the_path_repeats():
    """The contacts of a partner are partners: expanding forever describes nothing."""
    app = make_app({
        'partner_form': form_page({'type': 'collection', 'id': 'contacts',
                                   'model': 'Partner', 'fk': 'parent_id',
                                   'form': 'partner_form'}, model='Partner'),
    })

    contacts = page_aggregate(app, 'partner_form').collections['contacts']
    assert contacts.collections == {}


# ── Faces: what a button opens ──────────────────────────────────────────────
#
# A button opens a face of the *same* record (relations.md §19.1). An inline
# collection needs nothing new — the walk finds it wherever it sits — but a face
# named by page id is a second descriptor, and its collections belong to this
# record, at this level. Without the descent the client would draw a grid whose
# rows the save refuses by name.

def button(page=None, **overrides):
    node = {'type': 'button', 'label': 'More'}
    if page:
        node['opens'] = {'type': 'form', 'page': page}
    node.update(overrides)
    return node


def test_a_collection_behind_a_button_is_a_collection_of_this_page():
    """Inline: the node sits inside the button, and the walk goes everywhere."""
    app = make_app({'book_form': form_page(button(opens=authors_node()))})

    assert page_aggregate(app, 'book_form').collections['authors'].model == 'BookAuthor'


def test_the_collections_of_a_face_merge_at_this_level():
    """A face is the same record with other fields — not a child of it."""
    app = make_app({
        'book_form': form_page(button(page='book_more')),
        'book_more': form_page(authors_node()),
    })

    aggregate = page_aggregate(app, 'book_form')
    assert aggregate.collections['authors'].model == 'BookAuthor'
    assert aggregate.collections['authors'].fk == 'book_id'


def test_a_face_of_a_face_merges_too():
    app = make_app({
        'book_form': form_page(button(page='book_more')),
        'book_more': form_page(button(page='book_even_more')),
        'book_even_more': form_page(authors_node()),
    })

    assert 'authors' in page_aggregate(app, 'book_form').collections


def test_the_row_forms_of_a_face_still_descend():
    """A collection reached through a face is a collection like any other."""
    app = make_app({
        'book_form': form_page(button(page='book_more')),
        'book_more': form_page(authors_node(form='ba_row')),
        'ba_row': form_page({'type': 'collection', 'id': 'remarks',
                             'model': 'Remark', 'fk': 'ba_id'}, model='BookAuthor'),
    })

    authors = page_aggregate(app, 'book_form').collections['authors']
    assert authors.collections['remarks'].model == 'Remark'


def test_a_face_that_repeats_an_id_is_refused_naming_both():
    """One id, one collection: the payload names them, so two would be ambiguous."""
    app = make_app({
        'book_form': form_page(authors_node(), button(page='book_more')),
        'book_more': form_page(authors_node()),
    })

    with pytest.raises(ValueError) as exc:
        page_aggregate(app, 'book_form')

    assert 'authors' in str(exc.value) and 'book_more' in str(exc.value)


def test_a_face_nobody_wrote_stops_the_descent_quietly():
    app = make_app({'book_form': form_page(button(page='not_written_yet'))})

    assert page_aggregate(app, 'book_form').collections == {}


def test_a_face_that_points_back_does_not_loop():
    app = make_app({'book_form': form_page(button(page='book_form'))})

    assert page_aggregate(app, 'book_form').collections == {}


def test_a_button_that_calls_an_endpoint_declares_no_tree():
    app = make_app({'book_form': form_page(
        {'type': 'button', 'label': 'Statistics', 'endpoint': 'book_stats',
         'pass': {'id': '$record.id'}})})

    assert page_aggregate(app, 'book_form').collections == {}


# ── The other consumer ──────────────────────────────────────────────────────

def test_get_page_hands_the_client_the_completed_descriptor(monkeypatch):
    app = make_app({'book_form': form_page(authors_node())})
    monkeypatch.setattr(coframe.utils, 'get_app', lambda: app)

    result = get_page({'id': 'book_form'})
    node = result['data']['content']['layout'][0]

    assert result['status'] == 'success'
    assert node['view']['source']['model'] == 'BookAuthor'


def test_get_page_reports_a_broken_node_instead_of_serving_it(monkeypatch):
    app = make_app({'book_form': form_page(authors_node(fk=None))})
    monkeypatch.setattr(coframe.utils, 'get_app', lambda: app)

    result = get_page({'id': 'book_form'})

    assert result['code'] == 400
    assert 'fk' in result['message']


# ── List column titles from the model's labels ──────────────────────────────

class _Col:
    def __init__(self, name, label=None):
        self.name = name
        self.attributes = {'label': label} if label else {}


class _Table:
    def __init__(self, *cols):
        self.effective_columns = list(cols)


def titled_app(pages):
    app = make_app(pages)
    app.tables = {
        'Book': _Table(_Col('title', 'Title'), _Col('price')),
        'Publisher': _Table(_Col('name', 'Publisher name')),
        'BookAuthor': _Table(_Col('author_id', 'Author')),
    }
    return app


def test_a_written_list_takes_titles_from_the_model():
    from coframe.pages import load_page
    app = titled_app({'book_list': {'content': {
        'type': 'table',
        'source': {'model': 'Book'},
        'columns': [
            {'field': 'title'},
            {'field': 'price'},
            {'field': 'Publisher.name as publisher'},
            {'field': 'title', 'title': 'Mine'},
        ],
    }}})

    columns = load_page(app, 'book_list')['content']['columns']

    assert [c.get('title') for c in columns] == ['Title', None, 'Publisher name', 'Mine']


def test_a_collection_grid_takes_titles_from_its_model():
    from coframe.pages import load_page
    app = titled_app({'book_form': form_page(
        {'type': 'section', 'columns': [{'id': 'left', 'fields': [authors_node()]}]})})

    page = load_page(app, 'book_form')
    node = page['content']['layout'][0]['columns'][0]['fields'][0]

    assert node['view']['columns'] == [{'field': 'author_id', 'title': 'Author'}]


# ── Written form fields inherit what their column declares ──────────────────

class _FkTable:
    name = 'Publisher'


def test_a_written_form_field_inherits_its_column_and_keeps_what_it_says():
    from coframe.pages import load_page
    app = titled_app({'book_form': form_page(
        {'type': 'section', 'columns': [{'id': 'left', 'fields': [
            {'name': 'title', 'width': '50%'},
            {'name': 'publisher_id', 'label': 'Mine'},
            {'filler': None},
            {'name': 'not_a_column'},
        ]}]},
        authors_node(),
    )})
    pub = _Col('publisher_id', 'Publisher')
    pub.attributes.update({'type': 'FK', 'nullable': False,
                           'foreign_key': {'table': _FkTable(), 'id': 'id'}})
    app.tables['Book'].effective_columns.append(pub)

    layout = load_page(app, 'book_form')['content']['layout']
    title, publisher, filler, other = layout[0]['columns'][0]['fields']

    assert title == {'name': 'title', 'label': 'Title', 'width': '50%'}
    assert publisher['label'] == 'Mine'
    assert publisher['foreign_key'] == {'target': 'Publisher', 'field': 'id'}
    assert publisher['required'] is True
    assert filler == {'filler': None}
    assert other == {'name': 'not_a_column'}
    # A collection is its own model's business: its grid gets its own titles.
    assert layout[1]['view']['columns'] == [{'field': 'author_id', 'title': 'Author'}]
