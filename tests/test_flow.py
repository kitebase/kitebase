"""The rules of a table, judged on every port that writes it (kitebase.flow).

A rule asks, refuses, informs, or answers with what to do next; nothing is
written until every question has its answer. These tests call the ports the way
a client does, through the dispatcher, so the envelope is part of what they fix:
a question must reach the client as a refusal, never as a save.
"""
import importlib.util

import pytest
import yaml

import kitebase.context
import kitebase.utils
from kitebase import flow
from kitebase.app import App
from kitebase.model_base import Base
from kitebase.endpoints import Dispatcher
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
            # What a duplicate leaves behind, and what it carries regardless
            {'name': 'code', 'type': 'String', 'length': 20, 'nullable': True, 'unique': True},
            {'name': 'ref', 'type': 'String', 'length': 20, 'nullable': True, 'duplicate': False},
            {'name': 'serial', 'type': 'String', 'length': 20, 'nullable': True, 'unique': True,
             'duplicate': True},
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
            {'name': 'position', 'type': 'Integer', 'nullable': True},
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
    # The pages in the order the reader gave them
    'loan_ordered': {'content': {
        'type': 'form', 'source': {'model': 'Loan'},
        'layout': [{'name': 'title'},
                   {'type': 'collection', 'id': 'pages', 'model': 'Page', 'fk': 'loan_id',
                    'sequence': 'position'}],
    }},
    # The same document in two phases, the step to the pages judged
    'loan_doc': {'content': {
        'type': 'form', 'source': {'model': 'Loan'},
        'layout': [
            {'id': 'head', 'type': 'section',
             'columns': [{'id': 'left', 'fields': [{'name': 'title'}, {'name': 'member_id'}]}]},
            {'id': 'relations', 'type': 'tabs', 'pages': [{'id': 'pages_tab', 'layout': [
                {'type': 'collection', 'id': 'pages', 'model': 'Page', 'fk': 'loan_id'}]}]},
        ],
        'phases': [
            {'id': 'head', 'nodes': ['head'], 'next': {'endpoint': 'test_loan_doc_head'}},
            {'id': 'pages', 'nodes': ['relations']},
        ],
    }},
}


@pytest.fixture(autouse=True)
def fresh_registry():
    saved = flow._RULES.snapshot()
    flow._RULES.clear()
    yield
    flow._RULES.restore(saved)
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
    pm.load_plugins()

    instance = App()
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
    return Dispatcher().send({'operation': operation, 'parameters': parameters})


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


def test_save_tree_writes_the_root_when_the_ruled_rows_are_untouched(app):
    # The page declares a collection whose table has rules, but the tree carries
    # none of its rows: the client prunes what was not touched, nothing is skipped.
    @flow.on_write('Page')
    def page_rule(ctx):
        pass

    result = send('save_tree', {'page': 'loan_tree', 'root': {
        'op': 'create', 'id': -1, 'values': {'title': 'Dune', 'member_id': 1},
        'children': {}}})

    assert result['status'] == 'success'
    assert titles(app) == ['Dune'] and titles(app, 'Page') == []


def test_save_tree_counts_the_rows_a_delete_takes_with_it(app):
    # A deleted root takes its rows through the ORM composition, unseen by the
    # tree: their tables count as written, and their rules refuse the delete.
    from kitebase.endpoint_tree import _written_models, page_aggregate

    aggregate = page_aggregate(app, 'loan_tree')
    assert _written_models(aggregate, {'op': 'update', 'id': 1, 'children': {}}) == []
    assert _written_models(aggregate, {'op': 'delete', 'id': 1, 'children': {}}) == ['Page']
    assert _written_models(aggregate, {'op': 'update', 'id': 1, 'children': {
        'pages': [{'id': 5, 'values': {}}]}}) == []


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


