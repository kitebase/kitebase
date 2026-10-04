"""Registering kitebase's routes — on its own application, and inside someone else's.

`register_flask` and `register_fastapi` take whatever target they are given, so
the same call serves the two deployments that until now were one: kitebase as
the owner of the process, and kitebase as a guest of an application that already
exists. The tests below are mostly about the second one, because it is the one
nothing exercised: what has to be proven is not that the routes answer, but
that **nothing outside them moved** — the host's routes, its hooks, and its
JSON contract, which in the deployment that prompted this belongs to an API
already in production on handheld terminals.

No database and no plugin tree here: the registration layer only ever touches
`kitebase_app.cp` and `plugins.config`, and driving it with stubs is what keeps
these tests about routing instead of about everything else.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

import kitebase.server_utils as srv
from kitebase.db import BaseApp
from kitebase.endpoints import CommandProcessor, _ENDPOINTS

SECRET = 'test-secret-key'

CONFIG = {
    'name': 'routes-test',
    'version': '1.0',
    'api': {'prefix': 'kitebase', 'endpoint_prefix': 'endpoint'},
    'authentication': {'context_fields': ['id', 'username']},
}


@pytest.fixture
def kitebase_app():
    """A stub application: a command processor with a few endpoints on it."""
    registered = []

    def register(name, func):
        _ENDPOINTS[name] = func
        registered.append(name)

    register('echo', lambda params: {'seen': params})
    register('a_date', lambda params: {'when': date(2026, 8, 24),
                                       'at': datetime(2026, 8, 24, 10, 30)})
    # Keys of mixed type: a descriptor carries them, and sorting them raises.
    register('mixed_keys', lambda params: {1: 'one', 'two': 2})
    register('boom', _raise)
    register('who', lambda params: BaseApp.get_context())
    # A refusal with something to say: the field errors of a form.
    register('refuse', lambda params: {
        'status': 'error', 'code': 400, 'message': 'not changed',
        'data': {'errors': {'confirm': 'the two do not match'}}})

    cp = CommandProcessor()
    cp.endpoints = dict(_ENDPOINTS)
    yield SimpleNamespace(cp=cp)

    for name in registered:
        _ENDPOINTS.pop(name, None)
    BaseApp.set_context(None)


def _raise(params):
    raise KeyError('missing thing')


@pytest.fixture
def plugins():
    return SimpleNamespace(config=dict(CONFIG))


def token(last_refresh=None, **claims):
    """A valid bearer token; `last_refresh=0` is one due for a refresh."""
    now = datetime.now(timezone.utc)
    payload = {
        'id': 1,
        'username': 'tester',
        'exp': now + timedelta(hours=1),
        'last_refresh': now.timestamp() if last_refresh is None else last_refresh,
    }
    payload.update(claims)
    return jwt.encode(payload, SECRET, algorithm='HS256')


def bearer(**kwargs):
    return {'Authorization': f'Bearer {token(**kwargs)}'}


# ── Flask, owning the process ─────────────────────────────────────────────────

@pytest.fixture
def flask_client(kitebase_app, plugins):
    """kitebase registered straight onto the application, as a server does."""
    from flask import Flask

    app = Flask(__name__)
    srv.register_flask(app, kitebase_app, plugins, SECRET)
    return app.test_client()


def test_the_dispatcher_carries_an_operation(flask_client):
    res = flask_client.post('/kitebase/endpoint/echo', json={'a': 1},
                            headers=bearer())

    assert res.status_code == 200
    assert res.get_json() == {'status': 'success', 'data': {'seen': {'a': 1}},
                              'status_code': 200}


def test_info_lives_under_the_prefix(flask_client):
    """Not at the root: there it would collide with a host that has one."""
    assert flask_client.get('/kitebase/info').status_code == 200
    assert flask_client.get('/info').status_code == 404


def test_no_token_is_refused(flask_client):
    res = flask_client.post('/kitebase/endpoint/echo', json={})

    assert res.status_code == 401
    assert res.get_json()['status'] == 'error'


def test_an_invalid_token_is_refused(flask_client):
    res = flask_client.post('/kitebase/endpoint/echo', json={},
                            headers={'Authorization': 'Bearer not-a-token'})

    assert res.status_code == 401


def test_a_failing_endpoint_answers_with_its_status(flask_client):
    res = flask_client.post('/kitebase/endpoint/boom', json={}, headers=bearer())

    assert res.status_code >= 400
    assert res.get_json()['status'] == 'error'


def test_a_stale_token_comes_back_refreshed(flask_client):
    res = flask_client.post('/kitebase/endpoint/echo', json={},
                            headers=bearer(last_refresh=0))

    assert res.status_code == 200
    assert 'X-New-Token' in res.headers
    assert jwt.decode(res.headers['X-New-Token'], SECRET,
                      algorithms=['HS256'])['username'] == 'tester'


def test_a_fresh_token_is_left_alone(flask_client):
    res = flask_client.post('/kitebase/endpoint/echo', json={}, headers=bearer())

    assert 'X-New-Token' not in res.headers


def test_dates_go_out_as_iso(flask_client):
    """ISO 8601, the form the endpoints accept back on write — and without
    replacing the application's JSON provider to get it."""
    res = flask_client.post('/kitebase/endpoint/a_date', json={}, headers=bearer())

    assert res.get_json()['data'] == {'when': '2026-08-24', 'at': '2026-08-24T10:30:00'}


