"""The seed: a plugin's standard rows, written where a table is born empty.

The bench is the shape the seed has to carry: a pipeline with its stages (an
owned collection, nested in the file), a small table with a code read from a
CSV, an order citing it by code from another set, and an account tree whose
parent is a local label. The export writes the same shape back, and the round
trip — export, apply on an empty database, export again — is the test that
says the two halves agree.
"""
import importlib.util
import json

import pytest
import yaml

import kitebase.utils
from kitebase import seed
from kitebase.app import App
from kitebase.i18n import register_translations
from kitebase.model_base import Base
from kitebase.plugins import PluginsManager
from kitebase.source import Generator


def pk():
    return {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True}


TABLES = {
    'Pipeline': {'name': 'pipelines', 'columns': [
        pk(),
        {'name': 'name', 'type': 'String', 'length': 60},
        {'name': 'sequence', 'type': 'Integer', 'default': 0},
        {'name': 'active', 'type': 'Boolean', 'default': True},
    ]},
    'Stage': {'name': 'stages', 'columns': [
        pk(),
        {'name': 'name', 'type': 'String', 'length': 60},
        {'name': 'sequence', 'type': 'Integer', 'default': 0},
        {'name': 'pipeline_id', 'type': 'Integer',
         'foreign_key': {'target': 'Pipeline.id', 'owned': True}},
    ]},
    'Method': {'name': 'methods', 'code_field': 'code', 'columns': [
        pk(),
        {'name': 'code', 'type': 'String', 'length': 10},
        {'name': 'name', 'type': 'String', 'length': 60},
    ]},
    'Order': {'name': 'orders', 'columns': [
        pk(),
        {'name': 'reference', 'type': 'String', 'length': 20},
        {'name': 'method_id', 'type': 'Integer', 'foreign_key': {'target': 'Method.id'}},
    ]},
    'Account': {'name': 'accounts', 'columns': [
        pk(),
        {'name': 'code', 'type': 'String', 'length': 20},
        {'name': 'name', 'type': 'String', 'length': 60},
        {'name': 'parent_id', 'type': 'Integer', 'foreign_key': {'target': 'Account.id'}},
    ]},
    # Points at a table with no code: nothing portable to write for it
    'Note': {'name': 'notes', 'columns': [
        pk(),
        {'name': 'text', 'type': 'String', 'length': 60},
        {'name': 'order_id', 'type': 'Integer', 'foreign_key': {'target': 'Order.id'}},
    ]},
}

PIPELINE = [{'name': 'Sales', 'sequence': 10,
             'Stage': [{'name': 'New', 'sequence': 1}, {'name': 'Won', 'sequence': 2}]}]
METHODS_CSV = 'code,name\nWT,Wire transfer\nCASH,Cash\n'
ORDERS = [{'reference': 'A1', 'method_id.code': 'wt'}, {'reference': 'A2'}]
ACCOUNTS = [{'id': 1, 'code': '40', 'name': 'Revenue'},
            {'code': '40.01', 'name': 'Sales revenue', 'parent_id': 1}]


@pytest.fixture(autouse=True)
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()