def test_a_command_on_the_rows_asks_through_a_form_and_works_on_the_chosen_rows(app):
    app.plugins.data.setdefault('views', {})['page_mark'] = {
        'type': 'form', 'layout': [{'name': 'mark', 'label': 'Mark', 'type': 'String', 'default': '*'}]}

    def mark(ctx):
        typed = ctx.form('mark', 'page_mark', text='How to mark them?')
        for row in ctx.chosen():
            row['text'] += typed['mark']

    def command(answers=None, checked=(), visible=None):
        return flow.propose('Loan', mark, {
            'tree': document(pages=('p1', 'p2', 'p3')), 'answers': answers or {},
            'scope': {'collection': 'pages', 'current': -2, 'checked': list(checked),
                      'visible': visible}})

    asked = command()['$flow']['ask']
    assert asked['kind'] == 'form' and asked['values'] == {'mark': '*'}
    assert asked['view']['layout'][0]['name'] == 'mark'

    ticked = command({'mark': {'mark': '!'}}, checked=[-3])['data']['rows']['pages']
    assert [r['values']['text'] for r in ticked] == ['p1', 'p2!', 'p3']

    every = command({'mark': {'mark': '!'}})['data']['rows']['pages']
    assert [r['values']['text'] for r in every] == ['p1!', 'p2!', 'p3!']

    # Without ticks, a filtered grid means the rows on screen; ticks win over it
    shown = command({'mark': {'mark': '!'}}, visible=[-2, -4])['data']['rows']['pages']
    assert [r['values']['text'] for r in shown] == ['p1!', 'p2', 'p3!']
    ticked = command({'mark': {'mark': '!'}}, checked=[-3], visible=[-2])['data']['rows']['pages']
    assert [r['values']['text'] for r in ticked] == ['p1', 'p2!', 'p3']


def test_a_refusal_can_name_the_phase_where_the_problem_is(app):
    def close(ctx):
        if not ctx.rows['pages']:
            ctx.stay('A loan needs pages', phase='pages')
        ctx.stay('Not today')

    to_pages = propose(close, document(pages=()))
    assert to_pages['code'] == 422 and to_pages['$flow'] == {'phase': 'pages'}
    here = propose(close, document())
    assert here['message'] == 'Not today' and 'phase' not in here.get('$flow', {})


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


# ── apply_rows: the client's applyRows, on the server ───────────────────────
#
# The same cases as aggregate.test.ts: a batch must apply a proposal as the
# form does, or the two paths drift apart.

def stored():
    return {'id': 7, 'values': {'title': '1984'}, 'children': {'authors': [
        {'id': 42, 'values': {'author_id': 3, 'notes': 'first'}, 'children': {}},
        {'id': 43, 'values': {'author_id': 4, 'notes': None}, 'children': {}},
    ]}}


def test_apply_rows_touches_known_rows_creates_new_ones_removes_the_rest():
    tree, seq = stored(), [0]

    flow.apply_rows(tree, 'authors', [
        {'id': 43, 'values': {'author_id': 4, 'notes': 'second'}},
        {'values': {'author_id': 5, 'notes': None}},
    ], seq)

    assert [(r['id'], r.get('op'), r['values'].get('notes')) for r in tree['children']['authors']] \
        == [(43, 'update', 'second'), (-1, 'create', None), (42, 'delete', 'first')]


def test_apply_rows_drops_a_row_never_written_and_keeps_one_already_going():
    tree, seq = stored(), [1]
    tree['children']['authors'][1]['op'] = 'delete'
    tree['children']['authors'].append({'id': -1, 'op': 'create', 'values': {'author_id': 9}})

    flow.apply_rows(tree, 'authors', [{'id': 42, 'values': {'author_id': 3, 'notes': 'first'}}], seq)

    assert [(r['id'], r.get('op')) for r in tree['children']['authors']] == [(42, None), (43, 'delete')]


def test_apply_rows_with_the_same_rows_moves_nothing():
    tree = stored()
    same = [{'id': r['id'], 'values': dict(r['values'])} for r in tree['children']['authors']]

    flow.apply_rows(tree, 'authors', same, [0])

    assert tree == stored()