def test_keys_of_mixed_type_do_not_raise(flask_client):
    res = flask_client.post('/kitebase/endpoint/mixed_keys', json={}, headers=bearer())

    assert res.status_code == 200
    assert res.get_json()['data'] == {'1': 'one', 'two': 2}


def test_the_context_is_set_for_the_operation(flask_client):
    """The dispatcher runs the command as the user the token names."""
    res = flask_client.post('/kitebase/endpoint/who', json={}, headers=bearer())

    assert res.get_json()['data']['username'] == 'tester'


def test_the_thread_is_left_as_it_was_found(flask_client):
    """A worker goes back to the pool with no identity on it.

    Inside kitebase's own surface a leftover context is harmless: every dispatch
    sets its own. It is a guest that pays for it — the host's next page on that
    thread would inherit a user nobody chose.
    """
    BaseApp.set_context(None)
    flask_client.post('/kitebase/endpoint/who', json={}, headers=bearer())

    assert not BaseApp.get_context()


# ── Flask, as a guest ─────────────────────────────────────────────────────────

@pytest.fixture
def host_app(kitebase_app, plugins):
    """An application that already exists, with kitebase mounted inside it.

    The host's route serves a date through *its* JSON provider, which is the
    contract kitebase must not touch.
    """
    from flask import Blueprint, Flask, jsonify

    app = Flask(__name__)

    @app.route('/api/v1/status')
    def status():
        return jsonify({'ok': True, 'stamp': date(2026, 8, 24)})

    @app.route('/')
    def home():
        return 'the host'

    bp = Blueprint('kitebase', __name__)
    srv.register_flask(bp, kitebase_app, plugins, SECRET)
    app.register_blueprint(bp)

    return app


def test_the_guest_answers_where_it_was_mounted(host_app):
    res = host_app.test_client().post('/kitebase/endpoint/echo', json={'a': 1},
                                      headers=bearer())

    assert res.status_code == 200
    assert res.get_json()['data'] == {'seen': {'a': 1}}


def test_the_host_keeps_its_own_routes(host_app):
    client = host_app.test_client()

    assert client.get('/').data == b'the host'
    assert client.get('/api/v1/status').get_json()['ok'] is True


def test_the_host_json_contract_is_untouched(host_app):
    """Flask serializes dates as RFC 1123. kitebase wants ISO, and gets it on
    its own responses only: a host whose clients parse RFC 1123 keeps it."""
    res = host_app.test_client().get('/api/v1/status')

    assert res.get_json()['stamp'] == 'Mon, 24 Aug 2026 00:00:00 GMT'


