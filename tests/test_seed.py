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
from kitebase import access, seed
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


# ── Comparing ──────────────────────────────────────────────────────────────

def states(result):
    return [(c.state, c.key) for c in result]


def test_the_set_just_applied_compares_equal(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    assert states(seed.compare(app, 'Pipeline', PIPELINE)) == [('equal', 'Sales')]
    methods = seed.read_file(tmp_path / 'plugins/bench/seed/base/Method.csv')
    assert states(seed.compare(app, 'Method', methods)) == [('equal', 'WT'), ('equal', 'CASH')]


def test_a_row_meets_by_its_code_whatever_the_case(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    result = seed.compare(app, 'Method', [{'code': 'wt', 'name': 'Bank transfer'},
                                          {'code': 'DD', 'name': 'Direct debit'}])
    assert states(result) == [('different', 'wt'), ('new', 'DD'), ('only_db', 'CASH')]
    assert result[0].changes == [{'path': 'name', 'kind': 'changed',
                                  'db': 'Wire transfer', 'seed': 'Bank transfer'}]
    assert result[0].db_id == rows(app, 'Method', 'id')[0][0]


def test_without_a_code_a_row_meets_by_its_name_and_the_children_are_said(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    changed = [{'name': 'Sales', 'sequence': 20,
                'Stage': [{'name': 'New', 'sequence': 5}, {'name': 'Lost', 'sequence': 3}]}]
    (item,) = seed.compare(app, 'Pipeline', changed)
    assert item.state == 'different'
    assert item.changes == [
        {'path': 'sequence', 'kind': 'changed', 'db': 10, 'seed': 20},
        {'path': 'Stage[New].sequence', 'kind': 'changed', 'db': 1, 'seed': 5},
        {'path': 'Stage[Lost]', 'kind': 'added', 'db': None, 'seed': 'Lost'},
        {'path': 'Stage[Won]', 'kind': 'missing', 'db': 'Won', 'seed': None}]


def test_the_name_is_compared_as_the_installation_reads_it(tmp_path, monkeypatch):
    register_translations('it', {'Sales': 'Vendite', 'New': 'Nuova'})
    app = build(tmp_path, monkeypatch, standard_files(), config={'locale': 'it'})
    assert states(seed.compare(app, 'Pipeline', PIPELINE)) == [('equal', 'Vendite')]


def test_a_row_born_in_another_language_is_met_by_the_text_of_the_file(tmp_path, monkeypatch):
    """The language changed after the birth: the rows keep their text, and are the same rows."""
    app = build(tmp_path, monkeypatch, standard_files(), config={'locale': 'fr'})
    register_translations('fr', {'Sales': 'Ventes', 'New': 'Nouvelle'})
    (item,) = seed.compare(app, 'Pipeline', PIPELINE)
    assert (item.state, item.key, item.changes) == ('equal', 'Ventes', [])


def test_a_default_left_out_is_the_default(tmp_path, monkeypatch):
    """`active: true` written or not, and the CSV's text, are the same row."""
    app = build(tmp_path, monkeypatch, standard_files())
    explicit = [dict(PIPELINE[0], active='true')]
    assert states(seed.compare(app, 'Pipeline', explicit)) == [('equal', 'Sales')]


def test_a_name_twice_is_ambiguous_and_never_matched(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    with app.get_session() as s:
        s.add(app.models['Pipeline'](name='Sales', sequence=30))
        s.commit()
    result = seed.compare(app, 'Pipeline', PIPELINE + [{'name': 'Renewals'}, {'name': 'Renewals'}])
    assert states(result) == [('ambiguous', 'Sales'), ('ambiguous', 'Renewals'),
                              ('ambiguous', 'Renewals')]
    assert result[0].note == '2 rows of the table have it'
    assert result[1].note == '2 rows of the file have it'


def test_a_key_outside_and_a_label_compare_by_what_they_name(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    # the labels of the file are not those of the export: the parent is the same
    accounts = [{'id': 7, 'code': '40', 'name': 'Revenue'},
                {'code': '40.01', 'name': 'Sales revenue', 'parent_id': 7}]
    assert [c.state for c in seed.compare(app, 'Account', accounts)] == ['equal', 'equal']
    moved = [{'id': 7, 'code': '40', 'name': 'Revenue'}, {'id': 8, 'code': '41', 'name': 'Other'},
             {'code': '40.01', 'name': 'Sales revenue', 'parent_id': 8}]
    result = seed.compare(app, 'Account', moved)
    assert [c.state for c in result] == ['equal', 'new', 'different']
    assert result[2].changes == [{'path': 'parent_id', 'kind': 'changed',
                                  'db': 'Revenue', 'seed': 'Other'}]


def test_rows_meant_for_another_table_are_refused(tmp_path, monkeypatch):
    """Not passed over in silence: the keys the table does not have are said."""
    app = build(tmp_path, monkeypatch, standard_files())
    with pytest.raises(seed.SeedError, match=r'do not fit Method: it has no sequence\.'):
        seed.compare(app, 'Method', [{'code': 'WT', 'sequence': 1}])
    with pytest.raises(seed.SeedError, match=r'it has no Stage\.nope\.'):
        seed.compare(app, 'Pipeline', [{'name': 'Sales', 'Stage': [{'name': 'New', 'nope': 1}]}])
    # a key by code, a label and a collection are the table's own
    assert not seed._unknown_keys(app, 'Order', [{'id': 1, 'reference': 'A1', 'method_id.code': 'WT'}])
    assert not seed._unknown_keys(app, 'Pipeline', PIPELINE)


def test_a_file_a_person_made_may_name_the_columns_by_their_titles(tmp_path, monkeypatch):
    """What the export of a list writes: the id first, the titles of the columns."""
    app = build(tmp_path, monkeypatch, standard_files())
    code = next(c for c in app.tables['Method'].effective_columns if c.name == 'code')
    monkeypatch.setitem(code.attributes, 'label', 'Code')
    rows = seed.parse_rows('id,CODE,Name,Colour\n1,WT,Wire transfer,red\n', '.csv', 'mine.csv')
    assert seed.by_titles(app, 'Method', rows) == [
        {'id': '1', 'code': 'WT', 'name': 'Wire transfer', 'Colour': 'red'}]


def test_a_column_of_one_s_own_starts_with_a_hash_and_is_never_read(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    rows = seed.parse_rows('code,name,#to check,#\nWT,Wire transfer,ask Anna,x\n', '.csv', 'm.csv')
    assert rows == [{'code': 'WT', 'name': 'Wire transfer'}]
    nested = seed.parse_rows('- {name: Sales, "#why": old, Stage: [{name: New, "#": x}]}', '.yaml', 'p')
    assert nested == [{'name': 'Sales', 'Stage': [{'name': 'New'}]}]
    with pytest.raises(seed.SeedError, match='Tip: start the columns of your own with #'):
        seed.compare(app, 'Method', [{'code': 'WT', 'remark': 'x'}])


def test_a_file_of_some_columns_compares_only_those(tmp_path, monkeypatch):
    """A seed leaves out a default; a file a person made leaves out what it does not say."""
    app = build(tmp_path, monkeypatch, standard_files())
    some = [{'name': 'Sales', 'Stage': [{'name': 'New'}, {'name': 'Won'}]}]
    full = seed.compare(app, 'Pipeline', some)
    assert full[0].state == 'different'       # sequence 0, the default, against 10
    (item,) = seed.compare(app, 'Pipeline', some, only_given=True)
    assert (item.state, item.changes) == ('equal', [])
    (item,) = seed.compare(app, 'Pipeline', [{'name': 'Sales'}], only_given=True)
    assert item.state == 'equal'              # no Stage in the file: the stages are not said


def test_a_table_with_nothing_to_meet_by_is_refused(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    app.tables['Order'].search_info['display_field'] = None   # as a table with no text
    with pytest.raises(seed.SeedError, match='nothing to meet its rows by'):
        seed.compare(app, 'Order', ORDERS)


def test_the_cli_lists_what_differs_and_counts_the_rest(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    with app.get_session() as s:
        s.add(app.models['Method'](code='DD', name='Direct debit'))
        s.commit()
    lines = seed.compare_lines(app, seed.seed_files(app, 'bench/base'))
    assert lines == ['bench/base/Method: 2 equal, 1 only db',
                     '  only db    DD',
                     'bench/base/Pipeline: 1 equal']


# ── Writing what was chosen ────────────────────────────────────────────────

RENEWALS = {'name': 'Renewals', 'sequence': 20,
            'Stage': [{'name': 'Expiring', 'sequence': 1}, {'name': 'Signed', 'sequence': 2}]}


def test_only_the_rows_chosen_are_written_whole(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    file = PIPELINE + [RENEWALS, {'name': 'Support'}]
    lines, written = seed.apply_new(app, 'Pipeline', file, ['renewals'], system=True)
    assert written == 1
    assert lines == ['Renewals: written, with 2 rows it owns']
    assert rows(app, 'Pipeline', 'name') == [('Sales',), ('Renewals',)]
    assert rows(app, 'Stage', 'name', 'pipeline_id')[2:] == [('Expiring', 2), ('Signed', 2)]


def test_a_row_no_longer_new_is_skipped_and_said(tmp_path, monkeypatch):
    """Created by hand between the comparison and the writing: never twice."""
    app = build(tmp_path, monkeypatch, standard_files())
    with app.get_session() as s:
        s.add(app.models['Pipeline'](name='Renewals'))
        s.commit()
    lines, written = seed.apply_new(app, 'Pipeline', PIPELINE + [RENEWALS],
                                    ['Renewals', 'Sales', 'Nope'], system=True)
    assert written == 0
    assert lines == ['Sales: skipped, no longer new (equal)',
                     'Renewals: skipped, no longer new (different)',
                     'Nope: skipped, not in the file']
    assert len(rows(app, 'Pipeline', 'id')) == 2


def test_a_label_naming_a_row_the_table_has_is_that_row(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    file = [{'id': 1, 'code': '40', 'name': 'Revenue'},
            {'code': '40.02', 'name': 'Service revenue', 'parent_id': 1}]
    _lines, written = seed.apply_new(app, 'Account', file, ['Service revenue'], system=True)
    assert written == 1
    revenue = rows(app, 'Account', 'id')[0][0]
    assert rows(app, 'Account', 'name', 'parent_id')[-1] == ('Service revenue', revenue)


def test_one_row_that_fails_leaves_nothing(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    seed.apply(app, seed.seed_files(app, 'bench/demo'))
    file = [{'reference': 'B1'}, {'reference': 'B2', 'method_id.code': 'XX'}]
    app.tables['Order'].search_info['display_field'] = 'reference'
    with pytest.raises(seed.SeedError, match="B2: .*XX"):
        seed.apply_new(app, 'Order', file, ['B1', 'B2'], system=True)
    assert rows(app, 'Order', 'reference') == [('A1',), ('A2',)]


def test_the_dry_run_says_and_writes_nothing(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    lines, written = seed.apply_new(app, 'Pipeline', [RENEWALS], ['Renewals'],
                                    system=True, dry_run=True)
    assert (lines, written) == (['Renewals: would be written, with 2 rows it owns'], 1)
    assert rows(app, 'Pipeline', 'name') == [('Sales',)]


def test_as_a_user_a_column_they_may_not_write_takes_its_default(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, standard_files())
    monkeypatch.setattr(app.tables['Pipeline'], '_column_groups', {'sequence': 'pricing'})
    with access.holding(lambda p: p != 'pricing.write'):
        _lines, written = seed.apply_new(app, 'Pipeline', [RENEWALS], ['Renewals'])
    assert written == 1
    assert rows(app, 'Pipeline', 'name', 'sequence')[-1] == ('Renewals', 0)