# ── run_form: the form without its client ───────────────────────────────────

def judge_the_pages():
    from kitebase.endpoints import endpoint

    @endpoint('test_loan_doc_head')
    @flow.proposal('Loan')
    def loan_doc_head(ctx):
        drafts = [r for r in ctx.rows.get('pages', []) if r['text'].startswith('draft')]
        if drafts:
            if not ctx.confirm('drop_drafts', f'{len(drafts)} pages are drafts: drop them?'):
                ctx.stay('Drafts kept')
            ctx.rows['pages'] = [r for r in ctx.rows['pages'] if r not in drafts]
            ctx.info('Drafts dropped')
        ctx.record['note'] = 'judged'


def new_loan(title='Dune'):
    """A document as a batch writes it: values and rows, no ids."""
    return {'values': {'title': title, 'member_id': 1},
            'children': {'pages': [{'values': {'text': t}} for t in ('p1', 'draft a', 'p2')]}}


def written(app):
    """What the database holds, without the keys: two paths give two ids."""
    with app.get_session() as session:
        Loan, Page = app.models['Loan'], app.models['Page']
        return [(loan.title, loan.note, loan.member_id,
                 [p.text for p in session.query(Page).filter(Page.loan_id == loan.id).order_by(Page.id)])
                for loan in session.query(Loan).order_by(Loan.id)]


def test_run_form_stops_at_a_question_nobody_answered(app):
    judge_the_pages()

    result = flow.run_form('loan_doc', new_loan())

    assert result['status'] == 'error'
    assert "'drop_drafts'" in result['message']
    assert result['$flow']['ask']['key'] == 'drop_drafts'
    assert titles(app) == []


def test_run_form_writes_what_the_phases_proposed(app):
    judge_the_pages()

    result = flow.run_form('loan_doc', new_loan(), {'drop_drafts': True})

    assert result['status'] == 'success'
    assert result['$flow']['info'] == ['Drafts dropped']
    assert written(app) == [('Dune', 'judged', 1, ['p1', 'p2'])]


def test_run_form_and_the_form_write_the_same_document(app):
    """The client's path, step by step through the dispatcher, then run_form."""
    judge_the_pages()
    answers = {'drop_drafts': True}

    seq = [0]
    doc = flow._with_ids(new_loan(), seq)
    proposed = send('test_loan_doc_head', {'tree': doc, 'answers': answers})
    flow.apply_patch(doc, proposed['data'], seq)
    by_hand = send('save_tree', {'page': 'loan_doc', 'root': flow._created(doc), 'answers': answers})
    assert by_hand['status'] == 'success'

    assert flow.run_form('loan_doc', new_loan(), answers)['status'] == 'success'

    first, second = written(app)
    assert first == second == ('Dune', 'judged', 1, ['p1', 'p2'])


def test_run_form_does_not_edit_a_stored_document(app):
    result = flow.run_form('loan_doc', {**new_loan(), 'id': 1})

    assert result['code'] == 400 and 'new documents' in result['message']


def test_run_form_calls_as_its_caller_and_leaves_the_context_as_it_was(app):
    """The dispatcher replaces the context with the command's: run_form passes
    its caller's on, or each step would run as nobody, and so would the rest
    of the caller's own endpoint once run_form returned."""
    from kitebase.endpoints import endpoint
    seen = []

    @endpoint('test_loan_doc_head')
    @flow.proposal('Loan')
    def loan_doc_head(ctx):
        seen.append((kitebase.context.current() or {}).get('username'))

    kitebase.context.replace({'username': 'erin'})
    try:
        assert flow.run_form('loan_doc', new_loan())['status'] == 'success'
        assert seen == ['erin']
        assert kitebase.context.current() == {'username': 'erin'}
    finally:
        kitebase.context.replace(None)


# ── run_forms: many documents, one act ──────────────────────────────────────

