"""
The kitebase routes on FastAPI: an application or a router.

See kitebase.http_common for what the two adapters have in common.
"""

from typing import Any, Callable, Dict, Optional, Tuple

from kitebase.endpoints import FileResult
from kitebase.http_common import (
    _prefixes, file_headers, get_app_info, handle_download, handle_generic_endpoint,
    handle_media_get, handle_upload, handle_upload_op, media_chunks, report_checks,
)
from kitebase.tokens import HOST_TOKEN_NOT_JSON, AuthMiddleware, handle_host_token


def register_fastapi(target, kitebase_app, plugins, secret_key: str, *,
                     prefix: Optional[str] = None,
                     endpoint_prefix: Optional[str] = None,
                     auth: Optional['AuthMiddleware'] = None,
                     host_session: Optional[Callable[[Any], Optional[Dict[str, Any]]]] = None
                     ) -> 'AuthMiddleware':
    """
    Register kitebase's routes on a FastAPI application or APIRouter.

    Same arguments and same routes as `register_flask`, answering byte for
    byte the same — which is the property the pair exists to hold.

    Nothing here is application-wide, and that is what lets an APIRouter be a
    valid target: the refreshed token rides on the response the handler builds
    rather than on a middleware, and the responses are serialized here rather
    than through the application's encoder. A refusal is *returned* and not
    raised for the same reason — an HTTPException would go through the host's
    exception handlers and come back in the host's error shape.

    Unlike Flask, nothing clears the context on the way out: a sync handler
    runs on a copy of the request's context, and what it sets goes with it.
    """
    import json

    from fastapi import Request, Response

    from kitebase.i18n import set_locale
    from kitebase.querybuilder import JSONEncoder

    config = plugins.config
    prefix, endpoint_prefix = _prefixes(config, prefix, endpoint_prefix)
    auth = auth or AuthMiddleware(config, secret_key)
    if getattr(kitebase_app, 'media', None) is not None:
        kitebase_app.media.set_signing_key(secret_key)
    report_checks(kitebase_app)
    command_processor = kitebase_app.cp

    def reply(result: Dict[str, Any], status_code: Optional[int] = None,
              new_token: Optional[str] = None) -> Response:
        """Send a handler result with the status code it carries.

        A plain Response and not a returned dict: FastAPI would serialize the
        dict with the application's default response class and encoder — the
        host's, in a mounted deployment. kitebase's ISO 8601 dates are the
        format its own endpoints accept back on write, so they travel with the
        route rather than depending on the process they are in.
        """
        if status_code is None:
            status_code = result.get('status_code', result.get('code', 200))
        return Response(
            content=json.dumps(result, cls=JSONEncoder, sort_keys=False),
            status_code=status_code,
            media_type='application/json',
            headers={'X-New-Token': new_token} if new_token else None,
        )

    def identify(request: Request) -> Tuple[Optional[Dict[str, Any]],
                                            Optional[Response], Optional[str]]:
        """Validate the bearer token, refresh it when due, set the locale.

        Returns (context, refusal, new_token): exactly one of the first two is
        set. The caller carries the token to the response it builds, because a
        Response returned by a handler replaces the injected one, headers and
        all — writing it anywhere else would lose it in silence.
        """
        token, error = auth.extract_token(request.headers.get('authorization'))
        if error:
            return None, reply({'status': 'error', 'message': error}, 401), None

        payload, new_token, error = auth.decode_and_refresh(token)
        if error:
            return None, reply({'status': 'error', 'message': error}, 401), None

        set_locale(payload.get('locale') or config.get('locale', 'en'))
        return payload, None, new_token

    @target.get(f'{prefix}/info')
    def kitebase_info():
        return reply(get_app_info(config, prefix))

    @target.post(f'{prefix}/auth/login')
    def kitebase_login(data: dict):
        try:
            return reply(auth.login(command_processor, data))
        except Exception as e:
            return reply({'status': 'error', 'message': str(e)}, 500)

    @target.post(f'{prefix}/auth/update_context')
    def kitebase_update_context(data: dict, request: Request):
        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        return reply(auth.update_context(user, data), new_token=new_token)

    if host_session is not None:
        @target.post(f'{prefix}/auth/token')
        def kitebase_host_token(request: Request):
            """A JWT for the user of the host's session (see handle_host_token)."""
            content_type = request.headers.get('content-type', '')
            if not content_type.startswith('application/json'):
                return reply(HOST_TOKEN_NOT_JSON, 415)
            return reply(handle_host_token(host_session, request, auth))

    @target.post(f'{prefix}/{endpoint_prefix}/{{operation}}')
    def kitebase_dispatch(operation: str, data: dict, request: Request):
        """Everything that is not authentication: db, query, get_page, get_menu…"""
        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        return reply(
            handle_generic_endpoint(command_processor, operation, data, context=user),
            new_token=new_token)

    @target.post(f'{prefix}/download/{{operation}}')
    def kitebase_download(operation: str, data: dict, request: Request):
        """An operation whose answer is a file (FileResult): the bytes, not JSON."""
        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        result = handle_download(command_processor, operation, data, context=user)
        if not isinstance(result, FileResult):
            return reply(result, new_token=new_token)
        headers = file_headers(result)
        if new_token:
            headers['X-New-Token'] = new_token
        return Response(content=result.content, media_type=result.mime, headers=headers)

    @target.post(f'{prefix}/upload')
    async def kitebase_upload(request: Request):
        """A media file, multipart: table, field, id and the file (handle_upload).

        Async because reading a form is; the operation itself runs in the
        thread pool, like the sync routes, so a write never holds the loop.
        """
        from starlette.concurrency import run_in_threadpool
        from starlette.datastructures import UploadFile
        from kitebase.media import UploadedFile

        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        form = await request.form()
        part = form.get('file')
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        upload = None
        if isinstance(part, UploadFile):
            upload = UploadedFile(part.filename or '', iter(lambda: part.file.read(64 * 1024), b''))
        try:
            result = await run_in_threadpool(handle_upload, command_processor, fields, upload, user)
        finally:
            await form.close()
        return reply(result, new_token=new_token)

    @target.post(f'{prefix}/upload/{{operation}}')
    async def kitebase_upload_op(operation: str, request: Request):
        """An operation that takes a file, multipart: params and the file."""
        from starlette.concurrency import run_in_threadpool
        from starlette.datastructures import UploadFile
        from kitebase.media import UploadedFile

        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        form = await request.form()
        part = form.get('file')
        params = form.get('params')
        upload = None
        if isinstance(part, UploadFile):
            upload = UploadedFile(part.filename or '', iter(lambda: part.file.read(64 * 1024), b''))
        try:
            result = await run_in_threadpool(
                handle_upload_op, command_processor, operation,
                params if isinstance(params, str) else None, upload, user)
        finally:
            await form.close()
        return reply(result, new_token=new_token)

    @target.get(f'{prefix}/media/{{root}}/{{path:path}}')
    def kitebase_media(root: str, path: str, request: Request):
        """A file by its signed URL: no login, the signature is the permission."""
        from fastapi.responses import StreamingResponse

        answer = handle_media_get(kitebase_app, root, path, dict(request.query_params),
                                  request.headers.get('if-none-match'))
        if answer.path is None:
            if answer.status == 304:
                return Response(status_code=304, headers=answer.headers)
            return reply({'status': 'error', 'message': answer.message}, answer.status)
        headers = {**answer.headers, 'Content-Length': str(answer.path.stat().st_size)}
        return StreamingResponse(media_chunks(answer.path), headers=headers,
                                 media_type=answer.headers['Content-Type'])

    return auth


def serve_client_fastapi(target, app_dir, config: Dict[str, Any]) -> bool:
    """
    Mount `<app_dir>/clientui/` on a FastAPI application.

    Call it after the API routes: a client mounted at "/" would otherwise
    answer before them. Returns False, mounting nothing, when the client is not
    built.
    """
    from pathlib import Path
    from starlette.exceptions import HTTPException
    from starlette.staticfiles import StaticFiles
    from kitebase.clientui import CLIENT_DIR, client_settings

    directory = Path(app_dir) / CLIENT_DIR
    if not directory.is_dir():
        return False
    base = client_settings(config).base

    class SinglePageApp(StaticFiles):
        """StaticFiles answers 404 for a page of the client: give it index.html."""

        async def get_response(self, path, scope):
            try:
                response = await super().get_response(path, scope)
            except HTTPException as e:
                if e.status_code != 404:
                    raise
                return await super().get_response('index.html', scope)
            if response.status_code == 404:
                return await super().get_response('index.html', scope)
            return response

    target.mount(base or '/', SinglePageApp(directory=str(directory), html=True),
                 name='kitebase_client')
    return True
