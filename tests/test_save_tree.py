"""Reading and writing an aggregate, on a database.

The bench is the one the design names: `Book → BookAuthor`, an association object
whose rows never grow without bound, plus a `Book → Chapter → Note` chain so the
recursive half is exercised where it actually differs — grandchildren, and
deletions that must run the other way round.

What these tests are really about is the seam. The payload never names a table:
it names a page, and the page's descriptor decides what may be written. Half the
cases below are refusals, and each one is a table that could otherwise have been
written by anybody who could shape a JSON body.
"""
import importlib.util

import pytest
import yaml
from sqlalchemy import event, text

import kitebase.utils
from kitebase.db import DB, Base
from kitebase.endpoint_tree import load_tree, save_tree
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
            {'name': 'isbn', 'type': 'String', 'length': 20},
        ],
    },
    # Two foreign keys and a column of its own: a junction that is a record, so
    # it wants a grid and not a tag widget.
    'BookAuthor': {
        'name': 'books_authors',
        'columns': [{'name': 'notes', 'type': 'String', 'length': 200}],
        'many_to_many': {
            'target1': {'table': 'Book.id', 'column': 'book_id'},
            'target2': {'table': 'Author.id', 'column': 'author_id'},
        },
    },
    # Owned, and owned again one level down: a chapter is a part of its book, a
    # note a part of its chapter.
    'Chapter': {
        'name': 'chapters',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
            {'name': 'kind', 'type': 'String', 'length': 20},
            {'name': 'book_id', 'type': 'Integer',
             'foreign_key': {'target': 'Book.id', 'owned': True}},
        ],
    },
    'Note': {
        'name': 'notes',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'text', 'type': 'String', 'length': 200},
            {'name': 'chapter_id', 'type': 'Integer',
             'foreign_key': {'target': 'Chapter.id', 'owned': True}},
        ],
    },
    # Not owned: a loan grows in time and is nobody's part. It survives the book.
    'Loan': {
        'name': 'loans',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'due_date', 'type': 'String', 'length': 20},
            {'name': 'book_id', 'type': 'Integer', 'foreign_key': {'target': 'Book.id'}},
        ],
    },
    # Tied by a key that is not the primary one: a visit mints its code when it
    # is written, and a report cites the code, not the id — a terminal that wrote
    # the report knew the code long before any id existed.
    'Visit': {
        'name': 'visits',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'code', 'type': 'String', 'length': 36, 'default': '$uuid',
             'unique': True, 'nullable': False},
            {'name': 'place', 'type': 'String', 'length': 60},
        ],
    },
    'Report': {
        'name': 'reports',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'text', 'type': 'String', 'length': 200},
            {'name': 'visit_code', 'type': 'String', 'length': 36,
             'foreign_key': {'target': 'Visit.code', 'constraint': False}},
        ],
    },
}


def _collection(cid, model, fk, **extra):
    return {'type': 'collection', 'id': cid, 'model': model, 'fk': fk, **extra}


def _form(model, *layout):
    return {'content': {'type': 'form', 'source': {'model': model}, 'layout': list(layout)}}


PAGES = {
    # The bench: a book, its authors, its chapters — one aggregate.
    'book_form': _form(
        'Book',
        _collection('authors', 'BookAuthor', 'book_id'),
        _collection('chapters', 'Chapter', 'book_id', form='chapter_form'),
    ),
    # The row form of a chapter declares a collection of its own: the tree spans
    # pages, and the save engine reaches the third level without knowing it.
    'chapter_form': _form('Chapter', _collection('notes', 'Note', 'chapter_id')),
    # Same table, no node: a plain record. The flat case is the general one with
    # the recursion stopping at the first step.
    'book_quick_form': _form('Book'),
    # Tabs of one collection are domain + defaults, and nothing else.
    # The collection is tied to the column its foreign key targets, not to the id.
    'visit_form': _form('Visit', _collection('reports', 'Report', 'visit_code')),
    'book_appendix_form': _form(
        'Book',
        _collection('appendix', 'Chapter', 'book_id',
                    domain=[{'kind': 'appendix'}], defaults={'kind': 'appendix'}),
    ),
}