def test_run_forms_writes_every_document_in_one_go(app):
    judge_the_pages()

    result = flow.run_forms('loan_doc', [new_loan('Dune'), new_loan('Emma')],
                            {'drop_drafts': True}, labels=['A1', 'A2'])

    assert result['status'] == 'success'
    assert len(result['data']['ids']) == 2
    assert result['$flow']['info'] == ['Drafts dropped', 'Drafts dropped']
    assert [w[0] for w in written(app)] == ['Dune', 'Emma']


def test_a_document_the_rules_refuse_stops_them_all_and_is_named(app):
    judge_the_pages()

    @flow.on_write('Loan')
    def no_emma(ctx):
        if ctx.record.get('title') == 'Emma':
            ctx.stay('Not Emma')

    result = flow.run_forms('loan_doc', [new_loan('Dune'), new_loan('Emma')],
                            {'drop_drafts': True}, labels=['A1', 'A2'])

    assert result['status'] == 'error' and result['message'] == 'A2: Not Emma'
    assert titles(app) == []


def test_a_document_that_fails_to_write_takes_the_others_back(app):
    """Judged fine, refused by the database: the first is rolled back with it."""
    judge_the_pages()

    @flow.on_write('Loan', 'commit')
    def fail_on_emma(ctx):
        if ctx.record.get('title') == 'Emma':
            raise ValueError('the warehouse is closed')

    result = flow.run_forms('loan_doc', [new_loan('Dune'), new_loan('Emma')],
                            {'drop_drafts': True}, labels=['A1', 'A2'])

    assert result['status'] == 'error'
    assert result['message'] == 'A2: the warehouse is closed'
    assert titles(app) == [] and titles(app, 'Page') == []


def test_run_forms_stops_at_the_first_unanswered_question_and_names_the_document(app):
    judge_the_pages()

    result = flow.run_forms('loan_doc', [new_loan('Dune')], labels=['A1'])

    assert result['code'] == 409 and result['message'].startswith("A1: Unanswered 'drop_drafts'")
    assert titles(app) == []


# ── Sheets: documents read from a spreadsheet (kitebase.sheets) ─────────────

from kitebase.sheets import Ignored, Key, Lookup, SheetImport, Text  # noqa: E402


class LoanSheet(SheetImport):
    page = 'loan_doc'
    collection = 'pages'
    noun, nouns = 'loan', 'loans'
    columns = {
        'loan':   Key(),
        'member': Lookup('Member', 'name', field='member_id', head=True, required=True),
        'title':  Text('title', head=True, required=True),
        'page':   Text('text', required=True),
        'note':   Ignored(),
    }


def xlsx(rows):
    """The bytes of a sheet whose first row is `rows[0]`."""
    import io
    from openpyxl import Workbook
    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def import_sheet(rows, answers=None, name='loans.xlsx'):
    from kitebase.media import UploadedFile
    return LoanSheet().run({'file': UploadedFile(name, iter([xlsx(rows)])), 'answers': answers})


HEADER = ['loan', 'member', 'title', 'page', 'note']


def test_a_sheet_becomes_one_document_per_key_written_together(app):
    judge_the_pages()
    result = import_sheet([HEADER,
                           ['L1', 'erin', 'Dune', 'p1', 'for the reader'],
                           ['L1', 'Erin', 'Dune', 'p2', None],
                           ['L2', 'Erin', 'Emma', 'draft x', None],
                           ['L2', 'Erin', 'Emma', 'q1', None]], {'drop_drafts': True})

    assert result['status'] == 'success', result
    assert result['data']['message'] == '2 loans imported'
    assert written(app) == [('Dune', 'judged', 1, ['p1', 'p2']), ('Emma', 'judged', 1, ['q1'])]


