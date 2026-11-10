"""
The kitebase routes on Flask: an application or a blueprint.

See kitebase.http_common for what the two adapters have in common.
"""

from typing import Any, Callable, Dict, Optional

import kitebase.context
from kitebase.endpoints import FileResult
from kitebase.http_common import (
    _prefixes, file_headers, get_app_info, handle_download, handle_generic_endpoint,
    handle_media_get, handle_upload, handle_upload_op, media_chunks, report_checks,
)
from kitebase.tokens import HOST_TOKEN_NOT_JSON, AuthMiddleware, handle_host_token


def register_flask(target, kitebase_app, plugins, secret_key: str, *,
                   prefix: Optional[str] = None,
                   endpoint_prefix: Optional[str] = None,
                   auth: Optional['AuthMiddleware'] = None,
                   host_session: Optional[Callable[[Any], Optional[Dict[str, Any]]]] = None
                   ) -> 'AuthMiddleware':
    """
    Register kitebase's routes on a Flask application or Blueprint.

    Args:
        target:          a Flask app or a Blueprint — anything with .route()
                         and .after_request()
        kitebase_app:     the kitebase application (BaseApp)
        plugins:         the PluginsManager
        secret_key:      key the JWT is signed with
        prefix:          path the routes hang from, config.yaml's `api.prefix`
                         by default. Pass '' when the Blueprint carries its own
                         url_prefix and the host decides where it is mounted.
        endpoint_prefix: dispatcher segment, `api.endpoint_prefix` by default
        auth:            an AuthMiddleware to share with the rest of the
                         process — a server-rendered page that logs a person in
                         through the same identity passes the one it has
        host_session:    `host_session(request) -> context | None`, how a host
                         that logs people in itself (`client.login`) says who is
                         in. With it, `POST {prefix}/auth/token` signs that
                         context into a JWT; the dispatcher stays Bearer only.

    Returns:
        The AuthMiddleware in use, so the caller can authenticate by other
        doors without building a second one.
    """
    import json
    from functools import wraps
    from flask import Response, g, request

    from kitebase.i18n import set_locale
    from kitebase.querybuilder import JSONEncoder

    config = plugins.config
    prefix, endpoint_prefix = _prefixes(config, prefix, endpoint_prefix)
    auth = auth or AuthMiddleware(config, secret_key)
    if getattr(kitebase_app, 'media', None) is not None:
        kitebase_app.media.set_signing_key(secret_key)
    report_checks(kitebase_app)
    command_processor = kitebase_app.cp

    def reply(result: Dict[str, Any], status_code: Optional[int] = None) -> Any:
        """Send a handler result with the status code it carries.

        Deliberately not `jsonify`: that goes through the application's JSON
        provider — the host's, in a mounted deployment — and inherits both its
        date format and its key sorting, which raises on the mixed-type keys
        some descriptors carry.
        """
        if status_code is None:
            status_code = result.get('status_code', result.get('code', 200))
        return Response(json.dumps(result, cls=JSONEncoder, sort_keys=False),
                        status=status_code, mimetype='application/json')

    @target.teardown_request
    def kitebase_clear_context(exception=None):
        """Leave the thread as it was found.

        Every dispatch sets the context of the user it serves, so within
        kitebase's own surface a leftover is overwritten. It is a guest that
        pays for it: the worker goes back to the pool carrying an identity, and
        whatever the host serves next on that thread — a page, another API —
        inherits a user nobody chose, with the query behaviors filtering
        accordingly.
        """
        kitebase.context.replace(None)

    @target.after_request
    def kitebase_token_refresh(response):
        """Hand a refreshed token back in X-New-Token whenever one was issued."""
        if hasattr(g, 'kitebase_new_token'):
            response.headers['X-New-Token'] = g.kitebase_new_token
        return response

    def authenticated(view):
        """Validate the bearer token, refresh it when due, set the locale."""

        @wraps(view)
        def wrapper(*args, **kwargs):
            token, error = auth.extract_token(request.headers.get('Authorization'))
            if error:
                return reply({'status': 'error', 'message': error}, 401)

            payload, new_token, error = auth.decode_and_refresh(token)
            if error:
                return reply({'status': 'error', 'message': error}, 401)
            if new_token:
                g.kitebase_new_token = new_token

            g.user_context = payload
            set_locale(payload.get('locale') or config.get('locale', 'en'))
            return view(*args, **kwargs)

        return wrapper

    @target.route(f'{prefix}/info', methods=['GET'])
    def kitebase_info():
        return reply(get_app_info(config, prefix))

    @target.route(f'{prefix}/auth/login', methods=['POST'])
    def kitebase_login():
        try:
            return reply(auth.login(command_processor, request.json))
        except Exception as e:
            return reply({'status': 'error', 'message': str(e)}, 500)

    @target.route(f'{prefix}/auth/update_context', methods=['POST'])
    @authenticated
    def kitebase_update_context():
        return reply(auth.update_context(g.user_context, request.json))

    if host_session is not None:
        @target.route(f'{prefix}/auth/token', methods=['POST'])
        def kitebase_host_token():
            """A JWT for the user of the host's session (see handle_host_token)."""
            if not request.is_json:
                return reply(HOST_TOKEN_NOT_JSON, 415)
            return reply(handle_host_token(host_session, request, auth))

    @target.route(f'{prefix}/{endpoint_prefix}/<operation>', methods=['POST'])
    @authenticated
    def kitebase_dispatch(operation: str):
        """Everything that is not authentication: db, query, get_page, get_menu…"""
        return reply(handle_generic_endpoint(
            command_processor, operation, request.json, context=g.user_context))

    @target.route(f'{prefix}/download/<operation>', methods=['POST'])
    @authenticated
    def kitebase_download(operation: str):
        """An operation whose answer is a file (FileResult): the bytes, not JSON."""
        result = handle_download(command_processor, operation, request.json,
                                 context=g.user_context)
        if not isinstance(result, FileResult):
            return reply(result)
        return Response(result.content, mimetype=result.mime, headers=file_headers(result))

    @target.route(f'{prefix}/upload', methods=['POST'])
    @authenticated
    def kitebase_upload():
        """A media file, multipart: table, field, id and the file (handle_upload)."""
        from kitebase.media import UploadedFile

        part = request.files.get('file')
        upload = None
        if part is not None:
            upload = UploadedFile(part.filename or '', iter(lambda: part.stream.read(64 * 1024), b''))
        return reply(handle_upload(command_processor, request.form.to_dict(), upload,
                                   context=g.user_context))

    @target.route(f'{prefix}/upload/<operation>', methods=['POST'])
    @authenticated
    def kitebase_upload_op(operation: str):
        """An operation that takes a file, multipart: params and the file."""
        from kitebase.media import UploadedFile

        part = request.files.get('file')
        upload = None
        if part is not None:
            upload = UploadedFile(part.filename or '', iter(lambda: part.stream.read(64 * 1024), b''))
        return reply(handle_upload_op(command_processor, operation, request.form.get('params'),
                                      upload, context=g.user_context))

    @target.route(f'{prefix}/media/<root>/<path:path>', methods=['GET'])
    def kitebase_media(root: str, path: str):
        """A file by its signed URL: no login, the signature is the permission."""
        answer = handle_media_get(kitebase_app, root, path, request.args.to_dict(),
                                  request.headers.get('If-None-Match'))
        if answer.path is None:
            if answer.status == 304:
                return Response(status=304, headers=answer.headers)
            return reply({'status': 'error', 'message': answer.message}, answer.status)
        headers = {**answer.headers, 'Content-Length': str(answer.path.stat().st_size)}
        return Response(media_chunks(answer.path), status=200, headers=headers,
                        mimetype=answer.headers['Content-Type'], direct_passthrough=True)

    return auth