@pytest.fixture(autouse=True)
def fresh_registry():
    """Each test maps its own classes onto the shared declarative Base."""
    yield
    Base.registry.dispose()
    Base.metadata.clear()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A one-plugin application on a real sqlite file, wired to kitebase.utils."""
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
    spec = importlib.util.spec_from_file_location('bench_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    instance.initialize_db(f'sqlite:///{tmp_path / "bench.sqlite"}', module, check_schema=False)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)
    return instance


def rows(app, model_name):
    """Every row of a table, as dicts, straight from the ORM."""
    model_class = app.find_model_class(model_name)
    with app.get_session() as session:
        return [kitebase.utils.serialize_model(obj, db_table=app.tables.get(model_name))
                for obj in session.query(model_class).all()]


def make_author(app, name):
    model_class = app.find_model_class('Author')
    with app.get_session() as session:
        obj = model_class(name=name)
        session.add(obj)
        session.commit()
        return obj.id


def ok(result):
    """Assert a successful envelope and return its data."""
    assert result['status'] == 'success', result.get('message')
    return result['data']


# ── Create: the parent's key, and the children that were waiting for it ─────

def test_a_new_book_and_its_authors_are_written_in_one_call(app):
    gaiman = make_author(app, 'Gaiman')
    pratchett = make_author(app, 'Pratchett')

    data = ok(save_tree({
        'page': 'book_form',
        'root': {
            'op': 'create', 'id': -1,
            'values': {'title': 'Good Omens', 'isbn': '0-575-04800-X'},
            'children': {'authors': [
                # The child carries the parent's temporary id, which is what the
                # buffer held while the book had no key.
                {'op': 'create', 'id': -2,
                 'values': {'book_id': -1, 'author_id': gaiman, 'notes': 'first'}},
                {'op': 'create', 'id': -3,
                 'values': {'book_id': -1, 'author_id': pratchett}},
            ]},
        },
    }))

    book_id = data['id']
    assert book_id > 0
    assert data['id_map'][-1] == book_id
    assert set(data['id_map']) == {-1, -2, -3}

    written = rows(app, 'BookAuthor')
    assert len(written) == 2
    assert {row['book_id'] for row in written} == {book_id}
    assert {row['author_id'] for row in written} == {gaiman, pratchett}


def test_a_child_that_says_nothing_still_gets_its_foreign_key(app):
    """The FK is a value the framework writes, so the row need not carry it."""
    author = make_author(app, 'Le Guin')

    data = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'A Wizard of Earthsea'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2, 'values': {'author_id': author}}]}},
    }))

    assert rows(app, 'BookAuthor')[0]['book_id'] == data['id']


def test_a_temporary_id_nothing_creates_is_refused(app):
    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'x'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2, 'values': {'author_id': -99}}]}},
    })

    assert result['code'] == 400
    assert '-99' in result['message']
    assert rows(app, 'Book') == []      # one commit at the top: nothing was written


def test_a_temporary_id_used_twice_is_refused(app):
    """One counter for the payload: the map is global, and so is the collision."""
    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'}},
                     {'op': 'create', 'id': -2, 'values': {'title': 'Caladan'}},
                 ]}},
    })

    assert result['code'] == 400
    assert '-2' in result['message']
    assert rows(app, 'Book') == []


def test_a_row_cannot_reparent_itself(app):
    """A child naming another book is a reparenting the caller thinks it performed."""
    author = make_author(app, 'Borges')
    first = ok(save_tree({'page': 'book_form',
                          'root': {'op': 'create', 'id': -1, 'values': {'title': 'Ficciones'}}}))

    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'El Aleph'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2,
                      'values': {'book_id': first['id'], 'author_id': author}}]}},
    })

    assert result['code'] == 400
    assert 'book_id' in result['message']


# ── Update and delete: explicit operations, never a delta ───────────────────

def test_the_three_operations_travel_together(app):
    gaiman = make_author(app, 'Gaiman')
    pratchett = make_author(app, 'Pratchett')
    adams = make_author(app, 'Adams')

    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Good Omens'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2, 'values': {'author_id': gaiman}},
                     {'op': 'create', 'id': -3, 'values': {'author_id': pratchett}},
                 ]}},
    }))
    book_id = created['id']
    keep, drop = (created['id_map'][-2], created['id_map'][-3])

    ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': book_id, 'values': {'title': 'Good Omens (rev.)'},
                 'children': {'authors': [
                     {'op': 'update', 'id': keep, 'values': {'notes': 'lead'}},
                     {'op': 'delete', 'id': drop},
                     {'op': 'create', 'id': -9, 'values': {'author_id': adams}},
                 ]}},
    }))

    assert rows(app, 'Book')[0]['title'] == 'Good Omens (rev.)'
    written = {row['id']: row for row in rows(app, 'BookAuthor')}
    assert drop not in written
    assert written[keep]['notes'] == 'lead'
    assert {row['author_id'] for row in written.values()} == {gaiman, adams}


def test_a_form_without_the_node_leaves_the_children_alone(app):
    """The tree belongs to the page: no `children` means no operation on them."""
    author = make_author(app, 'Calvino')
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Le città invisibili'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2, 'values': {'author_id': author}}]}},
    }))

    ok(save_tree({'page': 'book_quick_form',
                  'root': {'op': 'update', 'id': created['id'],
                           'values': {'isbn': '88-06-15997-1'}}}))

    assert rows(app, 'Book')[0]['isbn'] == '88-06-15997-1'
    assert len(rows(app, 'BookAuthor')) == 1


# ── Composition: what a row owns goes with it ───────────────────────────────
#
# `owned: true` on a foreign key says the row is a part of its parent. It is the
# ORM that enforces it, not the DDL, so it reads the same for a soft key, survives
# dialects that refuse the constraint, and asks nothing of a database already in
# place. The client does not have to declare any of these deletions — and past the
# second level it could not, since it never loaded those rows.

def test_deleting_a_book_takes_its_junction_rows_and_leaves_the_authors(app):
    """The cascade only ever runs parent → child, and an author is a parent."""
    gaiman = make_author(app, 'Gaiman')
    pratchett = make_author(app, 'Pratchett')

    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Good Omens'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2, 'values': {'author_id': gaiman}},
                     {'op': 'create', 'id': -3, 'values': {'author_id': pratchett}},
                 ]}},
    }))

    ok(save_tree({'page': 'book_form',
                  'root': {'op': 'delete', 'id': created['id']}}))

    assert rows(app, 'Book') == []
    assert rows(app, 'BookAuthor') == []
    assert {row['name'] for row in rows(app, 'Author')} == {'Gaiman', 'Pratchett'}


def test_the_cascade_reaches_the_grandchildren_nobody_declared(app):
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'},
                      'children': {'notes': [{'op': 'create', 'id': -3,
                                              'values': {'text': 'spice'}}]}},
                 ]}},
    }))

    ok(save_tree({'page': 'book_form',
                  'root': {'op': 'delete', 'id': created['id']}}))

    assert rows(app, 'Chapter') == []
    assert rows(app, 'Note') == []


def test_deleting_one_collection_row_takes_its_own_children(app):
    """Removing a row from the grid: the parent stays, the row's parts go with it."""
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'},
                      'children': {'notes': [{'op': 'create', 'id': -3,
                                              'values': {'text': 'spice'}}]}},
                     {'op': 'create', 'id': -4, 'values': {'title': 'Caladan'},
                      'children': {'notes': [{'op': 'create', 'id': -5,
                                              'values': {'text': 'water'}}]}},
                 ]}},
    }))

    ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': created['id'], 'values': {},
                 'children': {'chapters': [
                     {'op': 'delete', 'id': created['id_map'][-2]}]}},
    }))

    assert [row['title'] for row in rows(app, 'Chapter')] == ['Caladan']
    assert [row['text'] for row in rows(app, 'Note')] == ['water']


