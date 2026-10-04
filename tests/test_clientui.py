"""The compiled client: where it is mounted, and how it is served.

`client:` in config.yaml says what kitebase is for an application — the
application itself (`role: app`, at "/") or the admin of a host (`role: admin`,
under /admin/) — and the two adapters serve `<app>/clientui/` there, handing
index.html to any path that is a page of the single-page application rather
than a file.
"""
from types import SimpleNamespace

import pytest

import kitebase.server_utils as srv
from kitebase.diagnostics import _check_client
from kitebase.clientui import client_settings


# -- The rule -----------------------------------------------------------------------

@pytest.mark.parametrize("section, expected", [
    (None, ('app', '', None, None)),
    ({'role': 'admin'}, ('admin', '/admin', None, None)),
    ({'role': 'admin', 'login': '/login', 'logout': '/logout'},
     ('admin', '/admin', '/login', '/logout')),
    ({'role': 'admin', 'base': '/backoffice/'}, ('admin', '/backoffice', None, None)),
    ({'role': 'app', 'base': '/'}, ('app', '', None, None)),
])
def test_the_role_gives_the_defaults(section, expected):
    config = {} if section is None else {'client': section}
    assert tuple(client_settings(config)) == expected


@pytest.mark.parametrize("section, message", [
    ({'role': 'backoffice'}, "client.role 'backoffice'"),
    ({'base': 'admin'}, "must start with '/'"),
    ({'role': 'admin', 'login': 'login'}, "client.login 'login'"),
    ({'role': 'admin', 'logout': 'logout'}, "client.logout 'logout'"),
])
def test_a_mount_nobody_would_find_is_refused(section, message):
    with pytest.raises(ValueError, match=message):
        client_settings({'client': section})


def _issues(config):
    issues = []
    _check_client(SimpleNamespace(pm=SimpleNamespace(config=config)), issues)
    return [(i['severity'], i['code']) for i in issues]


def test_check_reports_a_login_page_the_application_has_no_host_for():
    assert _issues({'client': {'role': 'app', 'login': '/login'}}) == [
        ('warning', 'client-login-without-host')]
    assert _issues({'client': {'role': 'admin', 'login': '/login',
                               'logout': '/logout'}}) == []
    assert _issues({'client': {'role': 'nope'}}) == [('error', 'client-config')]


def test_check_reports_a_host_login_nobody_can_leave():
    """Without a logout, leaving the client keeps the host's session, and
    auth/token hands a token back at once."""
    assert _issues({'client': {'role': 'admin', 'login': '/login'}}) == [
        ('warning', 'client-login-without-logout')]


# -- Serving it ---------------------------------------------------------------------

@pytest.fixture
def app_dir(tmp_path):
    client = tmp_path / 'clientui'
    (client / '_app').mkdir(parents=True)
    (client / 'index.html').write_text('<!doctype html>the client')
    (client / '_app' / 'start.js').write_text('// a chunk')
    return tmp_path


ADMIN = {'client': {'role': 'admin'}}


def _flask(app_dir, config):
    flask = pytest.importorskip('flask')
    app = flask.Flask(__name__)

    @app.route('/')
    def home():
        return 'the host'

    served = srv.serve_client_flask(app, app_dir, config)
    return served, app.test_client()


def _fastapi(app_dir, config):
    fastapi = pytest.importorskip('fastapi')
    testclient = pytest.importorskip('fastapi.testclient')
    app = fastapi.FastAPI()

    @app.get('/')
    def home():
        return 'the host'

    served = srv.serve_client_fastapi(app, app_dir, config)
    return served, testclient.TestClient(app)


@pytest.fixture(params=['flask', 'fastapi'])
def serve(request):
    return _flask if request.param == 'flask' else _fastapi


def test_an_admin_lives_under_admin_and_leaves_the_root_to_the_host(serve, app_dir):
    served, client = serve(app_dir, ADMIN)
    assert served
    assert 'the host' in client.get('/').text
    assert 'the client' in client.get('/admin/').text
    assert client.get('/admin/_app/start.js').text == '// a chunk'


def test_a_page_of_the_client_gets_index_html(serve, app_dir):
    _, client = serve(app_dir, ADMIN)
    response = client.get('/admin/app/rapportini')
    assert response.status_code == 200
    assert 'the client' in response.text


def test_the_application_itself_is_served_at_the_root(app_dir):
    """With `role: app` the client IS the application: nothing else at "/"."""
    flask = pytest.importorskip('flask')
    app = flask.Flask(__name__)
    assert srv.serve_client_flask(app, app_dir, {})
    client = app.test_client()
    assert 'the client' in client.get('/').text
    assert 'the client' in client.get('/login').text


def test_without_a_build_nothing_is_registered(serve, tmp_path):
    served, client = serve(tmp_path, ADMIN)
    assert served is False
    assert client.get('/admin/').status_code == 404