def test_the_header_is_checked_first_and_named_whole(app):
    result = import_sheet([['loan', 'Member', 'title', 'pages', 'extra'], ['L1', 'Erin', 'Dune', 'p1', 1]])

    assert result['code'] == 422
    assert result['data']['detail'].split('\n') == [
        'Row 1: missing columns: page',
        'Row 1: columns this import does not know: pages, extra']


def test_the_header_is_found_below_a_preamble_in_any_order_with_columns_of_its_own(app):
    result = import_sheet([['Loans of November'],
                           [None],
                           ['Page', 'my comments', 'Loan', '  MEMBER *', 'title:'],
                           ['p1', 'call Erin', 'L1', 'Erin', 'Dune'],
                           ['p2', None, 'L1', 'Erin', 'Dune']])

    assert result['status'] == 'success', result
    assert result['data']['detail'].split('\n')[-1] == 'Columns not read: my comments'
    assert [(title, pages) for title, _, _, pages in written(app)] == [('Dune', ['p1', 'p2'])]


def test_the_header_repeated_further_down_is_not_a_row(app):
    result = import_sheet([HEADER, ['L1', 'Erin', 'Dune', 'p1', None],
                           HEADER, ['L1', 'Erin', 'Dune', 'p2', None]])

    assert result['status'] == 'success', result
    assert [(title, pages) for title, _, _, pages in written(app)] == [('Dune', ['p1', 'p2'])]


def test_a_sheet_without_titles_is_read_by_position(app):
    result = import_sheet([['L1', 'Erin', 'Dune', 'p1', 'for the reader'],
                           ['L1', 'Erin', 'Dune', 'p2']])

    assert result['status'] == 'success', result
    assert [(title, pages) for title, _, _, pages in written(app)] == [('Dune', ['p1', 'p2'])]


def test_a_column_of_the_layout_named_twice_is_refused(app):
    result = import_sheet([['loan', 'member', 'title', 'page', 'Page'], ['L1', 'Erin', 'Dune', 'p1', 'p2']])

    assert result['code'] == 422
    assert result['data']['detail'] == 'Row 1: column page appears twice'


def test_every_mistake_of_the_rows_is_reported_in_sheet_order_and_nothing_written(app):
    with app.get_session() as session:      # two members with one name: ambiguous
        session.add_all([app.models['Member'](name='Sam'), app.models['Member'](name='sam')])
        session.commit()

    result = import_sheet([HEADER,
                           ['L1', 'Nobody', 'Dune', 'p1', None],
                           ['L2', 'Sam', 'Emma', None, None],
                           ['L3', 'Erin', 'Ulysses', 'p1', None],
                           ['L3', 'Erin', 'Odyssey', 'p2', None]])

    assert result['code'] == 422
    assert result['data']['detail'].split('\n') == [
        'Row 2, member: no Member "Nobody"',
        'Row 3, page: missing',
        'Row 3, member: more than one Member "Sam"',
        'Row 5, title: differs from row 4 of the same loan']
    assert titles(app) == []


def test_a_file_that_is_not_a_sheet_is_refused_before_reading(app):
    from kitebase.media import UploadedFile

    assert LoanSheet().run({'file': UploadedFile('loans.csv', iter([b'a,b']))})['code'] == 415
    assert LoanSheet().run({'file': UploadedFile('loans.xlsx', iter([b'not a zip']))})['code'] == 415

    class Small(LoanSheet):
        max_bytes = 10

    assert Small().run({'file': UploadedFile('loans.xlsx', iter([xlsx([HEADER])]))})['code'] == 413


def test_a_question_of_the_phases_is_asked_once_for_the_whole_sheet(app):
    judge_the_pages()
    rows = [HEADER, ['L1', 'Erin', 'Dune', 'draft', None], ['L2', 'Erin', 'Emma', 'draft', None],
            ['L2', 'Erin', 'Emma', 'p1', None]]

    asked = import_sheet(rows)
    assert asked['code'] == 409 and asked['$flow']['ask']['key'] == 'drop_drafts'

    assert import_sheet(rows, {'drop_drafts': True})['status'] == 'success'