def test_what_is_not_owned_survives_the_parent(app):
    """A loan grows in time and is nobody's part: it is not swept along."""
    created = ok(save_tree({'page': 'book_form',
                            'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'}}}))

    model_class = app.find_model_class('Loan')
    with app.get_session() as session:
        session.add(model_class(book_id=created['id'], due_date='2026-09-01'))
        session.commit()

    ok(save_tree({'page': 'book_form',
                  'root': {'op': 'delete', 'id': created['id']}}))

    assert len(rows(app, 'Loan')) == 1


def test_a_delete_needs_the_id_of_a_saved_row(app):
    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'x'},
                 'children': {'authors': [{'op': 'delete', 'id': -2}]}},
    })

    assert result['code'] == 400


def test_an_unknown_operation_is_refused(app):
    result = save_tree({'page': 'book_quick_form',
                        'root': {'op': 'upsert', 'values': {'title': 'x'}}})

    assert result['code'] == 400
    assert 'upsert' in result['message']


# ── Depth: grandchildren, and deletions the other way round ─────────────────

def test_three_levels_in_one_transaction(app):
    """The engine is recursive from the start: the third level costs nothing."""
    data = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'},
                      'children': {'notes': [
                          {'op': 'create', 'id': -3, 'values': {'text': 'spice'}},
                      ]}},
                 ]}},
    }))

    chapter = rows(app, 'Chapter')[0]
    note = rows(app, 'Note')[0]
    assert chapter['book_id'] == data['id']
    assert note['chapter_id'] == chapter['id']
    assert data['id_map'][-3] == note['id']


