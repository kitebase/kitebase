"""
What the HTTP adapters share, whatever the framework.

Every handler returns a plain dict:
- 'status': 'success' | 'error'
- 'data': response payload (on success)
- 'message': error message (on error)
- 'status_code': HTTP status code

The adapter turns it into its framework's response and does nothing else.
"""

import json
import traceback as _traceback
from typing import Any, Dict, Optional, Tuple, Union

from kitebase.endpoints import FileResult


def _error_response(message: str, status_code: int = 500,
                    error_type: Optional[str] = None,
                    traceback: Optional[str] = None,
                    request_id: Optional[str] = None,
                    data: Any = None,
                    flow: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a uniform error response dict."""
    r: Dict[str, Any] = {'status': 'error', 'message': message, 'status_code': status_code}
    # What a refusal has to say beyond its message - the field errors of a form.
    if data is not None:
        r['data'] = data
    if error_type:
        r['error_type'] = error_type
    if traceback:
        r['traceback'] = traceback
    # The line in the log that tells the whole story: what a person reports.
    if request_id:
        r['request_id'] = request_id
    # A question of a rule travels as a refusal: a caller that does not know
    # the protocol never takes it for a save (kitebase.flow).
    if flow:
        r['$flow'] = flow
    return r


def _error_from_exc(e: Exception, status_code: int = 500) -> Dict[str, Any]:
    """Build error response from a live exception (captures current traceback)."""
    return _error_response(
        message=str(e),
        status_code=status_code,
        error_type=type(e).__name__,
        traceback=_traceback.format_exc()
    )


def _error_from_result(result: Dict[str, Any],
                       default_message: str = 'Operation failed') -> Dict[str, Any]:
    """Build error response propagating traceback from a CommandResult dict.

    The HTTP status is the one the operation gave: 403 for a refused
    permission, 404 for an unknown operation, 500 for an endpoint that raised,
    an endpoint's own 4xx. A refusal that names no error status is a 400.
    """
    code = result.get('code')
    return _error_response(
        message=result.get('message', default_message),
        status_code=code if isinstance(code, int) and code >= 400 else 400,
        error_type=result.get('error_type'),
        traceback=result.get('traceback'),
        request_id=result.get('request_id'),
        data=result.get('data'),
        flow=result.get('$flow'),
    )


def handle_generic_endpoint(
    command_processor,
    operation: str,
    data: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Framework-agnostic generic endpoint handler.

    Args:
        command_processor: Kitebase command processor
        operation: Endpoint operation name
        data: Operation parameters
        context: User context

    Returns:
        Dict with status, data, and status_code
    """
    try:
        command = {
            "operation": operation,
            "parameters": data
        }

        if context:
            command["context"] = context

        result = command_processor.send(command)

        if result.get('status') == 'success':
            if isinstance(result.get('data'), FileResult):
                return _error_response(
                    f"Operation '{operation}' returns a file: call it through the download route",
                    status_code=400, request_id=result.get('request_id'))
            response = {'status': 'success', 'data': result.get('data'), 'status_code': 200}
            # What the endpoint had to say about what it did: an operation called
            # from a button reports in one line, and the envelope carries it.
            if result.get('message'):
                response['message'] = result['message']
            if result.get('$flow'):
                response['$flow'] = result['$flow']
            return response
        else:
            return _error_from_result(result, 'Operation failed')

    except Exception as e:
        return _error_from_exc(e)


def handle_download(
    command_processor,
    operation: str,
    data: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> Union[FileResult, Dict[str, Any]]:
    """
    Framework-agnostic handler of the download route.

    Same command processor as the dispatcher, so an operation is the same
    endpoint with the same context; only the answer differs. Returns the
    FileResult to send as bytes, or an error dict to send as JSON - which
    includes an operation that answered with data instead of a file.
    """
    try:
        command = {"operation": operation, "parameters": data}
        if context:
            command["context"] = context

        result = command_processor.send(command)

        if result.get('status') != 'success':
            return _error_from_result(result, 'Operation failed')
        if not isinstance(result.get('data'), FileResult):
            return _error_response(
                f"Operation '{operation}' does not return a file",
                status_code=400, request_id=result.get('request_id'))
        return result['data']

    except Exception as e:
        return _error_from_exc(e)


def handle_upload(command_processor, fields: Dict[str, Any], upload: Any,
                  context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Framework-agnostic handler of the upload route.

    The form fields and the file go to `media_upload` through the command
    processor, so the upload runs with the same context, permissions and log
    line as any operation, and the same statuses: 403, 404, 413, 415.
    """
    from kitebase.media import UploadedFile

    if not isinstance(upload, UploadedFile):
        return _error_response("The upload has no 'file' part", status_code=400)
    try:
        command = {"operation": "media_upload", "parameters": {**fields, 'file': upload}}
        if context:
            command["context"] = context
        result = command_processor.send(command)
        if result.get('status') == 'success':
            return {'status': 'success', 'data': result.get('data'), 'status_code': 200}
        return _error_from_result(result, 'Upload failed')
    except Exception as e:
        return _error_from_exc(e)


def handle_upload_op(command_processor, operation: str, params: Optional[str], upload: Any,
                     context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Framework-agnostic handler of `upload/<operation>`: an operation that takes
    a file, the twin of the download route. The form carries `params`, the
    operation's parameters as JSON, and the `file`, which the operation finds
    in `data['file']` (an UploadedFile) and reads itself, within its own limit.
    The answer is the dispatcher's, questions and all.
    """
    from kitebase.media import UploadedFile

    if not isinstance(upload, UploadedFile):
        return _error_response("The upload has no 'file' part", status_code=400)
    try:
        data = json.loads(params) if params else {}
    except ValueError:
        return _error_response("'params' is not JSON", status_code=400)
    if not isinstance(data, dict):
        return _error_response("'params' must be an object", status_code=400)
    return handle_generic_endpoint(command_processor, operation, {**data, 'file': upload},
                                   context=context)


def handle_media_get(kitebase_app, root: str, path: str, params: Dict[str, str],
                     if_none_match: Optional[str] = None):
    """Framework-agnostic handler of `GET media/<root>/<path>`: a MediaReply."""
    from kitebase.media import MediaReply, serve

    media = getattr(kitebase_app, 'media', None)
    if media is None or root not in media.roots:
        return MediaReply(404, {}, message='No such file')
    return serve(media, root, path, params, if_none_match)


def media_chunks(path, size: int = 64 * 1024):
    """The bytes of a served file, a chunk at a time."""
    with open(path, 'rb') as f:
        while chunk := f.read(size):
            yield chunk


def file_headers(file: FileResult) -> Dict[str, str]:
    """
    The headers a file leaves with, the same for both frameworks.

    The name goes twice: `filename*` in UTF-8 for the browsers (all of them
    today), `filename` in ASCII for whatever does not read the first. An
    export is the user's data of the moment, never something to keep in a
    cache.
    """
    from urllib.parse import quote

    ascii_name = file.filename.encode('ascii', 'replace').decode('ascii')
    ascii_name = ascii_name.replace('?', '_').replace('"', '_').replace('\\', '_')
    return {
        'Content-Disposition': (f'{file.disposition}; filename="{ascii_name}"; '
                                f"filename*=UTF-8''{quote(file.filename, safe='')}"),
        'Cache-Control': 'no-store',
    }


# ============================================
# App Info Handler
# ============================================

def get_app_info(plugins_config: Dict[str, Any], api_prefix: str) -> Dict[str, Any]:
    """
    Framework-agnostic app info handler.

    Args:
        plugins_config: Plugins configuration dict
        api_prefix: API prefix (e.g., '/kitebase' or '/api/v1')

    Returns:
        Dict with app information. `client` is the `client:` section, read by
        the client at startup: who logs people in follows config.yaml without
        rebuilding it. It also carries what the login page shows before anyone
        is signed in: the app's `title` and `client.signin` (subtitle, hint),
        written to be public, and nothing else of the configuration.
    """
    from kitebase.clientui import client_settings, signin_texts

    return {
        'status': 'success',
        'data': {
            'application': plugins_config.get('name', 'Unknown'),
            'version': plugins_config.get('version', '0.0.0'),
            'description': plugins_config.get('description', ''),
            'client': {**client_settings(plugins_config)._asdict(),
                       'title': plugins_config.get('title'),
                       'signin': signin_texts(plugins_config)},
            'kitebase_api_prefix': api_prefix,
            'available_endpoints': {
                'home': '/',
                'app_info': '/info',
                'kitebase_auth': f'{api_prefix}/auth/login',
                'kitebase_auth_update': f'{api_prefix}/auth/update_context',
                'kitebase_database': f'{api_prefix}/db/<table>',
                'kitebase_query': f'{api_prefix}/query',
                'kitebase_commands': f'{api_prefix}/endpoint/<operation>'
            }
        },
        'status_code': 200
    }


# ============================================
# Route registration
# ============================================
#
# The canonical routes — login, context refresh, the dispatcher that carries
# every other operation, and info — registered on whatever the caller hands
# over. `app.route` and `Blueprint.route` (and `after_request`) share a
# signature, so one function serves both cases and the caller's choice of
# target is what decides the scope:
#
#     srv.register_flask(app, kitebase_app, plugins, SECRET_KEY)
#
#         kitebase owns the process: the routes and the after_request hook are
#         the application's, which is what a standalone server wants.
#
#     bp = Blueprint('kitebase', __name__)
#     srv.register_flask(bp, kitebase_app, plugins, SECRET_KEY)
#     host_app.register_blueprint(bp)
#
#         kitebase is a guest: everything registered here lives inside the
#         blueprint, and the host's own routes and hooks are untouched.
#
# Neither form touches anything outside its target — no CORS, no static
# catch-all, no route at the root. Nor does either *depend* on anything
# outside it: both adapters serialize their own responses, with the ISO 8601
# dates their endpoints accept back on write, and both return a refusal rather
# than raising it. A host is free to keep its own JSON provider, its own
# default response class and its own exception handlers, and kitebase answers
# the same either way.

def _prefixes(plugins_config: Dict[str, Any],
              prefix: Optional[str],
              endpoint_prefix: Optional[str]) -> Tuple[str, str]:
    """Resolve the API prefixes, falling back to config.yaml."""
    api = plugins_config.get('api', {})
    if prefix is None:
        prefix = '/' + api.get('prefix', 'kitebase').strip('/')
    if endpoint_prefix is None:
        endpoint_prefix = api.get('endpoint_prefix', 'endpoint').strip('/')
    return prefix.rstrip('/'), endpoint_prefix.strip('/')


def report_checks(kitebase_app) -> None:
    """The warnings and errors of `kitebase check`, in the log, at startup.

    A typo in a YAML key (`nulable`) is dropped without a word by whatever was
    meant to read it; `check` names it, but only for whoever runs it. Here every
    start runs it too: in development the line is in the terminal of `kitebase
    dev` as soon as the server restarts, in service it is in the journal. It
    never stops the start: a warning is a question, not a refusal. The info
    lines (a title a plugin overrides) stay in `check`, where they are asked
    for. It costs a few milliseconds.
    """
    import logging
    from kitebase.diagnostics import run_checks

    log = logging.getLogger('kitebase')
    try:
        issues = [i for i in run_checks(kitebase_app) if i['severity'] in ('error', 'warning')]
    except Exception:
        log.exception('the startup checks failed; `kitebase check` tells more')
        return
    for i in issues:
        log.warning('check %s %s %s: %s', i['severity'], i['code'], i['path'], i['message'])
    if issues:
        log.warning('check: %d to look at, `kitebase check` for the details', len(issues))
