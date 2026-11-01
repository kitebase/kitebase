"""The two media routes on both frameworks: the GET of a signed URL, the upload.

The logic is kitebase.media's and is tested in test_media.py; here what is
proven is the translation, the same on Flask and on FastAPI: the signature
stands in for the login, the headers arrive, a 304 is a 304, the parts of a
multipart form reach `media_upload` and its status comes back as it was.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

import kitebase.server_utils as srv
from kitebase.db import BaseApp
from kitebase.endpoints import CommandProcessor, _ENDPOINTS
from kitebase.media import Media, UploadedFile

SECRET = 'test-secret-key-long-enough-for-hs256'
CONFIG = {'api': {'prefix': 'kitebase'}, 'authentication': {'context_fields': ['id', 'username']}}
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100


def fake_upload(params):
    """Stands in for media_upload: says what the route handed it."""
    upload = params.pop('file')
    if params.get('field') == 'refused':
        return {'status': 'error', 'code': 415, 'message': 'this field takes pdf'}
    return {'name': upload.filename, 'bytes': len(b''.join(upload.chunks)),
            'fields': params, 'user': (BaseApp.get_context() or {}).get('username')}


@pytest.fixture
def stub(tmp_path):
    (tmp_path / 'covers').mkdir()
    (tmp_path / 'covers' / 'my cover.png').write_bytes(PNG)
    (tmp_path / 'covers' / 'page.html').write_bytes(b'<script>alert(1)</script>')
    media = Media({'roots': {'covers': {'path': str(tmp_path / 'covers')}}})
    cp = CommandProcessor()
    cp.endpoints = {**_ENDPOINTS, 'media_upload': fake_upload}
    yield SimpleNamespace(cp=cp, media=media)
    BaseApp.set_context(None)


def bearer():
    now = datetime.now(timezone.utc)
    payload = {'id': 1, 'username': 'tester', 'exp': now + timedelta(hours=1),
               'last_refresh': now.timestamp()}
    return {'Authorization': f"Bearer {jwt.encode(payload, SECRET, algorithm='HS256')}"}


class Client:
    """One interface over the two test clients."""

    def __init__(self, framework, app):
        self.framework = framework
        if framework == 'flask':
            from flask import Flask
            web = Flask(__name__)
            srv.register_flask(web, app, SimpleNamespace(config=CONFIG), SECRET)
            self.c = web.test_client()
        else:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            web = FastAPI()
            srv.register_fastapi(web, app, SimpleNamespace(config=CONFIG), SECRET)
            self.c = TestClient(web)

    def get(self, url, headers=None):
        r = self.c.get(url, headers=headers or {})
        return r.status_code, r.headers, (r.data if self.framework == 'flask' else r.content)

    def upload(self, fields, filename=None, data=b'', headers=None):
        if self.framework == 'flask':
            import io
            form = dict(fields)
            if filename:
                form['file'] = (io.BytesIO(data), filename)
            r = self.c.post('/kitebase/upload', data=form, headers=headers or {},
                            content_type='multipart/form-data')
            return r.status_code, r.get_json()
        files = {'file': (filename, data)} if filename else None
        r = self.c.post('/kitebase/upload', data=fields, files=files, headers=headers or {})
        return r.status_code, r.json()


@pytest.fixture(params=['flask', 'fastapi'])
def client(request, stub):
    return Client(request.param, stub)


# ── GET ───────────────────────────────────────────────────────────────────────

def test_a_signed_url_needs_no_login(client, stub):
    status, headers, body = client.get('/kitebase/' + stub.media.url('covers:my cover.png'))
    assert status == 200 and body == PNG
    assert headers['Content-Type'] == 'image/png'
    assert headers['Content-Disposition'].startswith('inline;')
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['Content-Length'] == str(len(PNG))


def test_the_browser_cache_gets_a_304(client, stub):
    url = '/kitebase/' + stub.media.url('covers:my cover.png')
    _, headers, _ = client.get(url)
    status, _, body = client.get(url, headers={'If-None-Match': headers['ETag']})
    assert status == 304 and body == b''


def test_a_page_leaves_as_an_attachment(client, stub):
    _, headers, _ = client.get('/kitebase/' + stub.media.url('covers:page.html'))
    assert headers['Content-Disposition'].startswith('attachment;')
    assert 'sandbox' in headers['Content-Security-Policy']


def test_an_unsigned_or_altered_url_is_refused(client, stub):
    url = '/kitebase/' + stub.media.url('covers:my cover.png')
    assert client.get(url.split('?')[0])[0] == 403
    assert client.get(url.replace('my%20cover.png', 'page.html'))[0] == 403
    assert client.get('/kitebase/media/nowhere/a.png?e=1&s=x')[0] == 404


# ── upload ────────────────────────────────────────────────────────────────────

def test_an_upload_reaches_media_upload_with_its_user(client):
    status, body = client.upload({'table': 'Book', 'field': 'cover', 'id': '3'},
                                 'cover.png', PNG, headers=bearer())
    assert status == 200
    assert body['data'] == {'name': 'cover.png', 'bytes': len(PNG), 'user': 'tester',
                            'fields': {'table': 'Book', 'field': 'cover', 'id': '3'}}


def test_an_upload_needs_a_login(client):
    assert client.upload({'table': 'Book', 'field': 'cover'}, 'cover.png', PNG)[0] == 401


def test_an_upload_without_a_file_is_400(client):
    status, body = client.upload({'table': 'Book', 'field': 'cover'}, headers=bearer())
    assert status == 400 and 'file' in body['message']


def test_an_upload_keeps_the_status_of_the_refusal(client):
    status, body = client.upload({'table': 'Book', 'field': 'refused'}, 'a.png', PNG,
                                 headers=bearer())
    assert status == 415 and body['message'] == 'this field takes pdf'