def test_deletions_run_from_the_bottom_up(app):
    """Grandchild, child, parent — collected on the way down, applied by depth."""
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'},
                      'children': {'notes': [{'op': 'create', 'id': -3,
                                              'values': {'text': 'spice'}}]}},
                 ]}},
    }))
    chapter_id = created['id_map'][-2]
    note_id = created['id_map'][-3]

    ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': created['id'], 'values': {},
                 'children': {'chapters': [
                     {'op': 'delete', 'id': chapter_id,
                      'children': {'notes': [{'op': 'delete', 'id': note_id}]}},
                 ]}},
    }))

    assert rows(app, 'Chapter') == []
    assert rows(app, 'Note') == []


# ── The seam: what the payload may not say ──────────────────────────────────

def test_nothing_may_be_added_under_a_row_that_goes_away(app):
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'}}]}},
    }))
    chapter_id = created['id_map'][-2]

    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': created['id'], 'values': {},
                 'children': {'chapters': [
                     {'op': 'delete', 'id': chapter_id,
                      'children': {'notes': [{'op': 'create', 'id': -3,
                                              'values': {'text': 'orphan'}}]}},
                 ]}},
    })

    assert result['code'] == 400
    assert rows(app, 'Chapter') != []    # the whole save was refused, not half of it


def test_a_collection_the_page_does_not_declare_is_refused(app):
    result = save_tree({
        'page': 'book_quick_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'x'},
                 'children': {'authors': [{'op': 'create', 'id': -2, 'values': {}}]}},
    })

    assert result['code'] == 400
    assert 'authors' in result['message']
    assert rows(app, 'Book') == []


def test_a_collection_declared_one_level_up_is_not_available_below(app):
    """`chapters` belongs to the book, not to a chapter: depth is part of the map."""
    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'x'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {},
                      'children': {'chapters': [{'op': 'create', 'id': -3, 'values': {}}]}},
                 ]}},
    })

    assert result['code'] == 400
    assert 'chapters' in result['message']


def test_an_unknown_page_writes_nothing(app):
    result = save_tree({'page': 'nowhere_form',
                        'root': {'op': 'create', 'id': -1, 'values': {'title': 'x'}}})

    assert result['code'] == 400
    assert rows(app, 'Book') == []


# ── Pass-through: the rows that are only on the way ─────────────────────────

def _book_with_a_note(app):
    """A book, a chapter, a note — the shortest tree with something in the middle."""
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'},
                      'children': {'notes': [{'op': 'create', 'id': -3,
                                              'values': {'text': 'spice'}}]}},
                 ]}},
    }))
    return created['id'], created['id_map'][-2], created['id_map'][-3]