def test_the_refresh_hook_does_not_reach_the_host(host_app):
    """`after_request` registered on the blueprint runs for the blueprint's
    requests. A stale token on a kitebase route refreshes; the host's routes
    never grow a header they did not ask for."""
    client = host_app.test_client()

    assert 'X-New-Token' in client.post('/kitebase/endpoint/echo', json={},
                                        headers=bearer(last_refresh=0)).headers
    assert 'X-New-Token' not in client.get('/api/v1/status').headers


def test_the_host_decides_where_the_blueprint_hangs(kitebase_app, plugins):
    """With `prefix=''` the routes are relative, and the mount point is the
    host's to choose."""
    from flask import Blueprint, Flask

    app = Flask(__name__)
    bp = Blueprint('kitebase', __name__, url_prefix='/admin/kb')
    srv.register_flask(bp, kitebase_app, plugins, SECRET, prefix='')
    app.register_blueprint(bp)

    client = app.test_client()

    assert client.post('/admin/kb/endpoint/echo', json={}, headers=bearer()).status_code == 200
    assert client.post('/kitebase/endpoint/echo', json={}, headers=bearer()).status_code == 404


def test_registration_hands_back_the_middleware(kitebase_app, plugins):
    """The same identity enters by other doors — a server-rendered page logging
    a person in — and it must not build a second middleware to do it."""
    from flask import Flask

    auth = srv.register_flask(Flask(__name__), kitebase_app, plugins, SECRET)

    assert isinstance(auth, srv.AuthMiddleware)
    assert auth.secret_key == SECRET


# ── FastAPI, both ways ────────────────────────────────────────────────────────

@pytest.fixture
def fastapi_client(kitebase_app, plugins):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    srv.register_fastapi(app, kitebase_app, plugins, SECRET)
    return TestClient(app)


def test_fastapi_dispatches_the_same_way(fastapi_client):
    res = fastapi_client.post('/kitebase/endpoint/echo', json={'a': 1},
                              headers=bearer())

    assert res.status_code == 200
    assert res.json()['data'] == {'seen': {'a': 1}}


def test_fastapi_refuses_without_a_token(fastapi_client):
    assert fastapi_client.post('/kitebase/endpoint/echo', json={}).status_code == 401


def test_fastapi_refreshes_a_stale_token(fastapi_client):
    """No application middleware carries the header: the dependency writes it
    on the response, which is what lets a router be a valid target."""
    res = fastapi_client.post('/kitebase/endpoint/echo', json={},
                              headers=bearer(last_refresh=0))

    assert 'X-New-Token' in res.headers


def test_fastapi_dates_go_out_as_iso(fastapi_client):
    res = fastapi_client.post('/kitebase/endpoint/a_date', json={}, headers=bearer())

    assert res.json()['data'] == {'when': '2026-08-24', 'at': '2026-08-24T10:30:00'}


def test_fastapi_as_a_guest_on_a_router(kitebase_app, plugins):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get('/api/v1/status')
    def status():
        return {'ok': True}

    router = APIRouter()
    srv.register_fastapi(router, kitebase_app, plugins, SECRET)
    app.include_router(router)

    client = TestClient(app)

    assert client.get('/api/v1/status').json() == {'ok': True}
    assert client.post('/kitebase/endpoint/echo', json={'a': 1},
                       headers=bearer()).json()['data'] == {'seen': {'a': 1}}


def test_fastapi_leaves_the_thread_as_it_was_found(fastapi_client):
    BaseApp.set_context(None)
    fastapi_client.post('/kitebase/endpoint/who', json={}, headers=bearer())

    assert not BaseApp.get_context()


def test_the_host_route_after_a_kitebase_one_has_no_identity(host_app):
    """The scenario the cleanup exists for, end to end: an API request, then a
    page of the host on the same worker."""
    client = host_app.test_client()

    client.post('/kitebase/endpoint/who', json={}, headers=bearer())

    assert not BaseApp.get_context()
    assert client.get('/api/v1/status').get_json()['ok'] is True


def test_both_frameworks_answer_the_same(flask_client, fastapi_client):
    """The one property the pair exists to hold: what a client sees does not
    depend on which of the two is serving."""
    flask_res = flask_client.post('/kitebase/endpoint/echo', json={'a': 1},
                                  headers=bearer())
    fastapi_res = fastapi_client.post('/kitebase/endpoint/echo', json={'a': 1},
                                      headers=bearer())

    assert flask_res.get_json() == fastapi_res.json()


