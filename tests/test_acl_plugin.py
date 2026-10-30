"""The `acl` plugin of the commons: roles declared in YAML, answered at every port.

The bench is an app that includes `acl` from the shared plugins and a plugin of
its own with a library and its `acl.yaml`. Nothing else is called: including
the plugin is what turns access control on, and the tests below go through the
dispatcher with the context a token would carry.
"""
import importlib.util
import sys
from pathlib import Path

import jwt
import pytest
import yaml

import kitebase.utils
from kitebase import access, server_utils
from kitebase.db import DB, Base
from kitebase.endpoints import _ACCESS, _ENDPOINTS, CommandProcessor, endpoint
from kitebase.plugins import PluginsManager
from kitebase.source import Generator

COMMONS = Path(__file__).resolve().parent.parent.parent / 'commons' / 'plugins'
if not (COMMONS / 'acl').is_dir():
    pytest.skip(f'needs the kitebase-commons checkout at {COMMONS}', allow_module_level=True)

TABLES = {
    'Book': {
        'name': 'books',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
        ],
    },
    'Loan': {
        'name': 'loans',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'book_id', 'type': 'Integer', 'foreign_key': {'target': 'Book.id'}},
        ],
    },
}

ACL = {
    'permissions': {'library.renew': {'label': 'Renew a loan'}},
    'roles': {
        'library.member': {'label': 'Member', 'grants': ['Book.read', 'Loan.read']},
        'library.librarian': {
            'label': 'Librarian',
            'implies': ['library.member'],
            'grants': [{'Book': ['create', 'update']}, 'Loan.*', 'library.renew'],
        },
        'library.desk': {
            'label': 'Desk',
            'grants': ['Loan.read', '*.label', '-User.label'],
        },
        'library.office': {'label': 'Office', 'grants': ['*.label']},
        'library.archive': {'label': 'Archive', 'grants': ['Loan.read', 'Shelf.read', '*.lable']},
    },
}