def test_an_untouched_row_is_only_the_way_down_to_a_grandchild(app):
    """No `op` means no write: the node is there because a note below it changed."""
    book_id, chapter_id, note_id = _book_with_a_note(app)

    ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': book_id, 'values': {},
                 'children': {'chapters': [
                     # Values and all — a pass-through carries whatever the buffer
                     # holds, and none of it is written.
                     {'id': chapter_id, 'values': {'title': 'Caladan'},
                      'children': {'notes': [
                          {'op': 'update', 'id': note_id, 'values': {'text': 'melange'}}]}},
                 ]}},
    }))

    assert rows(app, 'Chapter')[0]['title'] == 'Arrakis'
    assert rows(app, 'Note')[0]['text'] == 'melange'


def test_the_root_may_pass_through_as_well(app):
    book_id, _chapter_id, _note_id = _book_with_a_note(app)

    ok(save_tree({
        'page': 'book_form',
        'root': {'id': book_id, 'values': {'title': 'Messiah'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -1, 'values': {'title': 'Appendix'}}]}},
    }))

    assert rows(app, 'Book')[0]['title'] == 'Dune'
    assert {row['title'] for row in rows(app, 'Chapter')} == {'Arrakis', 'Appendix'}
    assert {row['book_id'] for row in rows(app, 'Chapter')} == {book_id}


def test_what_load_returns_is_accepted_unchanged(app):
    """The two shapes are one: an answer fed straight back writes nothing."""
    book_id, _chapter_id, _note_id = _book_with_a_note(app)
    before = (rows(app, 'Book'), rows(app, 'Chapter'), rows(app, 'Note'))

    tree = ok(load_tree({'page': 'book_form', 'id': book_id}))
    ok(save_tree({'page': 'book_form', 'root': tree}))

    assert (rows(app, 'Book'), rows(app, 'Chapter'), rows(app, 'Note')) == before


def test_a_pass_through_needs_the_id_of_a_saved_row(app):
    """A row that was never written has nothing below it to reach."""
    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'x'},
                 'children': {'chapters': [{'id': -2, 'values': {}}]}},
    })

    assert result['code'] == 400
    assert rows(app, 'Book') == []


def test_a_pass_through_cannot_reach_into_another_aggregate(app):
    """Nothing is written on the node, so the parent it claims is checked instead."""
    _first, chapter_of_first, _note = _book_with_a_note(app)
    second = ok(save_tree({'page': 'book_form',
                           'root': {'op': 'create', 'id': -1, 'values': {'title': 'Ubik'}}}))

    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': second['id'], 'values': {},
                 'children': {'chapters': [
                     {'id': chapter_of_first, 'values': {},
                      'children': {'notes': [{'op': 'create', 'id': -2,
                                              'values': {'text': 'grafted'}}]}},
                 ]}},
    })

    assert result['code'] == 400
    assert str(chapter_of_first) in result['message']
    assert len(rows(app, 'Note')) == 1      # the one the fixture wrote, and no other


def test_a_row_that_would_survive_its_deleted_parent_is_refused(app):
    """Below a row that goes away only deletions make sense — silence included."""
    book_id, chapter_id, note_id = _book_with_a_note(app)

    result = save_tree({
        'page': 'book_form',
        'root': {'op': 'update', 'id': book_id, 'values': {},
                 'children': {'chapters': [
                     {'op': 'delete', 'id': chapter_id,
                      'children': {'notes': [{'id': note_id, 'values': {}}]}},
                 ]}},
    })

    assert result['code'] == 400
    assert rows(app, 'Chapter') != []


# ── Domain and defaults: the same invariant in both directions ──────────────

def test_a_row_created_under_a_domain_is_stamped_to_satisfy_it(app):
    """What you filter for, you stamp at creation — or the row vanishes at once."""
    data = ok(save_tree({
        'page': 'book_appendix_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'appendix': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Ecology'}}]}},
    }))

    assert rows(app, 'Chapter')[0]['kind'] == 'appendix'
    assert len(data['root']['children']['appendix']) == 1


