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
    },
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'library'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump(
        {'name': 'library', 'version': '0.0.1', 'depends_on': ['acl']}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}))
    (plugin / 'acl.yaml').write_text(yaml.safe_dump(ACL))

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

    pm = PluginsManager()
    pm.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(pm)
    pm.load_plugins()
    instance = DB()
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)
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


def as_user(operation, parameters, roles=(), is_admin=False):
    return CommandProcessor().send({
        'operation': operation, 'parameters': parameters,
        'context': {'id': 1, 'username': 'x', 'roles': list(roles), 'is_admin': is_admin}})


MEMBER = ['library.member']
LIBRARIAN = ['library.librarian']


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


# ── The token ────────────────────────────────────────────────────────────────

def test_the_login_puts_the_roles_in_the_token(app):
    auth = server_utils.AuthMiddleware(app.pm.config, 'secret')
    result = auth.login(app.cp, {'username': 'bea', 'password': 'pw'})

    payload = jwt.decode(result['data']['token'], 'secret', algorithms=['HS256'])
    assert payload['roles'] == LIBRARIAN
    assert payload['is_admin'] is False


def test_check_reads_the_roles_without_complaint(app):
    from kitebase.diagnostics import run_checks
    noise = [i for i in run_checks(app)
             if str(i.get('path', '')).startswith(('roles', 'permissions'))]
    assert noise == [], noise
