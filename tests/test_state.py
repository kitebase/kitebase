"""The state column of a table, which a board shows as columns.

What has to hold: the short form is a column name; a state on a foreign key
names the table its values are rows of; the choices of a column reach the
client; a state that names what is not there is said by `check`, not shown as
an empty board.
"""
import pytest
import yaml

from kitebase import state
from kitebase.app import App
from kitebase.model_base import Base
from kitebase.plugins import PluginsManager

TABLES = {
    'Stage': {'name': 'stages', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'name', 'type': 'String', 'length': 40},
        {'name': 'pipeline_id', 'type': 'Integer'},
        {'name': 'sequence', 'type': 'Integer'},
    ]},
    'Deal': {'name': 'deals',
             'state': {'column': 'stage_id', 'within': 'pipeline_id', 'order': 'sequence'},
             'columns': [
                 {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
                 {'name': 'pipeline_id', 'type': 'Integer'},
                 {'name': 'stage_id', 'type': 'Integer', 'foreign_key': {'target': 'Stage.id'}},
             ]},
    'Order': {'name': 'orders', 'state': 'status', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'status', 'type': 'String', 'length': 12, 'choices': [
            {'value': 'draft', 'label': 'To send'}, {'value': 'sent', 'label': 'Sent'}]},
    ]},
    # Every way to get it wrong, one per table
    'NoColumn': {'name': 'no_column', 'state': 'missing', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True}]},
    'NoValues': {'name': 'no_values', 'state': 'status', 'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'status', 'type': 'String', 'length': 12}]},
    'BadWithin': {'name': 'bad_within',
                  'state': {'column': 'stage_id', 'within': 'team_id', 'folded': 'closed'},
                  'columns': [
                      {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
                      {'name': 'stage_id', 'type': 'Integer', 'foreign_key': {'target': 'Stage.id'}},
                  ]},
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'bench'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}), encoding='utf-8')
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}), encoding='utf-8')
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins']}), encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    pm = PluginsManager()
    pm.load_config('config.yaml')
    pm.load_plugins()
    instance = App()
    instance.calc_db(pm)
    yield instance
    Base.registry.dispose()
    Base.metadata.clear()


def test_the_short_form_is_a_column_and_the_choices_reach_the_client(app):
    order = app.get_table_schema()['Order']
    assert order['state'] == {'column': 'status'}
    status = next(c for c in order['columns'] if c['name'] == 'status')
    assert [c['value'] for c in status['choices']] == ['draft', 'sent']


def test_a_state_on_a_foreign_key_names_the_table_of_its_values(app):
    assert app.get_table_schema()['Deal']['state'] == {
        'column': 'stage_id', 'within': 'pipeline_id', 'order': 'sequence', 'target': 'Stage'}


def test_a_table_without_state_sends_none(app):
    assert 'state' not in app.get_table_schema()['Stage']


def test_what_is_not_there_is_said(app):
    found = {(i['code'], i['path']) for i in state.diagnose(app)}
    assert found == {
        ('state-column', 'tables.NoColumn.state.column'),
        ('state-values', 'tables.NoValues.state.column'),
        ('state-within', 'tables.BadWithin.state.within'),
        ('state-key', 'tables.BadWithin.state.folded'),
    }


def test_a_view_folds_values_of_the_choices_and_says_the_others(app):
    order, deal, stage = app.tables['Order'], app.tables['Deal'], app.tables['Stage']
    assert state.check_board(order, {'folded': ['sent']}, 'pages.p', None) == []
    assert state.check_board(order, {'sum': 'id'}, 'pages.p', None) == []
    [unknown] = state.check_board(order, {'folded': ['sent', 'lost']}, 'pages.p', None)
    assert unknown['code'] == 'kanban-folded' and 'lost not among the choices' in unknown['message']
    # Rows of a table fold in their own column, never by id in the YAML
    [rows] = state.check_board(deal, {'folded': [3]}, 'pages.p', None)
    assert "column the state names as 'folded'" in rows['message']
    [none] = state.check_board(stage, {'folded': ['x']}, 'pages.p', None)
    assert 'declares no state' in none['message']
