"""The catalog of the forms: every key in use is described, the file holds together."""
from kitebase import catalog, vocabulary


def test_every_key_in_use_is_described():
    # A key the applications use, at a path the catalog knows, has its line
    missing = catalog.undescribed(catalog.load(), vocabulary.load())
    assert missing == []


def test_every_root_is_a_path_in_use_and_a_form():
    cat = catalog.load()
    voc = vocabulary.load()
    for norm, form in cat['roots'].items():
        assert norm in voc, norm
        assert form in cat['forms'], form


def test_every_key_says_what_it_holds_and_means():
    cat = catalog.load()
    for name in cat['forms']:
        for key, spec in catalog.keys_of(cat, name).items():
            assert spec.get('type') or spec.get('form') or spec.get('values'), f'{name}.{key}'
            if 'form' in spec:
                assert spec['form'] in cat['forms'], f'{name}.{key}'


def test_a_type_is_a_column_without_a_name():
    cat = catalog.load()
    keys = catalog.keys_of(cat, 'Type')
    assert 'name' not in keys and 'foreign_key' not in keys
    assert keys['nullable'] == catalog.keys_of(cat, 'Column')['nullable']
    assert 'columns' in keys and 'base' in keys


def test_the_tokens_are_told_by_their_registry():
    names = catalog.tokens(catalog.load())['defaults']['names']
    assert names['$op_date'].startswith('Operational')
    assert {'$now', '$uuid'} <= set(names)


def test_uses_find_the_keys_in_the_tree():
    voc = {'tables.*': ['columns', 'label'], 'tables.*.columns[]': ['name', 'nullable']}
    data = {'tables': {'Book': {'$plugin': 'books', 'label': 'Book',
                                'columns': [{'name': 'title', 'nullable': False}]}}}
    uses = catalog.uses(data, voc, catalog.load())
    assert uses['Table.label'] == [{'path': 'tables.Book.label', 'plugin': 'books', 'value': 'Book'}]
    assert uses['Column.nullable'][0]['path'] == 'tables.Book.columns[0].nullable'
    assert uses['Table.columns'][0]['value'] == '[...]'
