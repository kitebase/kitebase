"""Tests for `base:` on a composite type — deriving Address, and the like.

A composite type expands into several columns on the table that uses it. When
one derives from another, the derived type must carry the columns of the type
it derives from: without that, `base:` looked like it worked and silently
produced a type with only the columns added to it.

A type cannot be redeclared by a second plugin (that guard catches accidental
collisions), so derivation is the only way to extend one.
"""
import pytest
import yaml

import kitebase.utils
from kitebase.db import DB, Base
from kitebase.plugins import PluginsManager
from kitebase.source import Generator


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
        {'name': 'country', 'type': 'String'},
    ]
}


def generate(tmp_path, monkeypatch, types, tables):
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'types': types, 'tables': tables}))

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(manager)
    manager.load_plugins()

    db = DB()
    db.calc_db(manager)

    out = tmp_path / 'generated_model.py'
    Generator(db).generate(filename=str(out))
    return out.read_text()


def table(type_name):
    return {'columns': [{'name': 'id', 'type': 'Integer', 'primary_key': True},
                        {'name': 'addr', 'type': type_name}]}


def columns_of(source, cls):
    body = source.split(f'class {cls}')[1].split('\nclass ')[0]
    return [line.split(':')[0].strip() for line in body.splitlines() if 'mapped_column' in line]


def test_derived_composite_carries_the_columns_of_its_base(tmp_path, monkeypatch):
    source = generate(
        tmp_path, monkeypatch,
        {'Address': ADDRESS,
         'AddressIT': {'base': 'Address',
                       'columns': [{'name': 'cadastral_code', 'type': 'String'}]}},
        {'Plain': table('Address'), 'Italian': table('AddressIT')},
    )

    assert columns_of(source, 'Plain') == ['id', 'address', 'city', 'country']
    # Inherited columns first, in the order the base declares them.
    assert columns_of(source, 'Italian') == ['id', 'address', 'city', 'country', 'cadastral_code']


def test_derived_composite_can_refine_an_inherited_column(tmp_path, monkeypatch):
    source = generate(
        tmp_path, monkeypatch,
        {'Address': ADDRESS,
         'AddressIT': {'base': 'Address',
                       'columns': [{'name': 'country', 'type': 'String', 'length': 2}]}},
        {'Plain': table('Address'), 'Italian': table('AddressIT')},
    )

    assert 'country: Mapped[str] = mapped_column(String(length=2)' in source
    # Refining the derived type leaves the base alone for whoever uses it directly.
    assert 'country: Mapped[str] = mapped_column(String, nullable=True)' in source


def test_inheritance_chains(tmp_path, monkeypatch):
    source = generate(
        tmp_path, monkeypatch,
        {'Address': ADDRESS,
         'AddressIT': {'base': 'Address',
                       'columns': [{'name': 'cadastral_code', 'type': 'String'}]},
         'AddressITFull': {'base': 'AddressIT',
                           'columns': [{'name': 'region', 'type': 'String'}]}},
        {'Full': table('AddressITFull')},
    )

    assert columns_of(source, 'Full') == ['id', 'address', 'city', 'country',
                                          'cadastral_code', 'region']