def test_a_row_that_sets_the_default_itself_keeps_its_value(app):
    """Defaults fill what the row leaves unsaid; they do not overrule it."""
    ok(save_tree({
        'page': 'book_appendix_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'appendix': [
                     {'op': 'create', 'id': -2,
                      'values': {'title': 'Arrakis', 'kind': 'body'}}]}},
    }))

    assert rows(app, 'Chapter')[0]['kind'] == 'body'


def test_the_domain_hides_from_the_tab_what_does_not_belong_to_it(app):
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis', 'kind': 'body'}},
                     {'op': 'create', 'id': -3, 'values': {'title': 'Ecology',
                                                           'kind': 'appendix'}},
                 ]}},
    }))

    whole = ok(load_tree({'page': 'book_form', 'id': created['id']}))
    tab = ok(load_tree({'page': 'book_appendix_form', 'id': created['id']}))

    assert len(whole['children']['chapters']) == 2
    assert [row['values']['title'] for row in tab['children']['appendix']] == ['Ecology']


# ── Load ────────────────────────────────────────────────────────────────────

def test_load_returns_the_same_shape_the_save_accepts(app):
    author = make_author(app, 'Herbert')
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {
                     'authors': [{'op': 'create', 'id': -2, 'values': {'author_id': author}}],
                     'chapters': [{'op': 'create', 'id': -3, 'values': {'title': 'Arrakis'},
                                   'children': {'notes': [
                                       {'op': 'create', 'id': -4, 'values': {'text': 'spice'}}]}}],
                 }},
    }))

    root = ok(load_tree({'page': 'book_form', 'id': created['id']}))

    assert root['id'] == created['id']
    assert root['values']['title'] == 'Dune'
    assert root['children']['authors'][0]['values']['author_id'] == author
    chapter = root['children']['chapters'][0]
    assert chapter['children']['notes'][0]['values']['text'] == 'spice'


def test_an_empty_collection_is_a_fact_and_not_a_gap(app):
    created = ok(save_tree({'page': 'book_form',
                            'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'}}}))

    root = ok(load_tree({'page': 'book_form', 'id': created['id']}))

    assert root['children'] == {'authors': [], 'chapters': []}


def test_a_level_costs_one_query_per_collection_and_not_one_per_row(app):
    """No N+1: the ids of a level are all known, so `IN` takes it in one go."""
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'},
                      'children': {'notes': [{'op': 'create', 'id': -3,
                                              'values': {'text': 'spice'}}]}},
                     {'op': 'create', 'id': -4, 'values': {'title': 'Caladan'},
                      'children': {'notes': [{'op': 'create', 'id': -5,
                                              'values': {'text': 'water'}}]}},
                     {'op': 'create', 'id': -6, 'values': {'title': 'Giedi Prime'}},
                 ]}},
    }))

    statements = []

    def record(conn, cursor, statement, *rest):
        statements.append(statement)

    event.listen(app.engine, 'before_cursor_execute', record)
    try:
        ok(load_tree({'page': 'book_form', 'id': created['id']}))
    finally:
        event.remove(app.engine, 'before_cursor_execute', record)

    # root + authors + chapters + notes — three chapters and two notes change nothing
    selects = [s for s in statements if s.lstrip().upper().startswith('SELECT')]
    assert len(selects) == 4


def test_an_aggregate_opens_whole_under_a_behavior_that_scopes_lists(app):
    """A behavior scopes a browse list. An aggregate opened by key is not one.

    Archivable is the case that matters: a book kept out of the picker must still
    open, and open *whole* — its authors are stored facts, not a picklist of what
    is selectable now, and a child invisible in the only place it can be edited
    could never be restored.
    """
    class HideEverything:
        @classmethod
        def applies_to(cls, model_class):
            return True

        @classmethod
        def apply(cls, model_class, query_def, query):
            return query.where(text('1 = 0'))

    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis'}}]}},
    }))

    app.query_behaviors.append(HideEverything)
    root = ok(load_tree({'page': 'book_form', 'id': created['id']}))

    assert root['values']['title'] == 'Dune'
    assert [row['values']['title'] for row in root['children']['chapters']] == ['Arrakis']


