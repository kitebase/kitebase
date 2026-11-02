"""The rules of a table, judged on every port that writes it (kitebase.flow).

A rule asks, refuses, informs, or answers with what to do next; nothing is
written until every question has its answer. These tests call the ports the way
a client does, through the dispatcher, so the envelope is part of what they fix:
a question must reach the client as a refusal, never as a save.
"""
import importlib.util

import pytest
import yaml

import kitebase.utils
from kitebase import flow
from kitebase.db import DB, Base
from kitebase.endpoints import CommandProcessor
from kitebase.plugins import PluginsManager
from kitebase.source import Generator

TABLES = {
    'Member': {
        'name': 'members',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'name', 'type': 'String', 'length': 60},
        ],
    },
    'Loan': {
        'name': 'loans',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
            {'name': 'note', 'type': 'String', 'length': 120, 'nullable': True},
            {'name': 'member_id', 'type': 'Integer', 'foreign_key': {'target': 'Member.id'}},
        ],
    },
    'Stamp': {
        'name': 'stamps',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'what', 'type': 'String', 'length': 60},
        ],
    },
    'Page': {
        'name': 'pages',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'text', 'type': 'String', 'length': 60},
            {'name': 'loan_id', 'type': 'Integer',
             'foreign_key': {'target': 'Loan.id', 'owned': True}},
        ],
    },
}

PAGES = {
    'loan_tree': {'content': {
        'type': 'form', 'source': {'model': 'Loan'},
        'layout': [{'name': 'title'},
                   {'type': 'collection', 'id': 'pages', 'model': 'Page', 'fk': 'loan_id'}],
    }},
}


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(flow, '_RULES', {})
    yield
    Base.registry.dispose()
    Base.metadata.clear()


