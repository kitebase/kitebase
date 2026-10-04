"""auth/token: one login, the host's, and the client takes its token from it.

A host that logs people in itself (`client.login`) hands the adapters a
`host_session(request) -> context | None`. With it, `POST {prefix}/auth/token`
signs that context as the login would. The cookie that authenticates it never
authenticates the dispatcher, which stays Bearer only: a cross-site POST to a
generic dispatcher would otherwise leave authenticated.

The host's session is a plain cookie here, read the same way by both
frameworks: what is under test is kitebase's side, not how a host keeps people in.
"""
import jwt
import pytest

import kitebase.server_utils as srv

from test_server_routes import SECRET, kitebase_app, plugins  # noqa: F401

USERS = {'s3cret': {'id': 7, 'username': 'coordinatore', 'email': 'c@example.org'}}


def host_session(request):
    """The host's session: a cookie naming a logged-in user."""
    return USERS.get(request.cookies.get('host_session', ''))


def broken_session(request):
    raise RuntimeError('session store unreachable')


def _flask(kitebase_app, plugins, session):
    from flask import Flask

    app = Flask(__name__)
    srv.register_flask(app, kitebase_app, plugins, SECRET, host_session=session)
    return app.test_client()


def _fastapi(kitebase_app, plugins, session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    srv.register_fastapi(app, kitebase_app, plugins, SECRET, host_session=session)
    return TestClient(app)


@pytest.fixture(params=['flask', 'fastapi'])
def make(request, kitebase_app, plugins):  # noqa: F811
    build = _flask if request.param == 'flask' else _fastapi

    def make_client(session=host_session, cookie=None):
        client = build(kitebase_app, plugins, session)
        if cookie is not None:
            if hasattr(client, 'set_cookie'):        # Flask
                client.set_cookie('host_session', cookie)
            else:                                    # Starlette (httpx)
                client.cookies.set('host_session', cookie)
        return client

    return make_client


def _json(res):
    return res.get_json() if hasattr(res, 'get_json') else res.json()


def test_a_host_session_gets_a_token(make):
    res = make(cookie='s3cret').post('/kitebase/auth/token', json={})

    assert res.status_code == 200
    payload = jwt.decode(_json(res)['data']['token'], SECRET, algorithms=['HS256'])
    # Signed as the login signs: username and the configured context fields.
    assert payload['id'] == 7
    assert payload['username'] == 'coordinatore'
    assert 'email' not in payload
    assert 'op_date' not in payload


def test_the_token_opens_the_dispatcher(make):
    client = make(cookie='s3cret')
    token = _json(client.post('/kitebase/auth/token', json={}))['data']['token']

    res = client.post('/kitebase/endpoint/who', json={},
                      headers={'Authorization': f'Bearer {token}'})

    assert res.status_code == 200
    assert _json(res)['data']['username'] == 'coordinatore'


def test_without_a_host_session_it_is_a_401(make):
    assert make().post('/kitebase/auth/token', json={}).status_code == 401
    assert make(cookie='forged').post('/kitebase/auth/token', json={}).status_code == 401


def test_only_a_json_body_is_taken(make):
    """A cross-site form can post form data with the cookie; it cannot post JSON."""
    res = make(cookie='s3cret').post('/kitebase/auth/token',
                                     data={'x': '1'})

    assert res.status_code == 415


def test_the_cookie_never_opens_the_dispatcher(make):
    res = make(cookie='s3cret').post('/kitebase/endpoint/who', json={})

    assert res.status_code == 401


def test_a_failing_host_session_is_a_500_not_a_token(make):
    res = make(session=broken_session, cookie='s3cret').post('/kitebase/auth/token',
                                                             json={})

    assert res.status_code == 500
    assert 'token' not in (_json(res).get('data') or {})


@pytest.mark.parametrize('build', [_flask, _fastapi])
def test_without_host_session_there_is_no_route(build, kitebase_app, plugins):  # noqa: F811
    client = build(kitebase_app, plugins, None)

    assert client.post('/kitebase/auth/token', json={}).status_code in (404, 405)


def test_info_tells_the_client_who_logs_in(make, plugins):  # noqa: F811
    plugins.config['client'] = {'role': 'admin', 'login': '/login', 'logout': '/logout'}

    client = _json(make().get('/kitebase/info'))['data']['client']

    assert client == {'role': 'admin', 'base': '/admin',
                      'login': '/login', 'logout': '/logout'}