def test_the_domain_of_a_tab_is_a_filter_and_still_applies(app):
    """`resolve` skips behaviors, never the view's own conditions."""
    created = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'chapters': [
                     {'op': 'create', 'id': -2, 'values': {'title': 'Arrakis', 'kind': 'body'}},
                     {'op': 'create', 'id': -3, 'values': {'title': 'Ecology',
                                                           'kind': 'appendix'}},
                 ]}},
    }))

    tab = ok(load_tree({'page': 'book_appendix_form', 'id': created['id']}))
    assert [row['values']['title'] for row in tab['children']['appendix']] == ['Ecology']


def test_loading_a_record_that_is_not_there(app):
    assert load_tree({'page': 'book_form', 'id': 999})['code'] == 404


def test_the_save_hands_back_the_tree_as_the_database_holds_it(app):
    author = make_author(app, 'Herbert')
    data = ok(save_tree({
        'page': 'book_form',
        'root': {'op': 'create', 'id': -1, 'values': {'title': 'Dune'},
                 'children': {'authors': [
                     {'op': 'create', 'id': -2, 'values': {'author_id': author}}]}},
    }))

    row = data['root']['children']['authors'][0]
    assert row['id'] == data['id_map'][-2]
    assert row['values']['book_id'] == data['id']


# ── A collection tied by a key that is not the primary one ─────────────────

def test_a_new_parent_hands_its_minted_key_to_the_children(app):
    """The code exists only after the flush, like an id: insert is one gesture."""
    data = ok(save_tree({
        'page': 'visit_form',
        'root': {'op': 'create', 'id': -1, 'values': {'place': 'Colle'},
                 'children': {'reports': [
                     {'op': 'create', 'id': -2, 'values': {'text': 'meal delivered'}}]}},
    }))

    visit = rows(app, 'Visit')[0]
    assert len(visit['code']) == 36
    assert [r['visit_code'] for r in rows(app, 'Report')] == [visit['code']]
    assert data['root']['children']['reports'][0]['values']['text'] == 'meal delivered'


def test_a_child_added_to_a_saved_parent_gets_its_key(app):
    created = ok(save_tree({'page': 'visit_form',
                            'root': {'op': 'create', 'id': -1, 'values': {'place': 'Colle'}}}))

    ok(save_tree({
        'page': 'visit_form',
        'root': {'op': 'update', 'id': created['id'], 'values': {},
                 'children': {'reports': [
                     {'op': 'create', 'id': -2, 'values': {'text': 'late'}}]}},
    }))

    assert rows(app, 'Report')[0]['visit_code'] == rows(app, 'Visit')[0]['code']


def test_load_finds_the_children_by_the_key_they_cite(app):
    """Rows written elsewhere with the code — by a terminal — show up on their own."""
    first = ok(save_tree({'page': 'visit_form',
                          'root': {'op': 'create', 'id': -1, 'values': {'place': 'A'}}}))
    second = ok(save_tree({'page': 'visit_form',
                           'root': {'op': 'create', 'id': -1, 'values': {'place': 'B'}}}))
    code_of = {v['id']: v['code'] for v in rows(app, 'Visit')}

    report = app.find_model_class('Report')
    with app.get_session() as session:
        session.add(report(text='for A', visit_code=code_of[first['id']]))
        session.add(report(text='for B', visit_code=code_of[second['id']]))
        session.add(report(text='orphan', visit_code='never-arrived'))
        session.commit()

    root = ok(load_tree({'page': 'visit_form', 'id': first['id']}))

    assert [r['values']['text'] for r in root['children']['reports']] == ['for A']


def test_a_child_cannot_cite_another_parent_by_key(app):
    """The reparenting refusal holds whatever column ties the two."""
    created = ok(save_tree({'page': 'visit_form',
                            'root': {'op': 'create', 'id': -1, 'values': {'place': 'A'}}}))

    result = save_tree({
        'page': 'visit_form',
        'root': {'op': 'update', 'id': created['id'], 'values': {},
                 'children': {'reports': [
                     {'op': 'create', 'id': -2,
                      'values': {'text': 'x', 'visit_code': 'someone-else'}}]}},
    })

    assert result['code'] == 400
    assert rows(app, 'Report') == []
