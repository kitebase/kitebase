"""What a composite column says about all of its parts.

`type: Address` expands into several columns. Before this, each part was built
from the *type's* definition alone, so anything the table declared on the
composite reached nothing: `editable: false` on an address left five writable
columns and no complaint. An application marking who owns a field had the same
silence.

The line drawn here: what names or shapes ONE column stays private (`label`,
and every storage or constraint attribute — `unique: true` on a composite must
not make each part unique on its own); everything else is about the whole
address and reaches every part.
"""
import pytest
import yaml

import kitebase.utils
from kitebase.db import DB, Base
from kitebase.plugins import PluginsManager


@pytest.fixture(autouse=True)
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()


ADDRESS = {
    'label': 'Address',
    'columns': [
        {'name': 'address', 'type': 'String'},
        {'name': 'city', 'type': 'String'},
        {'name': 'country', 'type': 'String', 'length': 3, 'label': 'Country'},
    ],
}


def build(tmp_path, monkeypatch, address_column):
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({
        'types': {'Address': ADDRESS},
        'tables': {'Subject': {'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True},
            address_column,
        ]}},
    }))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(manager)
    manager.load_plugins()

    db = DB()
    db.calc_db(manager)
    return {c.name: c for c in db.tables['Subject'].columns}


def test_an_attribute_of_the_composite_reaches_every_part(tmp_path, monkeypatch):
    cols = build(tmp_path, monkeypatch,
                 {'name': 'residence', 'type': 'Address',
                  'editable': False, 'owner': 'legacy'})

    for part in ('address', 'city', 'country'):
        assert cols[part].attributes.get('editable') is False
        # An attribute kitebase knows nothing about travels the same way: this is
        # how an application says who writes a field.
        assert cols[part].attributes.get('owner') == 'legacy'


def test_the_part_wins_over_the_composite(tmp_path, monkeypatch):
    cols = build(tmp_path, monkeypatch,
                 {'name': 'residence', 'type': 'Address', 'owner': 'legacy'})

    # The type says `country` is 3 long; the table saying something about the
    # whole address must not overwrite what the type knows about one column.
    assert cols['country'].attributes['length'] == 3


def test_words_that_name_one_column_do_not_travel(tmp_path, monkeypatch):
    cols = build(tmp_path, monkeypatch,
                 {'name': 'residence', 'type': 'Address',
                  'label': 'Residence', 'help': 'Where they live'})

    # Five columns all labelled "Residence" would be worse than none.
    assert cols['address'].attributes.get('label') != 'Residence'
    assert cols['country'].attributes.get('label') == 'Country'
    assert cols['city'].attributes.get('help') is None


@pytest.mark.parametrize('attr,value', [
    ('unique', True),
    ('index', True),
    ('nullable', False),
    ('primary_key', True),
    ('length', 7),
    ('default', 'x'),
])
def test_storage_and_constraints_stay_private(tmp_path, monkeypatch, attr, value):
    cols = build(tmp_path, monkeypatch,
                 {'name': 'residence', 'type': 'Address', attr: value})

    for part in ('address', 'city'):
        assert cols[part].attributes.get(attr) != value, (
            f"'{attr}' on a composite must not reach its parts: it would apply "
            f"to each of them on its own"
        )


def test_prefix_still_renames_and_is_not_an_attribute(tmp_path, monkeypatch):
    cols = build(tmp_path, monkeypatch,
                 {'name': 'residence', 'type': 'Address',
                  'prefix': 'res_', 'owner': 'legacy'})

    assert 'res_city' in cols and 'city' not in cols
    assert cols['res_city'].attributes.get('prefix') is None
    assert cols['res_city'].attributes.get('owner') == 'legacy'
