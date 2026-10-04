"""Tests for `default:` and `onupdate:` — the callables behind an audit stamp.

Both were broken in the same place and in the same way. `default: datetime.now()`
is a *call*, evaluated once when the generated model is imported, so every row
of every table carried the instant the server started; and `onupdate` was not
among the recognised column attributes at all, so declaring it did nothing and
said nothing — an updated_at that never updated.

The value is emitted verbatim, so what these tests protect is that a callable
stays a callable, and that `onupdate` reaches the generated column.
"""
import importlib.util
import time

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, configure_mappers

import kitebase.utils
from kitebase.db import DB, Base
from kitebase.plugins import PluginsManager
from kitebase.source import Generator


@pytest.fixture(autouse=True)
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()


def generate(tmp_path, monkeypatch, tables, types=None):
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    model = {'tables': tables}
    if types:
        model['types'] = types
    (plugin / 'model.yaml').write_text(yaml.safe_dump(model))

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


def load(tmp_path, source):
    path = tmp_path / 'generated_model.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location('generated_model', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configure_mappers()
    return module


STAMPED = {
    'Note': {
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'text', 'type': 'String'},
            {'name': 'created_at', 'type': 'DateTime', 'default': 'datetime.now'},
            {'name': 'updated_at', 'type': 'DateTime',
             'default': 'datetime.now', 'onupdate': 'datetime.now'},
        ]
    }
}


def test_onupdate_reaches_the_generated_column(tmp_path, monkeypatch):
    source = generate(tmp_path, monkeypatch, STAMPED)

    assert 'onupdate=datetime.now' in source
    # The callable, not its result: `datetime.now()` would be evaluated on import.
    assert 'default=datetime.now,' in source or 'default=datetime.now)' in source
    assert 'datetime.now()' not in source


def test_rows_inserted_apart_carry_different_stamps(tmp_path, monkeypatch):
    module = load(tmp_path, generate(tmp_path, monkeypatch, STAMPED))
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    session = Session(engine)

    session.add(module.Note(text='first'))
    session.commit()
    time.sleep(0.01)
    session.add(module.Note(text='second'))
    session.commit()

    first, second = session.scalars(select(module.Note).order_by(module.Note.id)).all()
    assert first.created_at != second.created_at


def test_updating_a_row_moves_updated_at_and_leaves_created_at(tmp_path, monkeypatch):
    module = load(tmp_path, generate(tmp_path, monkeypatch, STAMPED))
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    session = Session(engine)

    session.add(module.Note(text='before'))
    session.commit()
    row = session.scalars(select(module.Note)).one()
    created, updated = row.created_at, row.updated_at

    time.sleep(0.01)
    row.text = 'after'
    session.commit()

    assert row.updated_at > updated
    assert row.created_at == created


def test_a_mixin_carries_the_stamps_to_the_tables_that_use_it(tmp_path, monkeypatch):
    """The real shape: the columns come from the TimeStamp type, not from the table."""
    source = generate(
        tmp_path, monkeypatch,
        {'Note': {'mixins': ['TimeStamp'],
                  'columns': [{'name': 'id', 'type': 'Integer', 'primary_key': True}]}},
        types={'TimeStamp': {'columns': [
            {'name': 'created_at', 'type': 'DateTime', 'default': 'datetime.now'},
            {'name': 'updated_at', 'type': 'DateTime',
             'default': 'datetime.now', 'onupdate': 'datetime.now'},
        ]}},
    )

    assert 'onupdate=datetime.now' in source
    assert 'datetime.now()' not in source