@pytest.fixture
def app(tmp_path, monkeypatch):
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
    spec = importlib.util.spec_from_file_location('flow_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    instance.initialize_db(f'sqlite:///{tmp_path / "bench.sqlite"}', module, check_schema=False)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)

    with instance.get_session() as session:
        session.add(instance.models['Member'](name='Erin'))
        session.commit()
    return instance


def send(operation, parameters):
    return CommandProcessor().send({'operation': operation, 'parameters': parameters})


def create_loan(title='Dune', answers=None, **extra):
    params = {'table': 'Loan', 'method': 'create',
              'data': {'title': title, 'member_id': 1, **extra}}
    if answers is not None:
        params['answers'] = answers
    return send('db', params)


def titles(app, model='Loan'):
    with app.get_session() as session:
        rows = session.query(app.models[model]).all()
        return [getattr(r, 'title', None) or getattr(r, 'what', None) or r.text for r in rows]


def open_loans(ctx):
    Loan = kitebase.utils.get_app().models['Loan']
    return ctx.session.query(Loan).filter(Loan.member_id == ctx.record['member_id']).count()


# ── Without rules nothing changes ───────────────────────────────────────────

def test_a_table_without_rules_saves_as_before(app):
    result = create_loan()

    assert result['status'] == 'success'
    assert '$flow' not in result
    assert titles(app) == ['Dune']


# ── A question, and the same call with its answer ───────────────────────────

def ask_on_second_loan():
    @flow.on_write('Loan')
    def many_open(ctx):
        if ctx.mode == 'insert' and open_loans(ctx) >= 1:
            if not ctx.confirm('many_open', 'Erin has a loan open: lend anyway?',
                               yes='Lend', no='Do not lend'):
                ctx.stay('Not lent')


def test_a_question_without_its_answer_writes_nothing(app):
    ask_on_second_loan()
    create_loan('Dune')

    result = create_loan('Emma')

    assert result['status'] == 'error'
    assert result['code'] == 409
    assert result['$flow']['ask'] == {'key': 'many_open', 'kind': 'confirm',
                                      'text': 'Erin has a loan open: lend anyway?',
                                      'yes': 'Lend', 'no': 'Do not lend'}
    assert titles(app) == ['Dune']


def test_the_same_call_with_the_answer_writes(app):
    ask_on_second_loan()
    create_loan('Dune')

    result = create_loan('Emma', answers={'many_open': True})

    assert result['status'] == 'success'
    assert titles(app) == ['Dune', 'Emma']


def test_answering_no_lets_the_rule_refuse(app):
    ask_on_second_loan()
    create_loan('Dune')

    result = create_loan('Emma', answers={'many_open': False})

    assert result['code'] == 422
    assert result['message'] == 'Not lent'
    assert titles(app) == ['Dune']


def test_a_choice_comes_back_as_its_value(app):
    @flow.on_write('Loan')
    def pick(ctx):
        ctx.record['note'] = ctx.choose('weeks', 'For how long?',
                                        [{'value': '2w', 'label': 'Two weeks'},
                                         {'value': '4w', 'label': 'A month'}])

    asked = create_loan()
    assert asked['$flow']['ask']['kind'] == 'choose'
    assert [o['value'] for o in asked['$flow']['ask']['options']] == ['2w', '4w']

    create_loan(answers={'weeks': '4w'})
    with app.get_session() as session:
        assert session.query(app.models['Loan']).one().note == '4w'


# ── Refusals, lines, changes ─────────────────────────────────────────────────

def test_a_refusal_carries_its_patch_and_writes_nothing(app):
    @flow.on_write('Loan')
    def no_empty(ctx):
        if not ctx.record.get('note'):
            ctx.stay('A note is required', patch={'note': 'to fill'})

    result = create_loan()

    assert result['code'] == 422
    assert result['$flow']['patch'] == {'note': 'to fill'}
    assert titles(app) == []


def test_a_line_for_the_person_travels_with_the_success(app):
    @flow.on_write('Loan')
    def remind(ctx):
        ctx.info('Due back in two weeks')

    result = create_loan()

    assert result['status'] == 'success'
    assert result['$flow']['info'] == ['Due back in two weeks']


def test_a_value_set_by_a_check_rule_is_written(app):
    @flow.on_write('Loan')
    def default_note(ctx):
        ctx.record.setdefault('note', 'set by the rule')

    create_loan()

    with app.get_session() as session:
        assert session.query(app.models['Loan']).one().note == 'set by the rule'


def test_severity_decides_between_refusing_and_asking(app):
    @flow.on_write('Loan')
    def checked(ctx):
        ctx.check(False, 'Over the limit', key='limit',
                  severity=ctx.record.get('note') or 'block')

    assert create_loan()['code'] == 422
    assert create_loan(note='confirm')['code'] == 409
    assert create_loan(note='confirm', answers={'limit': True})['status'] == 'success'


# ── The mode, on every method ───────────────────────────────────────────────

def test_a_rule_sees_the_mode_and_the_stored_row(app):
    seen = []

    @flow.on_write('Loan')
    def watch(ctx):
        seen.append((ctx.mode, ctx.record.get('title'),
                     ctx.original.get('title') if ctx.original else None))

    loan_id = create_loan('Dune')['data']['id']
    send('db', {'table': 'Loan', 'method': 'update', 'id': loan_id, 'data': {'title': 'Emma'}})
    send('db', {'table': 'Loan', 'method': 'delete', 'id': loan_id})

    assert seen == [('insert', 'Dune', None), ('update', 'Emma', 'Dune'),
                    ('delete', 'Emma', 'Emma')]


def test_a_delete_can_be_refused(app):
    @flow.on_write('Loan')
    def keep(ctx):
        if ctx.mode == 'delete':
            ctx.stay('Loans are history')

    loan_id = create_loan()['data']['id']
    result = send('db', {'table': 'Loan', 'method': 'delete', 'id': loan_id})

    assert result['code'] == 422
    assert titles(app) == ['Dune']


# ── The three stages ─────────────────────────────────────────────────────────

def test_a_check_rule_that_writes_fails_and_writes_nothing(app):
    @flow.on_write('Loan')
    def sneaky(ctx):
        ctx.session.add(kitebase.utils.get_app().models['Stamp'](what='sneaked'))
        ctx.session.flush()

    result = create_loan()

    assert result['status'] == 'error'
    assert 'may read, not write' in result['message']
    assert titles(app) == [] and titles(app, 'Stamp') == []


def test_a_commit_rule_writes_with_the_record(app):
    @flow.on_write('Loan', 'commit')
    def stamp(ctx):
        ctx.session.add(kitebase.utils.get_app().models['Stamp'](what=f'lent {ctx.obj.title}'))

    create_loan()

    assert titles(app, 'Stamp') == ['lent Dune']


def test_a_commit_rule_that_fails_takes_the_record_with_it(app):
    @flow.on_write('Loan', 'commit')
    def stamp(ctx):
        ctx.session.add(kitebase.utils.get_app().models['Stamp'](what='half'))
        raise RuntimeError('the ledger is closed')

    result = create_loan()

    assert result['status'] == 'error'
    assert titles(app) == [] and titles(app, 'Stamp') == []


def test_a_commit_rule_cannot_ask(app):
    @flow.on_write('Loan', 'commit')
    def late(ctx):
        ctx.confirm('late', 'Too late to ask?')

    result = create_loan()

    assert result['status'] == 'error'
    assert 'cannot ask' in result['message']
    assert titles(app) == []


def test_an_after_rule_answers_with_actions_on_insert_only(app):
    @flow.on_write('Loan', 'after')
    def ticket(ctx):
        if ctx.mode == 'insert':
            ctx.action(action='download', op='loan_ticket', params={'id': ctx.record['id']})

    created = create_loan()
    loan_id = created['data']['id']
    updated = send('db', {'table': 'Loan', 'method': 'update', 'id': loan_id,
                          'data': {'title': 'Emma'}})

    assert created['$flow']['actions'] == [
        {'action': 'download', 'op': 'loan_ticket', 'params': {'id': loan_id}}]
    assert '$flow' not in updated


def test_an_after_rule_that_fails_leaves_the_record_saved(app):
    @flow.on_write('Loan', 'after')
    def broken(ctx):
        raise RuntimeError('printer on fire')

    result = create_loan()

    assert result['status'] == 'success'
    assert 'broken' in result['$flow']['info'][0]
    assert titles(app) == ['Dune']


def test_a_reloaded_module_replaces_its_rule(app):
    calls = []

    def make():
        def rule(ctx):
            calls.append(1)
        return rule

    first, second = make(), make()
    flow.on_write('Loan')(first)
    flow.on_write('Loan')(second)

    create_loan()
    assert calls == [1]


def test_a_file_imported_under_two_names_registers_once(app, tmp_path):
    # A plugin reachable from two roots (`plugins` and `plugins/libapp`) is
    # imported as `library.loans` and as `libapp.library.loans`.
    rules_file = tmp_path / 'loan_rules.py'
    rules_file.write_text(
        'from kitebase.flow import on_write\n'
        '@on_write("Loan")\n'
        'def remind(ctx):\n'
        '    ctx.info("once")\n')
    for name in ('library.loan_rules', 'libapp.library.loan_rules'):
        spec = importlib.util.spec_from_file_location(name, rules_file)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

    assert create_loan()['$flow']['info'] == ['once']


# ── The other port: save_tree judges the root ───────────────────────────────

def tree(title, answers=None):
    params = {'page': 'loan_tree', 'root': {
        'op': 'create', 'id': -1, 'values': {'title': title, 'member_id': 1},
        'children': {'pages': [{'op': 'create', 'id': -2, 'values': {'text': 'p1'}}]}}}
    if answers is not None:
        params['answers'] = answers
    return send('save_tree', params)


def test_save_tree_asks_the_root_and_writes_nothing_without_the_answer(app):
    ask_on_second_loan()
    create_loan('Dune')

    asked = tree('Emma')
    assert asked['code'] == 409
    assert titles(app) == ['Dune'] and titles(app, 'Page') == []

    saved = tree('Emma', answers={'many_open': True})
    assert saved['status'] == 'success'
    assert titles(app) == ['Dune', 'Emma'] and titles(app, 'Page') == ['p1']


def test_save_tree_refuses_rows_whose_rules_it_would_skip(app):
    @flow.on_write('Page')
    def page_rule(ctx):
        pass

    result = tree('Dune')

    assert result['status'] == 'error'
    assert 'Rules on rows' in result['message']
    assert titles(app) == []


# ── Proposals: what a form suggests on its way, before anything is written ──

def document(title='Dune', pages=('p1', 'p2'), root_id=-1):
    """The tree a form holds: a header and its rows, the shape of save_tree."""
    return {'id': root_id, 'op': 'create', 'values': {'title': title, 'member_id': 1},
            'children': {'pages': [{'id': -(i + 2), 'op': 'create', 'values': {'text': t}}
                                   for i, t in enumerate(pages)]}}


def propose(fn, tree, answers=None):
    return flow.propose('Loan', fn, {'tree': tree, 'answers': answers or {}})


def test_a_proposal_that_changes_nothing_answers_an_empty_patch(app):
    result = propose(lambda ctx: None, document())

    assert result == {'status': 'success', 'data': {}}


def test_a_proposal_sees_the_header_the_rows_and_the_mode(app):
    seen = {}

    def look(ctx):
        seen.update(mode=ctx.mode, title=ctx.record['title'],
                    rows=[(r['id'], r['text']) for r in ctx.rows['pages']])

    propose(look, document())
    assert seen == {'mode': 'insert', 'title': 'Dune', 'rows': [(-2, 'p1'), (-3, 'p2')]}

    propose(look, document(root_id=7))
    assert seen['mode'] == 'update'


def test_a_deleted_row_is_not_among_the_rows(app):
    tree = document()
    tree['children']['pages'].append({'id': 5, 'op': 'delete', 'values': {}})

    result = propose(lambda ctx: None if [r['id'] for r in ctx.rows['pages']] == [-2, -3]
                     else ctx.stay('a deleted row was seen'), tree)
    assert result['status'] == 'success'


def test_the_patch_carries_changed_values_and_changed_collections_whole(app):
    def edit(ctx):
        ctx.record['note'] = 'checked'
        ctx.rows['pages'] = [r for r in ctx.rows['pages'] if r['text'] != 'p1']
        ctx.rows['pages'].append({'text': 'p3'})

    result = propose(edit, document())

    assert result['data'] == {
        'values': {'note': 'checked'},
        'rows': {'pages': [{'id': -3, 'values': {'text': 'p2'}},
                           {'id': None, 'values': {'text': 'p3'}}]},
    }


def test_a_proposal_asks_and_refuses_as_a_rule_does(app):
    def drop(ctx):
        if not ctx.confirm('drop', 'Drop the first page?'):
            ctx.stay('Kept', patch={'note': 'kept'})
        ctx.rows['pages'] = ctx.rows['pages'][1:]

    asked = propose(drop, document())
    assert asked['code'] == 409 and asked['$flow']['ask']['key'] == 'drop'

    refused = propose(drop, document(), {'drop': False})
    assert refused['code'] == 422 and refused['$flow']['patch'] == {'note': 'kept'}

    done = propose(drop, document(), {'drop': True})
    assert [r['id'] for r in done['data']['rows']['pages']] == [-3]


def test_a_proposal_writes_nothing(app):
    def write(ctx):
        ctx.session.add(kitebase.utils.get_app().models['Stamp'](what='proposed'))
        ctx.session.flush()

    with pytest.raises(flow.ReadOnly):
        propose(write, document())
    assert titles(app, 'Stamp') == []


def test_a_proposal_reaches_the_client_through_its_endpoint(app):
    """The envelope a client receives: the patch, and the lines for the person."""
    from kitebase.endpoints import endpoint

    @endpoint('test_loan_head')
    @flow.proposal('Loan')
    def loan_head(ctx):
        ctx.record['note'] = 'seen'
        ctx.info('Looked at it')

    result = send('test_loan_head', {'tree': document()})

    assert result['status'] == 'success'
    assert result['data'] == {'values': {'note': 'seen'}}
    assert result['$flow'] == {'info': ['Looked at it']}
    assert loan_head.proposes.__name__ == 'loan_head'