def build(tmp_path, monkeypatch, files, config=None):
    """An application on a sqlite file, its plugin carrying `files` under seed/."""
    plugin = tmp_path / 'plugins' / 'bench'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}), encoding='utf-8')
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}), encoding='utf-8')
    for name, content in files.items():
        path = plugin / 'seed' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins'],
                                   'db_engine': f'sqlite:///{tmp_path / "bench.sqlite"}',
                                   **(config or {})}), encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('KITEBASE_ENGINE', raising=False)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    instance = App()
    instance.calc_db(pm)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)
    monkeypatch.setattr(Base, '__kitebase_app__', instance)

    source = tmp_path / 'model.py'
    Generator(instance).generate(filename=str(source))
    spec = importlib.util.spec_from_file_location('bench_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    instance.open(module, check_schema=False)
    return instance


def standard_files():
    return {'base/Pipeline.yaml': yaml.safe_dump(PIPELINE),
            'base/Method.csv': METHODS_CSV,
            'demo/Order.json': json.dumps(ORDERS),
            'demo/Account.yaml': yaml.safe_dump(ACCOUNTS)}


def rows(app, model, *columns):
    cls = app.models[model]
    with app.get_session() as s:
        return [tuple(getattr(o, c) for c in columns)
                for o in s.query(cls).order_by(cls.id).all()]


def test_the_base_set_fills_the_tables_born_with_the_database(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    assert rows(app, 'Pipeline', 'name', 'active') == [('Sales', True)]
    assert rows(app, 'Stage', 'name', 'pipeline_id') == [('New', 1), ('Won', 1)]
    assert rows(app, 'Method', 'code', 'name') == [('WT', 'Wire transfer'), ('CASH', 'Cash')]


def test_another_set_waits_to_be_asked(tmp_path, monkeypatch):
    """`demo` never by itself; asked, its keys outside the set go by code."""
    app = build(tmp_path, monkeypatch, standard_files())
    assert rows(app, 'Order', 'reference') == []

    lines, ok = seed.apply(app, seed.seed_files(app, 'bench/demo'))
    assert ok, lines
    wt = rows(app, 'Method', 'id', 'code')[0][0]
    assert rows(app, 'Order', 'reference', 'method_id') == [('A1', wt), ('A2', None)]


def test_a_local_label_is_mapped_onto_the_id_written(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    (parent_id, _), (_, parent_of_child) = rows(app, 'Account', 'id', 'parent_id')
    assert parent_of_child == parent_id


def test_a_table_with_rows_is_skipped_and_said(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    lines, ok = seed.apply(app, seed.seed_files(app, 'bench/base'))
    assert ok
    assert any('skipped, Pipeline has 1 rows' in line for line in lines)
    assert len(rows(app, 'Pipeline', 'id')) == 1


def test_a_table_that_already_exists_is_never_seeded_by_itself(tmp_path, monkeypatch):
    """Emptied on purpose, it stays empty: the seed is for a table being born."""
    build(tmp_path, monkeypatch, standard_files())
    Base.registry.dispose()
    Base.metadata.clear()
    import sqlite3
    db = sqlite3.connect(tmp_path / 'bench.sqlite')
    db.execute('DELETE FROM stages')
    db.execute('DELETE FROM pipelines')
    db.commit()
    db.close()
    app = build(tmp_path, monkeypatch, standard_files())
    assert rows(app, 'Pipeline', 'id') == []


def test_a_code_nobody_has_stops_the_file_and_leaves_nothing(tmp_path, monkeypatch):
    files = standard_files()
    files['demo/Order.json'] = json.dumps([{'reference': 'A1'},
                                           {'reference': 'A2', 'method_id.code': 'XX'}])
    app = build(tmp_path, monkeypatch, files)
    lines, ok = seed.apply(app, seed.seed_files(app, 'bench/demo'))
    assert not ok
    assert any("row 2" in line and "XX" in line for line in lines)
    assert rows(app, 'Order', 'id') == []
    assert len(rows(app, 'Account', 'id')) == 2      # the other file goes on


def test_a_plain_key_must_be_a_label_of_the_run(tmp_path, monkeypatch):
    """An id of the database in a file names a different row everywhere."""
    files = standard_files()
    files['demo/Order.json'] = json.dumps([{'reference': 'A1', 'method_id': 1}])
    app = build(tmp_path, monkeypatch, files)
    lines, ok = seed.apply(app, seed.seed_files(app, 'bench/demo'))
    assert not ok
    assert any('method_id.<code>' in line for line in lines)


def test_the_dry_run_writes_nothing(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    lines, ok = seed.apply(app, seed.seed_files(app, 'bench/demo'), dry_run=True)
    assert ok
    assert any('would write 2 rows' in line for line in lines)
    assert rows(app, 'Order', 'id') == []


def test_the_display_field_is_written_in_the_app_s_language(tmp_path, monkeypatch):
    register_translations('it', {'Sales': 'Vendite', 'New': 'Nuova'})
    app = build(tmp_path, monkeypatch, standard_files(), config={'locale': 'it'})
    assert rows(app, 'Pipeline', 'name') == [('Vendite',)]
    assert rows(app, 'Stage', 'name') == [('Nuova',), ('Won',)]


def test_an_unknown_set_is_said(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    with pytest.raises(seed.SeedError, match="No set 'bench/nope'"):
        seed.seed_files(app, 'bench/nope')


# ── Export ─────────────────────────────────────────────────────────────────

def test_the_export_nests_the_collection_and_leaves_the_bookkeeping(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    with app.get_session() as s:
        exported = seed.export_rows(app, s, 'Pipeline')
    # no id nobody cites, no key of the parent, no default left as it is
    assert exported == PIPELINE


def test_the_export_writes_a_key_outside_by_code_and_a_label_where_cited(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    with app.get_session() as s:
        assert seed.export_rows(app, s, 'Order') == [
            {'reference': 'A1', 'method_id.code': 'WT'}, {'reference': 'A2'}]
        accounts = seed.export_rows(app, s, 'Account')
    assert accounts == [{'id': 1, 'code': '40', 'name': 'Revenue'},
                        {'code': '40.01', 'name': 'Sales revenue', 'parent_id': 1}]


def test_the_export_refuses_a_key_it_cannot_write(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    with app.get_session() as s:
        s.add(app.models['Note'](text='x', order_id=1))
        s.commit()
        with pytest.raises(seed.SeedError, match='Order, which declares no code_field'):
            seed.export_rows(app, s, 'Note')


def test_export_then_apply_then_export_gives_the_same_file(tmp_path, monkeypatch):
    """The round trip: what the export writes, the loader reads back as it was."""
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    path, count = seed.export(app, 'Account', 'bench/copy')
    assert count == 2
    first = path.read_text(encoding='utf-8')

    with app.get_session() as s:
        s.query(app.models['Account']).delete()
        s.commit()
    lines, ok = seed.apply(app, seed.seed_files(app, 'bench/copy'))
    assert ok, lines
    path, _ = seed.export(app, 'Account', 'bench/copy')
    assert path.read_text(encoding='utf-8') == first