# ── The compiled client ──────────────────────────────────────────────────────
#
# Where the client is mounted is the application's `client:` section, read by
# kitebase.clientui: "/" when kitebase is the application, "/admin/" when it is
# the admin of a host. The directory is always `<app>/clientui/`, so `static/`
# stays the application's own, with the route its framework gives it.
#
# The client is a single-page application: a path that is not a file is one of
# its pages, and gets index.html so that a reload or a bookmark lands where it
# was. Both adapters do exactly that and nothing else — and touch nothing
# outside the base, like the API adapters above.

def serve_client_flask(target, app_dir, config: Dict[str, Any]) -> bool:
    """
    Serve `<app_dir>/clientui/` on a Flask application or Blueprint.

    Returns False, registering nothing, when the client is not built: what to
    answer then is the application's choice.
    """
    from pathlib import Path
    from flask import redirect, send_from_directory
    from kitebase.clientui import CLIENT_DIR, client_settings

    directory = Path(app_dir) / CLIENT_DIR
    if not directory.is_dir():
        return False
    base = client_settings(config).base

    def client(path=''):
        if path and (directory / path).is_file():
            return send_from_directory(directory, path)
        return send_from_directory(directory, 'index.html')

    target.add_url_rule(f'{base}/', 'kitebase_client', client)
    target.add_url_rule(f'{base}/<path:path>', 'kitebase_client_path', client)
    if base:
        target.add_url_rule(base, 'kitebase_client_base', lambda: redirect(f'{base}/'))
    return True
