"""Tests for `field: true` on a junction target: a junction read as a value.

The end that declares it gets a field holding the keys it is linked to; the other
end gets nothing. What is checked here is the declaration: where the field
appears, what it says to the client, and the junctions that cannot be one.
"""
import pytest

from test_codegen_relations import build, generate, fresh_registry  # noqa: F401


def pk():
    return {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True}


def tables(link_columns=(), **target1):
    return {
        'Book': {'name': 'books', 'columns': [pk(), {'name': 'title', 'type': 'String'}]},
        'BookTag': {'name': 'book_tags', 'columns': [pk(), {'name': 'name', 'type': 'String'}]},
        'BookTagLink': {
            'name': 'book_tag_links',
            **({'columns': list(link_columns)} if link_columns else {}),
            'many_to_many': {
                'target1': {'table': 'Book.id', 'column': 'book_id',
                            'collection': 'tags', 'field': True, **target1},
                'target2': {'table': 'BookTag.id', 'column': 'tag_id'},
            },
        },
    }


def test_the_end_that_declares_it_gets_the_field(tmp_path, monkeypatch):
    db = build(tmp_path, monkeypatch, tables())

    ref = db.tables['Book'].ref_sets['tags']
    assert (ref.junction.name, ref.owner_column, ref.target_column) == \
        ('BookTagLink', 'book_id', 'tag_id')
    assert (ref.target.name, ref.target_key, ref.owner_key) == ('BookTag', 'id', 'id')
    assert db.tables['BookTag'].ref_sets == {}


def test_the_client_sees_it_as_a_column_of_its_own_type(tmp_path, monkeypatch):
    schema = build(tmp_path, monkeypatch, tables()).get_table_schema()

    tags = next(col for col in schema['Book']['columns'] if col['name'] == 'tags')
    assert tags == {'name': 'tags', 'type': 'RefSet',
                    'ref_set': {'target': 'BookTag', 'field': 'id'}}
    assert 'books' not in [col['name'] for col in schema['BookTag']['columns']]


def test_it_is_not_among_the_stored_columns(tmp_path, monkeypatch):
    """Whoever walks the columns of a table does not meet it unless asking for it."""
    db = build(tmp_path, monkeypatch, tables())
    assert 'tags' not in [col.name for col in db.tables['Book'].effective_columns]


def test_the_generated_model_does_not_change(tmp_path, monkeypatch):
    with_field = generate(tmp_path, monkeypatch, tables())
    plain = tables()
    del plain['BookTagLink']['many_to_many']['target1']['field']
    assert generate(tmp_path, monkeypatch, plain) == with_field


def test_the_field_needs_a_named_collection(tmp_path, monkeypatch):
    declared = tables()
    del declared['BookTagLink']['many_to_many']['target1']['collection']
    with pytest.raises(ValueError, match='without a `collection:`'):
        build(tmp_path, monkeypatch, declared)


def test_a_junction_whose_rows_carry_data_is_refused(tmp_path, monkeypatch):
    """A note on the pair would be dropped by every write of the list."""
    notes = {'name': 'notes', 'type': 'String', 'nullable': True}
    with pytest.raises(ValueError, match='carry notes'):
        build(tmp_path, monkeypatch, tables([notes]))


def test_a_column_filled_by_itself_is_no_obstacle(tmp_path, monkeypatch):
    stamp = {'name': 'created_at', 'type': 'DateTime', 'default': 'datetime.now'}
    db = build(tmp_path, monkeypatch, tables([stamp]))
    assert 'tags' in db.tables['Book'].ref_sets