PAGES = {
    'loan_list': {'content': {
        'type': 'table', 'source': {'model': 'Loan'},
        'columns': [{'field': 'id'}, {'field': 'Book.title as title'}],
    }},
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'library'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump(
        {'name': 'library', 'version': '0.0.1', 'depends_on': ['acl']}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}))
    (plugin / 'acl.yaml').write_text(yaml.safe_dump(ACL))
    (plugin / 'pages.yaml').write_text(yaml.safe_dump({'pages': PAGES}))

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({
        'name': 'library',
        'plugins': [{'path': str(COMMONS), 'include': ['acl']}, 'plugins'],
        'acl': {'endpoints': {'library.renew': 'library.renew'}},
    }))
    monkeypatch.chdir(tmp_path)

    # Registries a plugin module fills once per process: put back what the
    # first load did, and take it away after, for the tests that follow.
    fields = list(server_utils._REGISTERED_CONTEXT_FIELDS)
    loaded = sys.modules.get('acl.guard')
    if loaded is not None:
        access._GUARD = loaded.GUARD
        server_utils.add_context_field('roles')
        server_utils.add_context_field('is_admin')
        server_utils.add_context_field('acl_exceptions')

    pm = PluginsManager()
    pm.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()
    instance = DB()
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)
    # what a model asks for its definition (secret columns, groups), as in an app
    monkeypatch.setattr(Base, '__kitebase_app__', instance)
    instance.calc_db(pm)

    source = tmp_path / 'model.py'
    Generator(instance).generate(filename=str(source))
    spec = importlib.util.spec_from_file_location('acl_bench_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    instance.initialize_db(f'sqlite:///{tmp_path / "bench.sqlite"}', module, check_schema=False)

    with instance.get_session() as session:
        User = instance.models['User']
        for name, roles, admin in [('ada', ['library.member'], False),
                                   ('bea', ['library.librarian'], False),
                                   ('cy', [], False),
                                   ('root', [], True)]:
            session.add(User(username=name, password='pw', name=name, roles=roles,
                             email=f'{name}@example.com',
                             is_admin=admin, is_active=True))
        session.add(instance.models['Book'](title='Palomar'))
        session.commit()

    yield instance

    Base.registry.dispose()
    Base.metadata.clear()
    access._GUARD = access.OPEN
    server_utils._REGISTERED_CONTEXT_FIELDS[:] = fields


def as_user(operation, parameters, roles=(), is_admin=False, exceptions=()):
    return CommandProcessor().send({
        'operation': operation, 'parameters': parameters,
        'context': {'id': 1, 'username': 'x', 'roles': list(roles), 'is_admin': is_admin,
                    'acl_exceptions': list(exceptions)}})


MEMBER = ['library.member']
LIBRARIAN = ['library.librarian']
DESK = ['library.desk']


# ── Roles into permissions ───────────────────────────────────────────────────

def test_a_role_gives_what_it_implies(app):
    guard = access._GUARD
    assert guard.permissions(LIBRARIAN) >= {'Book.read', 'Loan.read', 'Book.create',
                                            'Book.update', 'Loan.*', 'library.renew'}
    assert 'Book.delete' not in guard.permissions(LIBRARIAN)


def test_a_cycle_stops_the_start(app):
    acl_guard = sys.modules['acl.guard']
    with pytest.raises(ValueError, match='cycle'):
        acl_guard.resolve_roles({'a': {'implies': ['b']}, 'b': {'implies': ['a']}})


def test_a_condition_is_refused_until_it_is_supported(app):
    acl_guard = sys.modules['acl.guard']
    with pytest.raises(ValueError, match='condition'):
        acl_guard.resolve_roles({'a': {'grants': [{'Loan.read': {'where': {'id': 1}}}]}})


# ── At the ports ─────────────────────────────────────────────────────────────

def test_a_member_reads_and_does_not_write(app):
    assert as_user('query', {'query': {'table': 'Book'}}, MEMBER)['status'] == 'success'
    assert as_user('db', {'table': 'Book', 'method': 'create',
                          'data': {'title': 'x'}}, MEMBER)['code'] == 403


def test_a_librarian_writes_what_the_role_grants(app):
    assert as_user('db', {'table': 'Book', 'method': 'create',
                          'data': {'title': 'Le città invisibili'}}, LIBRARIAN)['status'] == 'success'
    assert as_user('db', {'table': 'Book', 'method': 'delete', 'id': 1}, LIBRARIAN)['code'] == 403


def test_a_wildcard_covers_every_action(app):
    assert as_user('db', {'table': 'Loan', 'method': 'create',
                          'data': {'book_id': 1}}, LIBRARIAN)['status'] == 'success'


def test_no_role_no_table(app):
    assert as_user('query', {'query': {'table': 'Book'}})['code'] == 403


def test_a_table_no_role_reaches_is_closed(app):
    """`User` is in no role: only the administrator reads it."""
    assert as_user('query', {'query': {'table': 'User'}}, LIBRARIAN)['code'] == 403
    assert as_user('query', {'query': {'table': 'User'}}, is_admin=True)['status'] == 'success'


# ── The label ────────────────────────────────────────────────────────────────

LOAN_WITH_TITLE = {'query': {
    'table': 'Loan', 'select': ['Loan.id', 'Book.title'],
    'joins': [{'Book': 'Book.id = Loan.book_id'}]}}


def test_a_wildcard_label_shows_every_reference(app):
    as_user('db', {'table': 'Loan', 'method': 'create', 'data': {'book_id': 1}}, LIBRARIAN)
    result = as_user('query', LOAN_WITH_TITLE, DESK)

    assert result['status'] == 'success'
    assert result['data'][1] == [[1, 'Palomar']]


def test_the_label_does_not_open_the_record(app):
    assert as_user('db', {'table': 'Book', 'method': 'get', 'id': 1}, DESK)['code'] == 403


def test_a_denial_in_the_role_wins_over_its_wildcard(app):
    users = {'query': {'table': 'User', 'select': ['id', 'name']}}
    assert as_user('query', users, DESK)['code'] == 403
    assert as_user('query', users, ['library.office'])['status'] == 'success'


def test_a_denial_does_not_reach_another_role(app):
    users = {'query': {'table': 'User', 'select': ['id', 'name']}}
    assert as_user('query', users, DESK + ['library.office'])['status'] == 'success'


def test_an_authenticated_endpoint_needs_no_role(app):
    assert as_user('get_server_config', {})['status'] == 'success'


# ── Endpoints without access ─────────────────────────────────────────────────

@pytest.fixture
def plugin_endpoints():
    @endpoint('library.renew')
    def renew(params):
        return {'renewed': True}

    @endpoint('library.silent')
    def silent(params):
        return {}

    yield
    for name in ('library.renew', 'library.silent'):
        _ENDPOINTS.pop(name, None)
        _ACCESS.pop(name, None)


def test_an_undeclared_endpoint_is_closed(app, plugin_endpoints):
    assert as_user('library.silent', {}, LIBRARIAN)['code'] == 403
    assert as_user('library.silent', {}, is_admin=True)['status'] == 'success'


def test_the_app_config_opens_an_undeclared_endpoint(app, plugin_endpoints):
    assert as_user('library.renew', {}, LIBRARIAN)['status'] == 'success'
    assert as_user('library.renew', {}, MEMBER)['code'] == 403


# ── Assigning roles ──────────────────────────────────────────────────────────

def test_the_catalogue_lists_the_roles_with_what_they_imply(app):
    result = as_user('acl_roles', {}, is_admin=True)

    roles = {r['value']: r for r in result['data']}
    assert roles['library.librarian'] == {
        'value': 'library.librarian', 'label': 'Librarian',
        'group': 'library', 'implies': ['library.member']}
    assert roles['library.member']['implies'] == []


def test_the_catalogue_is_for_who_reads_the_users(app):
    assert as_user('acl_roles', {}, LIBRARIAN)['code'] == 403


def _effective(roles, **extra):
    data = as_user('acl_effective', {'roles': roles, **extra}, is_admin=True)['data']
    return {t['name']: t['actions'] for t in data['tables']}, data


def test_the_effective_access_names_the_role_that_gives_it(app):
    tables, data = _effective(LIBRARIAN + DESK + ['library.nobody'])

    assert tables['Book']['create'] == {'ok': True, 'by': ['Librarian'], 'denied_by': [],
                                        'forced': None}
    assert tables['Book']['read']['by'] == ['Librarian']     # through `implies`
    assert tables['Book']['delete']['ok'] is False
    assert data['unknown'] == ['library.nobody']
    renew = next(p for p in data['permissions'] if p['code'] == 'library.renew')
    assert renew['label'] == 'Renew a loan' and renew['by'] == ['Librarian']


def test_the_effective_access_shows_a_denial_and_where_it_is(app):
    tables, _data = _effective(DESK)

    assert tables['User']['label'] == {'ok': False, 'by': [], 'denied_by': ['Desk'],
                                       'forced': None}
    assert tables['Book']['label']['by'] == ['Desk']


def test_the_administrator_has_everything(app):
    tables, _data = _effective([], is_admin=True)

    assert all(a['ok'] for t in tables.values() for a in t.values())


# ── Exceptions for one user ──────────────────────────────────────────────────

def test_an_exception_grants_what_no_role_gives(app):
    create = {'table': 'Book', 'method': 'create', 'data': {'title': 'x'}}
    assert as_user('db', create, MEMBER)['code'] == 403
    assert as_user('db', create, MEMBER, exceptions=['+Book.create'])['status'] == 'success'


def test_an_exception_denies_what_a_role_gives_and_wins(app):
    query = {'query': {'table': 'Book'}}
    assert as_user('query', query, LIBRARIAN, exceptions=['-Book.read'])['code'] == 403
    assert as_user('query', query, LIBRARIAN,
                   exceptions=['+Book.read', '-Book.read'])['code'] == 403


def test_the_effective_access_shows_the_exceptions(app):
    tables, data = _effective(MEMBER, exceptions=['+Book.create', '-Loan.read'])

    assert tables['Book']['create'] == {'ok': True, 'by': [], 'denied_by': [], 'forced': '+'}
    assert tables['Loan']['read'] == {'ok': False, 'by': ['Member'], 'denied_by': [],
                                      'forced': '-'}


def test_the_effective_access_names_the_pages_a_user_cannot_read(app):
    _tables, data = _effective(['library.archive'], exceptions=['-Book.label'])

    assert data['problems'] == [{'page': 'loan_list', 'title': None,
                                 'what': 'Book.title', 'grant': 'Book.read or Book.label'}]


def _set_exceptions(exceptions):
    return as_user('db', {'table': 'User', 'method': 'update', 'id': 1,
                          'data': {'acl_exceptions': exceptions}}, is_admin=True)


def test_exceptions_are_signed_permissions_that_exist(app):
    assert _set_exceptions(['+Book.create', '-Loan.*', '+*.label'])['status'] == 'success'
    result = _set_exceptions(['Book.create', '+Book.publish'])
    assert result['status'] == 'error'
    assert 'Book.create' in str(result) and '+Book.publish' in str(result)


def _set_roles(roles):
    return as_user('db', {'table': 'User', 'method': 'update', 'id': 1,
                          'data': {'roles': roles}}, is_admin=True)


def test_declared_roles_are_saved(app):
    assert _set_roles(['library.member', 'library.desk'])['status'] == 'success'


def test_an_unknown_role_is_named_and_nothing_is_saved(app):
    result = _set_roles(['library.member', 'library.membr'])

    assert result['status'] == 'error'
    assert 'library.membr' in str(result)
    with app.get_session() as session:
        assert session.get(app.models['User'], 1).roles == MEMBER


def test_roles_are_a_list_of_codes(app):
    assert _set_roles('library.member')['status'] == 'error'
    assert _set_roles([1, 2])['status'] == 'error'


# ── The token ────────────────────────────────────────────────────────────────

def test_the_login_puts_the_exceptions_in_the_token(app):
    _set_user(app, 'bea', acl_exceptions=['+Book.delete'])
    auth = server_utils.AuthMiddleware(app.pm.config, 'secret')
    token = auth.login(app.cp, {'username': 'bea', 'password': 'pw'})['data']['token']

    assert jwt.decode(token, 'secret', algorithms=['HS256'])['acl_exceptions'] == ['+Book.delete']


def test_the_login_puts_the_roles_in_the_token(app):
    auth = server_utils.AuthMiddleware(app.pm.config, 'secret')
    result = auth.login(app.cp, {'username': 'bea', 'password': 'pw'})

    payload = jwt.decode(result['data']['token'], 'secret', algorithms=['HS256'])
    assert payload['roles'] == LIBRARIAN
    assert payload['is_admin'] is False
    assert payload['src'] == 'login'


def test_check_reads_the_roles_without_complaint(app):
    from kitebase.diagnostics import run_checks
    noise = [i for i in run_checks(app)
             if str(i.get('path', '')).startswith(('roles', 'permissions'))
             and not i['code'].startswith('acl-')]
    assert noise == [], noise


def acl_issues(app, code):
    from kitebase.diagnostics import run_checks
    return {i['path']: i['message'] for i in run_checks(app) if i['code'] == code}


def test_check_names_a_grant_nothing_declares(app):
    found = acl_issues(app, 'acl-grant-unknown')
    assert set(found) == {'roles.library.archive'}
    assert {"'Shelf.read'" in m or "'*.lable'" in m for m in found.values()} == {True}


def test_check_names_a_table_no_role_reads(app):
    assert acl_issues(app, 'acl-table-closed') == {}     # `*.label` reaches them all

    guard = access._GUARD
    roles = guard.roles
    guard.roles = sys.modules['acl.guard'].resolve_roles(
        {'a': {'grants': ['Book.read']}, 'b': {'grants': ['Loan.label']}})
    try:
        assert set(acl_issues(app, 'acl-table-closed')) == {'tables.User', 'tables.UserLog', 'tables.Config'}
    finally:
        guard.roles = roles


def test_check_names_an_endpoint_without_access(app, plugin_endpoints):
    found = acl_issues(app, 'acl-endpoint-undeclared')
    assert 'endpoints.library.silent' in found
    assert 'endpoints.library.renew' not in found      # assigned by the app's config


def test_check_names_a_page_a_role_opens_and_cannot_read(app):
    found = acl_issues(app, 'acl-page-unreadable')
    assert list(found) == ['pages.loan_list']
    assert "'library.archive'" in found['pages.loan_list']


# ── Refresh: what the token says is read again ───────────────────────────────

def _stale(token):
    """The same token, as if issued before the last refresh interval."""
    payload = jwt.decode(token, 'secret', algorithms=['HS256'])
    return jwt.encode({**payload, 'last_refresh': 0}, 'secret', algorithm='HS256')


def _set_user(app, username, **values):
    with app.get_session() as session:
        user = session.query(app.models['User']).filter_by(username=username).one()
        for key, value in values.items():
            setattr(user, key, value)
        session.commit()


def test_a_role_taken_away_counts_from_the_next_refresh(app):
    auth = server_utils.AuthMiddleware(app.pm.config, 'secret')
    token = auth.login(app.cp, {'username': 'bea', 'password': 'pw'})['data']['token']
    _set_user(app, 'bea', roles=MEMBER)

    payload, new_token, error = auth.decode_and_refresh(token)
    assert payload['roles'] == LIBRARIAN        # not due yet: the token stands

    payload, new_token, error = auth.decode_and_refresh(_stale(token))
    assert error is None
    assert payload['roles'] == MEMBER           # this request already sees it
    assert jwt.decode(new_token, 'secret', algorithms=['HS256'])['roles'] == MEMBER


def test_a_disabled_account_is_refused_at_the_refresh(app):
    auth = server_utils.AuthMiddleware(app.pm.config, 'secret')
    token = auth.login(app.cp, {'username': 'bea', 'password': 'pw'})['data']['token']
    _set_user(app, 'bea', is_active=False)

    payload, new_token, error = auth.decode_and_refresh(_stale(token))
    assert payload is None and new_token is None
    assert error == 'Account is inactive'


def test_what_the_host_vouched_for_is_not_read_again(app):
    """auth/token signs the host's word: the user need not be in our table."""
    auth = server_utils.AuthMiddleware(app.pm.config, 'secret')
    token = auth.issue_token({'username': 'guest', 'roles': MEMBER}, source='host')['data']['token']

    payload, new_token, error = auth.decode_and_refresh(_stale(token))
    assert error is None
    assert payload['roles'] == MEMBER
    assert jwt.decode(new_token, 'secret', algorithms=['HS256'])['src'] == 'host'


# ── Column groups: who edits users does not raise anyone's privileges ───────

# Users without the `acl` group, and with it read only.
KEEPER = ['+User.*']
VIEWER = ['+User.*', '+acl.read']


def _user(app, user_id):
    with app.get_session() as session:
        user = session.get(app.models['User'], user_id)
        return {'name': user.name, 'roles': user.roles, 'is_admin': user.is_admin}


def test_a_column_of_a_group_is_not_written_without_its_permission(app):
    result = as_user('db', {'table': 'User', 'method': 'update', 'id': 1, 'data': {
        'name': 'Ada', 'roles': ['library.librarian'], 'is_admin': True}}, exceptions=KEEPER)

    assert result['status'] == 'success'
    assert _user(app, 1) == {'name': 'Ada', 'roles': ['library.member'], 'is_admin': False}


def test_a_new_user_gets_the_default_of_a_column_it_cannot_write(app):
    result = as_user('db', {'table': 'User', 'method': 'create', 'data': {
        'username': 'dee', 'name': 'dee', 'password': 'a long secret', 'email': 'dee@example.com',
        'roles': ['library.librarian'], 'is_admin': True}}, exceptions=VIEWER)

    assert result['status'] == 'success'
    assert _user(app, result['data']['id']) == {'name': 'dee', 'roles': [], 'is_admin': False}


def test_the_group_written_with_its_permission(app):
    as_user('db', {'table': 'User', 'method': 'update', 'id': 1,
                   'data': {'roles': ['library.desk']}},
            exceptions=KEEPER + ['+acl.write'])
    assert _user(app, 1)['roles'] == ['library.desk']


def test_a_record_leaves_out_the_columns_it_cannot_read(app):
    hidden = as_user('db', {'table': 'User', 'method': 'get', 'id': 1}, exceptions=KEEPER)['data']
    shown = as_user('db', {'table': 'User', 'method': 'get', 'id': 1}, exceptions=VIEWER)['data']

    assert hidden['name'] == 'ada'
    assert not {'roles', 'acl_exceptions', 'is_admin'} & set(hidden)
    assert {'roles', 'acl_exceptions', 'is_admin'} <= set(shown)


def test_a_wildcard_query_leaves_them_out(app):
    result = as_user('query', {'format': 'dict', 'query': {'table': 'User'}}, exceptions=KEEPER)

    assert result['status'] == 'success'
    assert 'name' in result['data']['headers']
    assert not {'roles', 'acl_exceptions', 'is_admin'} & set(result['data']['headers'])


def test_naming_them_in_a_query_is_refused(app):
    """In the select, a filter or an ordering: a filter on `is_admin` would
    tell who the administrators are as surely as the column."""
    for query in ({'table': 'User', 'select': ['User.id', 'User.roles']},
                  {'table': 'User', 'select': ['User.id'],
                   'filters': {'conditions': [{'User.is_admin': True}]}},
                  {'table': 'User', 'select': ['User.id'], 'order_by': ['User.is_admin']}):
        result = as_user('query', {'query': query}, exceptions=KEEPER)
        assert result['code'] == 403, query
        assert 'acl.read' in result['message']
    assert as_user('query', {'query': {'table': 'User', 'select': ['User.id', 'User.roles']}},
                   exceptions=VIEWER)['status'] == 'success'


def test_a_list_of_db_get_refuses_them_too(app):
    assert as_user('db', {'table': 'User', 'method': 'get', 'query': {'is_admin': True}},
                   exceptions=KEEPER)['code'] == 403
    assert as_user('db', {'table': 'User', 'method': 'get', 'order_by': 'is_admin'},
                   exceptions=KEEPER)['code'] == 403


def _fields(page):
    found = {}

    def walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get('name'), str):
                found.setdefault(obj['name'], []).append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(page)
    return found