def draft_sheet(rows, sheet=LoanSheet):
    from kitebase.media import UploadedFile
    return sheet().draft({'file': UploadedFile('loans.xlsx', iter([xlsx(rows)]))})


def test_a_draft_opens_the_document_in_its_form_and_writes_nothing(app):
    class Proposing(LoanSheet):
        def propose(self, session, tree):
            tree['values']['note'] = 'from the sheet'

    result = draft_sheet([HEADER, ['L1', 'Erin', 'Dune', 'p1', None],
                          ['L1', 'Erin', 'Dune', 'p2', None]], Proposing)

    assert result['status'] == 'success', result
    assert result['data']['action'] == 'open_form' and result['data']['page'] == 'loan_doc'
    assert result['data']['draft'] == {
        'values': {'member_id': 1, 'title': 'Dune', 'note': 'from the sheet'},
        'children': {'pages': [{'values': {'text': 'p1'}}, {'values': {'text': 'p2'}}]}}
    assert titles(app) == []


def test_a_draft_is_one_document(app):
    result = draft_sheet([HEADER, ['L1', 'Erin', 'Dune', 'p1', None], ['L2', 'Erin', 'Emma', 'p1', None]])

    assert result['code'] == 422
    assert result['message'] == 'The sheet holds 2 loans: a draft is one'


# ── Duplicate: a saved document as a new draft (kitebase.duplicate) ─────────

from kitebase import duplicate as dup  # noqa: E402


@pytest.fixture
def stored_loan(app, request):
    saved = dup._DUPLICATORS.snapshot()
    request.addfinalizer(lambda: dup._DUPLICATORS.restore(saved))
    dup._DUPLICATORS.clear()
    with app.get_session() as session:
        loan = app.models['Loan'](title='Dune', note='n', member_id=1, code='C1', ref='R1', serial='S1')
        session.add(loan)
        session.flush()
        session.add_all([app.models['Page'](text=t, loan_id=loan.id) for t in ('p1', 'p2')])
        session.commit()
        return loan.id


def test_a_duplicate_asks_first_then_opens_the_draft(app, stored_loan):
    asked = send('duplicate', {'page': 'loan_tree', 'id': stored_loan})['data']
    assert asked['action'] == 'endpoint' and asked['confirm'] == f'Duplicate #{stored_loan}?'

    assert send(asked['op'], asked['params'])['data']['action'] == 'open_form'


def test_a_command_that_says_confirm_false_opens_the_draft_at_once(app, stored_loan):
    result = send('duplicate', {'page': 'loan_tree', 'id': stored_loan, 'confirm': False})

    assert result['data']['action'] == 'open_form'


def test_a_duplicate_is_the_document_without_keys_links_and_what_must_not_repeat(app, stored_loan):
    result = send('duplicate', {'page': 'loan_tree', 'id': stored_loan, 'confirm': False})

    assert result['status'] == 'success', result
    assert result['data']['action'] == 'open_form' and result['data']['page'] == 'loan_tree'
    assert result['data']['draft'] == {
        'values': {'title': 'Dune', 'note': 'n', 'member_id': 1, 'serial': 'S1'},
        'children': {'pages': [{'values': {'text': 'p1', 'position': None}, 'children': {}},
                               {'values': {'text': 'p2', 'position': None}, 'children': {}}]}}
    assert titles(app) == ['Dune']


def test_a_duplicator_changes_the_draft(app, stored_loan):
    @dup.duplicator('loan_tree')
    def fresh(tree, choices):
        tree['values']['title'] += ' (copy)'
        tree['children']['pages'] = []

    draft = send('duplicate', {'page': 'loan_tree', 'id': stored_loan, 'confirm': False})['data']['draft']

    assert draft['values']['title'] == 'Dune (copy)' and draft['children']['pages'] == []