def test_both_frameworks_refuse_the_same(flask_client, fastapi_client):
    """Parity on the unhappy path too. FastAPI used to raise an HTTPException
    here, which came back in FastAPI's shape (`{"detail": …}`) and not in
    kitebase's — and, in a mounted deployment, in whatever shape the host's
    exception handlers give it."""
    flask_res = flask_client.post('/kitebase/endpoint/echo', json={})
    fastapi_res = fastapi_client.post('/kitebase/endpoint/echo', json={})

    assert flask_res.status_code == fastapi_res.status_code == 401
    assert flask_res.get_json() == fastapi_res.json()
    assert flask_res.get_json()['status'] == 'error'


def test_both_frameworks_report_a_failure_the_same(flask_client, fastapi_client):
    flask_res = flask_client.post('/kitebase/endpoint/boom', json={}, headers=bearer())
    fastapi_res = fastapi_client.post('/kitebase/endpoint/boom', json={},
                                      headers=bearer())

    assert flask_res.status_code == fastapi_res.status_code
    assert flask_res.get_json()['message'] == fastapi_res.json()['message']


def test_both_frameworks_carry_the_field_errors_of_a_refusal(flask_client, fastapi_client):
    """A form shows each error under its field: the errors travel in `data`."""
    flask_res = flask_client.post('/kitebase/endpoint/refuse', json={}, headers=bearer())
    fastapi_res = fastapi_client.post('/kitebase/endpoint/refuse', json={},
                                      headers=bearer())

    expected = {'errors': {'confirm': 'the two do not match'}}
    assert flask_res.get_json()['data'] == expected
    assert fastapi_res.json()['data'] == expected
    assert flask_res.status_code == fastapi_res.status_code


def test_fastapi_keys_of_mixed_type_do_not_raise(fastapi_client):
    """A descriptor carries them; sorting them raises. Flask has had this
    covered since it stopped using jsonify — FastAPI needed its own answer."""
    res = fastapi_client.post('/kitebase/endpoint/mixed_keys', json={},
                              headers=bearer())

    assert res.status_code == 200
    assert res.json()['data'] == {'1': 'one', 'two': 2}


def test_fastapi_ignores_the_hosts_exception_handler(kitebase_app, plugins):
    """An application that reshapes its own errors must not reshape kitebase's:
    the refusal is returned, so it never reaches a handler."""
    from fastapi import FastAPI, Request
    from fastapi.exceptions import HTTPException
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def host_errors(request: Request, exc: HTTPException):
        return JSONResponse({'host_says': 'mine'}, status_code=exc.status_code)

    srv.register_fastapi(app, kitebase_app, plugins, SECRET)

    res = TestClient(app).post('/kitebase/endpoint/echo', json={})

    assert res.status_code == 401
    assert res.json()['status'] == 'error'


def test_fastapi_ignores_the_hosts_default_response_class(kitebase_app, plugins):
    """The FastAPI counterpart of Flask's JSON provider: an application-wide
    encoder kitebase's routes would otherwise inherit."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    class ShoutingResponse(JSONResponse):
        def render(self, content) -> bytes:
            return b'{"host": "took over"}'

    app = FastAPI(default_response_class=ShoutingResponse)
    srv.register_fastapi(app, kitebase_app, plugins, SECRET)

    res = TestClient(app).post('/kitebase/endpoint/a_date', json={},
                               headers=bearer())

    assert res.json()['data'] == {'when': '2026-08-24', 'at': '2026-08-24T10:30:00'}


def test_fastapi_still_refreshes_a_token_on_a_response_it_builds(fastapi_client):
    """The trap the refusal fix could have sprung: a Response returned by a
    handler replaces the injected one, headers and all, so the refreshed token
    has to be written on the response that is actually returned."""
    res = fastapi_client.post('/kitebase/endpoint/echo', json={},
                              headers=bearer(last_refresh=0))

    assert 'X-New-Token' in res.headers