def _tabs(page):
    sections = next(item for item in page['content']['layout'] if item.get('id') == 'sections')
    return [tab['id'] for tab in sections['pages']]


def test_the_user_form_hides_what_cannot_be_read(app):
    page = as_user('get_page', {'id': 'user_form'}, exceptions=KEEPER)['data']

    assert _tabs(page) == ['account']
    assert 'name' in _fields(page)
    assert not {'roles', 'acl_exceptions', 'is_admin'} & set(_fields(page))


def test_the_user_form_shows_read_only_what_cannot_be_written(app):
    page = as_user('get_page', {'id': 'user_form'}, exceptions=VIEWER)['data']
    fields = _fields(page)

    assert _tabs(page) == ['account', 'roles', 'access']
    assert all(f.get('readonly') for name in ('roles', 'acl_exceptions', 'is_admin')
               for f in fields[name])
    assert not fields['name'][0].get('readonly')


def test_a_table_wildcard_does_not_open_a_column_group(app):
    from acl.guard import granted
    assert granted('Book.read', ['*.read'])
    assert not granted('acl.read', ['*.read'])


def test_check_knows_the_permissions_of_the_groups(app):
    from acl.guard import _known_permissions
    assert {'acl.read', 'acl.write'} <= _known_permissions(app, _ACCESS)