def test_a_duplicator_with_options_asks_them_first(app, stored_loan):
    app.plugins.data.setdefault('views', {})['loan_copy'] = {
        'layout': [{'name': 'pages', 'type': 'Boolean', 'default': True}]}

    @dup.duplicator('loan_tree', options='loan_copy')
    def choose(tree, choices):
        if not choices['pages']:
            tree['children']['pages'] = []

    @choose.options_values
    def computed(source):
        # The record as it was read: what the draft leaves behind is still there
        return {'title': f"{source['values']['title']} II, after {source['values']['ref']}"}

    asked = send('duplicate', {'page': 'loan_tree', 'id': stored_loan})['data']
    assert asked['action'] == 'form' and asked['values'] == {'pages': True, 'title': 'Dune II, after R1'}

    then = asked['then']
    answered = send(then['op'], {**then['params'], 'pages': False})['data']
    assert answered['action'] == 'open_form' and answered['draft']['children']['pages'] == []


def test_a_record_that_is_not_there_is_refused_before_asking(app, stored_loan):
    assert send('duplicate', {'page': 'loan_tree', 'id': 999})['code'] == 404


# ── A sheet into the rows of a form already open (SheetImport.into) ─────────

class PageSheet(SheetImport):
    page = 'loan_doc'
    collection = 'pages'
    noun, nouns = 'loan', 'loans'
    columns = {'page': Text('text', required=True), 'note': Ignored()}


def sheet_into(rows, answers=None, pages=('p1',), with_file=True):
    from kitebase.media import UploadedFile
    data = {'tree': document(pages=pages), 'answers': answers or {},
            'scope': {'collection': 'pages', 'checked': []}}
    if with_file:
        data['file'] = UploadedFile('pages.xlsx', iter([xlsx(rows)]))
    return flow.propose('Loan', lambda ctx: PageSheet().into(ctx), data)


def test_a_sheet_into_the_rows_asks_for_the_file_first(app):
    asked = sheet_into([], with_file=False)

    assert asked['code'] == 409
    assert asked['$flow']['ask'] == {'key': 'sheet', 'kind': 'file', 'accept': ['.xlsx']}


def test_a_sheet_into_rows_already_there_asks_whether_to_add_or_replace(app):
    sheet = [['page', 'my notes'], ['s1', 'x'], ['s2', None]]

    asked = sheet_into(sheet)
    assert asked['$flow']['ask']['key'] == 'sheet_rows'

    added = sheet_into(sheet, {'sheet_rows': 'add'})
    assert [(r['id'], r['values']['text']) for r in added['data']['rows']['pages']] == [
        (-2, 'p1'), (None, 's1'), (None, 's2')]
    assert added['$flow']['info'] == ['2 rows from pages.xlsx', 'Columns not read: my notes']

    replaced = sheet_into(sheet, {'sheet_rows': 'replace'})
    assert [r['values']['text'] for r in replaced['data']['rows']['pages']] == ['s1', 's2']


def test_a_sheet_into_an_empty_collection_asks_nothing_and_a_wrong_one_is_refused(app):
    assert [r['values']['text'] for r in sheet_into([['page'], ['s1']], pages=())['data']['rows']['pages']] == ['s1']

    refused = sheet_into([['page', 'note'], [None, 'x']], pages=())
    assert refused['code'] == 422 and 'Row 2, page: missing' in refused['message']


def test_a_collection_with_a_sequence_loads_in_its_order(app, stored_loan):
    with app.get_session() as session:
        Page = app.models['Page']
        for page, position in zip(session.query(Page).order_by(Page.id), (2, 1)):
            page.position = position
        session.commit()

    tree = send('load_tree', {'page': 'loan_ordered', 'id': stored_loan})['data']
    assert [r['values']['text'] for r in tree['children']['pages']] == ['p2', 'p1']
    by_key = send('load_tree', {'page': 'loan_tree', 'id': stored_loan})['data']
    assert [r['values']['text'] for r in by_key['children']['pages']] == ['p1', 'p2']
