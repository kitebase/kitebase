"""Computing the schema leaves what the plugins declared as they declared it.

Resolution writes into the definitions it works on: a column takes the
attributes of its type, a derived type the columns of its base, a foreign key
the table it points at. It works on its own copy. The merged tree and the
sources of the plugins are read again afterwards, by the dump, the inspector
and the help, as what was written.
"""
import copy

import yaml

import kitebase.utils
from kitebase.app import App
from kitebase.plugins import PluginsManager

TYPES = {
    'ID': {'base': 'Integer', 'primary_key': True, 'autoincrement': True, 'label': 'ID'},
    'Name': {'base': 'String', 'length': 80, 'help': 'How it is called.'},
    'Address': {'columns': [{'name': 'street', 'type': 'Name'}]},
    'FullAddress': {'base': 'Address', 'columns': [{'name': 'city', 'type': 'Name'}]},
}

TABLES = {
    'Publisher': {'columns': [
        {'name': 'id', 'type': 'ID'},
        {'name': 'name', 'type': 'Name'},
        {'name': 'seat', 'type': 'FullAddress', 'prefix': 'seat_'},
    ]},
    'Book': {'columns': [
        {'name': 'id', 'type': 'ID'},
        {'name': 'publisher_id', 'foreign_key': {'target': 'Publisher.id'}},
    ]},
}


def _manager(tmp_path, monkeypatch) -> PluginsManager:
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'types': TYPES, 'tables': TABLES}))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    manager.load_plugins()
    return manager


def test_schema_calculation_leaves_the_declarations_alone(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    merged = copy.deepcopy(manager.data)
    sources = copy.deepcopy(manager.plugins['app'].data)

    db = App()
    db.calc_db(manager)

    # The schema did resolve: the column has what its type says, the derived
    # type the column of its base.
    name = next(c for c in db.tables['Publisher'].columns if c.name == 'name')
    assert name.attributes['length'] == 80
    assert [c.name for c in db.types['FullAddress'].columns] == ['street', 'city']

    assert manager.data == merged
    assert manager.plugins['app'].data == sources


def test_a_schema_is_built_without_an_application(tmp_path, monkeypatch):
    """Types and tables need the plugins and nothing else: no engine, no
    dispatcher, no plugin code loaded."""
    from kitebase.schema import Schema

    manager = _manager(tmp_path, monkeypatch)
    schema = Schema()
    schema.calc(manager)

    assert schema.tables_list == ['Book', 'Publisher']    # the order of the file
    assert [c.name for c in schema.tables['Publisher'].columns] == [
        'id', 'name', 'seat_street', 'seat_city']
    book = schema.tables['Book']
    assert next(c for c in book.columns if c.name == 'publisher_id').db_type.name == 'ID'
