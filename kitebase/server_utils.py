"""
Framework-agnostic server utilities for Kitebase.

All handlers return plain dict with:
- 'status': 'success' | 'error'
- 'data': response payload (on success)
- 'message': error message (on error)
- 'status_code': HTTP status code

This allows the same logic to be used with Flask, FastAPI, Django, or any other framework.
"""

from datetime import datetime, timezone, timedelta
import logging
import logging.handlers
import sys
import traceback as _traceback
import jwt
from typing import Any, Callable, Dict, Optional, Tuple


def _error_response(message: str, status_code: int = 500,
                    error_type: Optional[str] = None,
                    traceback: Optional[str] = None,
                    request_id: Optional[str] = None,
                    data: Any = None) -> Dict[str, Any]:
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
    return r


def _error_from_exc(e: Exception, status_code: int = 500) -> Dict[str, Any]:
    """Build error response from a live exception (captures current traceback)."""
    return _error_response(
        message=str(e),
        status_code=status_code,
        error_type=type(e).__name__,
        traceback=_traceback.format_exc()
    )


def _error_from_result(result: Dict[str, Any], default_message: str = 'Operation failed',
                       status_code: int = 400) -> Dict[str, Any]:
    """Build error response propagating traceback from a CommandResult dict."""
    return _error_response(
        message=result.get('message', default_message),
        status_code=status_code,
        error_type=result.get('error_type'),
        traceback=result.get('traceback'),
        request_id=result.get('request_id'),
        data=result.get('data'),
    )


# ============================================
# Logging
# ============================================

def setup_logging(level: str = 'INFO', file: Optional[str] = None, *,
                  max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> logging.Logger:
    """
    Make the process's log audible. The library only speaks (`kitebase` logger,
    one line per request, the traceback on a failure); this is where an
    application decides to listen, once at startup, with values from its
    environment. Not calling it is a choice too: an application that embeds
    kitebase in a process of its own keeps its own logging.

    Always stdout — under systemd that is journald, at a console it is the
    terminal, so running by hand and running as a service read the same. The
    line is short because journald adds the time itself. `file` adds a rotating
    file with timestamps, for the case "copy it and read it elsewhere".

    Configures the root logger so the application's own loggers flow the same
    way. Calling it again replaces what it installed, never doubles it.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, '_kitebase', False):
            root.removeHandler(handler)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
    handlers = [stdout]
    if file:
        rotating = logging.handlers.RotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
        rotating.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(name)s: %(message)s'))
        handlers.append(rotating)
    for handler in handlers:
        handler._kitebase = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # Third parties whisper: what they say at INFO is their own bookkeeping
    # (alembic's plugin setup, every SQL statement with echo), and it would
    # bury the one line per request this exists for. WARNING still passes.
    for noisy in ('alembic', 'sqlalchemy'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger('kitebase')


# ============================================
# JWT Token Management
# ============================================

def decode_and_check_refresh(
    token: str,
    secret_key: str,
    jwt_expiration_hours: int = 24,
    refresh_interval_minutes: int = 20
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """
    Decode JWT token and check if it needs refresh.

    Framework-agnostic function that handles token decoding and automatic
    refresh based on last_refresh timestamp.

    Args:
        token: JWT token string
        secret_key: Secret key for JWT decoding
        jwt_expiration_hours: Token lifetime in hours
        refresh_interval_minutes: Refresh after this inactivity

    Returns:
        Tuple of (payload, new_token, error):
        - payload: Decoded token payload (or None if error)
        - new_token: New refreshed token (or None if not needed)
        - error: Error message (or None if success)

    Example:
        >>> payload, new_token, error = decode_and_check_refresh(
        ...     token='eyJhbGc...',
        ...     secret_key='secret',
        ...     jwt_expiration_hours=24,
        ...     refresh_interval_minutes=20
        ... )
        >>> if error:
        ...     return {'status': 'error', 'message': error, 'status_code': 401}
        >>> if new_token:
        ...     # Include new_token in response
        ...     response['new_token'] = new_token
    """
    try:
        # Decode token
        payload = jwt.decode(token, secret_key, algorithms=['HS256'])

        # Check if refresh is needed
        last_refresh = payload.get('last_refresh', 0)
        now = datetime.now(timezone.utc).timestamp()
        refresh_interval_seconds = refresh_interval_minutes * 60

        new_token = None
        if now - last_refresh > refresh_interval_seconds:
            # Generate new token with extended expiration
            new_payload = {**payload}
            new_payload['exp'] = datetime.now(timezone.utc) + timedelta(hours=jwt_expiration_hours)
            new_payload['last_refresh'] = now
            new_payload.pop('iat', None)  # Remove old issued-at

            new_token = jwt.encode(new_payload, secret_key, algorithm='HS256')

        return payload, new_token, None

    except jwt.ExpiredSignatureError:
        return None, None, 'Token expired'
    except jwt.InvalidTokenError as e:
        return None, None, f'Invalid token: {str(e)}'
    except Exception as e:
        return None, None, f'Token decode error: {str(e)}'


def extract_bearer_token(authorization_header: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract JWT token from Authorization header.

    Framework-agnostic function to parse Bearer token.

    Args:
        authorization_header: Authorization header value (e.g., "Bearer eyJhbGc...")

    Returns:
        Tuple of (token, error):
        - token: Extracted token string (or None if error)
        - error: Error message (or None if success)

    Example:
        >>> token, error = extract_bearer_token(request.headers.get('Authorization'))
        >>> if error:
        ...     return {'status': 'error', 'message': error, 'status_code': 401}
    """
    if not authorization_header:
        return None, 'Missing authorization header'

    if not authorization_header.startswith('Bearer '):
        return None, 'Invalid authorization header format'

    token = authorization_header.split(' ')[1]
    return token, None


# ============================================
# Authentication Handlers
# ============================================

def issue_token(user_data: Dict[str, Any],
                secret_key: str,
                jwt_expiration_hours: int = 24,
                context_fields: Optional[list] = None) -> Dict[str, Any]:
    """
    Sign a user context into a JWT: the answer of a successful login.

    Only `username` and the configured `context_fields` go into the token.
    No op_date: absent means today, read per request by defaults.op_date(); a
    token outlives the day it was issued on, so it carries only a date the
    user chose (update_context).
    """
    now = datetime.now(timezone.utc)
    payload = {
        'username': user_data.get('username'),
        'exp': now + timedelta(hours=jwt_expiration_hours),
        'last_refresh': now.timestamp(),  # Track last refresh for auto-refresh
    }
    for field in context_fields or []:
        if field in user_data:
            payload[field] = user_data[field]

    return {
        'status': 'success',
        'data': {
            'token': jwt.encode(payload, secret_key, algorithm='HS256'),
            'user': user_data
        },
        'status_code': 200
    }


# auth/token is authenticated by the host's cookie, which a cross-site page can
# make the browser send. A form can only post form or text bodies; JSON needs a
# CORS preflight the browser will not pass, so requiring it is what keeps
# another origin from minting a token (and its answer is unreadable there).
HOST_TOKEN_NOT_JSON = {'status': 'error', 'message': 'auth/token takes a JSON body'}


def handle_host_token(host_session, request, auth: 'AuthMiddleware') -> Dict[str, Any]:
    """
    Answer `auth/token`: a JWT for the user the host's session says is in.

    The host logs people in and keeps its own session; kitebase only asks it,
    through `host_session(request) -> context | None`, who the user is, and
    signs that context as the login would. No session is a 401, which the
    client turns into a redirect to the host's login page.
    """
    try:
        context = host_session(request)
    except Exception as e:
        return _error_from_exc(e)
    if not context:
        return {'status': 'error', 'message': 'No host session',
                'status_code': 401}
    return auth.issue_token(context)

def handle_auth(
    command_processor,
    data: Dict[str, Any],
    secret_key: str,
    jwt_expiration_hours: int = 24,
    context_fields: Optional[list] = None
) -> Dict[str, Any]:
    """
    Framework-agnostic authentication handler.

    Args:
        command_processor: Kitebase command processor instance
        data: Request data with 'username' and 'password'
        secret_key: JWT secret key
        jwt_expiration_hours: Token expiration in hours
        context_fields: Fields to include in JWT payload

    Returns:
        Dict with status, token, user, and status_code
    """
    if not data or not data.get('username') or not data.get('password'):
        return {
            'status': 'error',
            'message': 'Username and password are required',
            'status_code': 400
        }

    try:
        command = {
            "operation": "auth",
            "parameters": {
                "username": data['username'],
                "password": data['password']
            }
        }

        result = command_processor.send(command)

        if result.get('status') == 'success':
            user_data = result.get('data', {}).get('context', {})
            return issue_token(user_data, secret_key, jwt_expiration_hours,
                               context_fields)
        else:
            return {
                'status': 'error',
                'message': result.get('message', 'Authentication failed'),
                'status_code': 401
            }

    except Exception as e:
        return _error_from_exc(e)


# Context fields the client may always set via update_context, regardless of
# app config. These are framework-owned, non-identity fields.
FRAMEWORK_UPDATABLE_FIELDS = frozenset({'op_date'})


def custom_context_fields(config: Dict[str, Any]) -> list:
    """
    App-defined context fields a client is allowed to set via update_context:
    configured `context_fields` that are NOT columns of the user table.

    Identity columns (id, email, is_active, is_admin, ...) come from the user
    record and must stay server-authoritative — a client must never be able to
    set them in its own token. Requires the kitebase app to be loaded.
    """
    import kitebase.utils
    app = kitebase.utils.get_app()
    auth = config.get('authentication', {})
    user_model = app.models.get(auth.get('user_table', 'User'))
    if user_model is None:
        return []
    user_cols = {c.key for c in user_model.__table__.columns}
    return [f for f in auth.get('context_fields', []) if f not in user_cols]


def handle_update_context(
    current_context: Dict[str, Any],
    updates: Dict[str, Any],
    secret_key: str,
    jwt_expiration_hours: int = 24,
    allowed_fields: Optional[list] = None
) -> Dict[str, Any]:
    """
    Framework-agnostic context update handler.

    Args:
        current_context: Current user context from JWT
        updates: Fields to update in context (filtered against the allowlist);
            a None value removes the field
        secret_key: JWT secret key
        jwt_expiration_hours: Token expiration in hours
        allowed_fields: App-defined fields the client may set. Framework fields
            (FRAMEWORK_UPDATABLE_FIELDS) are always allowed. Any other key in
            `updates` is dropped — this is the guard against a client escalating
            its own token (e.g. sending is_admin=True).

    Returns:
        Dict with new token and updated context
    """
    try:
        # Allowlist: only framework fields + app-declared custom fields may be
        # set by the client. Everything else (identity, JWT machinery) is dropped.
        allowed = set(FRAMEWORK_UPDATABLE_FIELDS)
        if allowed_fields:
            allowed |= set(allowed_fields)
        filtered = {k: v for k, v in (updates or {}).items() if k in allowed}

        # Merge the filtered updates into current context; None removes the
        # field (op_date: None = back to following today).
        new_context = {**current_context}
        for key, value in filtered.items():
            if value is None:
                new_context.pop(key, None)
            else:
                new_context[key] = value

        # Remove 'exp' and 'iat' if present
        new_context.pop('exp', None)
        new_context.pop('iat', None)

        # Add new expiration
        new_context['exp'] = datetime.now(timezone.utc) + timedelta(hours=jwt_expiration_hours)

        # Generate new token
        new_token = jwt.encode(new_context, secret_key, algorithm='HS256')

        return {
            'status': 'success',
            'data': {
                'token': new_token,
                'context': new_context
            },
            'status_code': 200
        }

    except Exception as e:
        return _error_from_exc(e)


def handle_db_operation(
    command_processor,
    operation: str,
    table: str,
    record_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Framework-agnostic database operation handler.

    Args:
        command_processor: Kitebase command processor
        operation: 'get', 'create', 'update', 'delete'
        table: Table name
        record_id: Record ID (for get, update, delete)
        data: Record data (for create, update)
        context: User context

    Returns:
        Dict with status, data, and status_code
    """
    try:
        command = {
            "operation": "db",
            "parameters": {
                "operation": operation,
                "table": table
            }
        }

        if record_id:
            command["parameters"]["id"] = record_id

        if data:
            command["parameters"]["data"] = data

        if context:
            command["context"] = context

        result = command_processor.send(command)

        if result.get('status') == 'success':
            return {'status': 'success', 'data': result.get('data'), 'status_code': 200}
        else:
            return _error_from_result(result, 'Operation failed')

    except Exception as e:
        return _error_from_exc(e)


def handle_query(
    command_processor,
    query_data: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Framework-agnostic query handler.

    Args:
        command_processor: Kitebase command processor
        query_data: Query definition (table, fields, filters, etc.)
        context: User context

    Returns:
        Dict with status, data, and status_code
    """
    try:
        command = {
            "operation": "query",
            "parameters": query_data
        }

        if context:
            command["context"] = context

        result = command_processor.send(command)

        if result.get('status') == 'success':
            return {'status': 'success', 'data': result.get('data'), 'status_code': 200}
        else:
            return _error_from_result(result, 'Query failed')

    except Exception as e:
        return _error_from_exc(e)


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
            response = {'status': 'success', 'data': result.get('data'), 'status_code': 200}
            # What the endpoint had to say about what it did: an operation called
            # from a button reports in one line, and the envelope carries it.
            if result.get('message'):
                response['message'] = result['message']
            return response
        else:
            return _error_from_result(result, 'Operation failed')

    except Exception as e:
        return _error_from_exc(e)


# ============================================
# Authentication Middleware (Optional Wrapper)
# ============================================

class AuthMiddleware:
    """
    Optional wrapper class for authentication logic.

    Provides a cleaner interface for servers to handle authentication
    with automatic token refresh.

    Example usage in FastAPI:
        >>> auth = AuthMiddleware(plugins.config, SECRET_KEY)
        >>>
        >>> async def get_current_user(request: Request):
        ...     token, error = auth.extract_token(request.headers.get('Authorization'))
        ...     if error:
        ...         raise HTTPException(401, detail=error)
        ...
        ...     payload, new_token, error = auth.decode_and_refresh(token)
        ...     if error:
        ...         raise HTTPException(401, detail=error)
        ...
        ...     if new_token:
        ...         request.state.new_token = new_token
        ...
        ...     return payload
    """

    def __init__(self, config: Dict[str, Any], secret_key: str):
        """
        Initialize auth middleware with configuration.

        Args:
            config: Kitebase configuration dict (from plugins.config)
            secret_key: Secret key for JWT encoding/decoding
        """
        self.config = config
        self.secret_key = secret_key

        # Extract auth configuration
        auth_config = config.get('authentication', {})
        self.jwt_expiration_hours = auth_config.get('jwt_expiration_hours', 24)
        self.refresh_interval_minutes = auth_config.get('jwt_refresh_interval_minutes', 20)
        self.context_fields = auth_config.get('context_fields', [])

    def extract_token(self, authorization_header: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract JWT token from Authorization header.

        Returns:
            Tuple of (token, error)
        """
        return extract_bearer_token(authorization_header)

    def decode_and_refresh(self, token: str) -> Tuple[Optional[Dict], Optional[str], Optional[str]]:
        """
        Decode JWT token and check if refresh is needed.

        Returns:
            Tuple of (payload, new_token, error)
        """
        return decode_and_check_refresh(
            token,
            self.secret_key,
            self.jwt_expiration_hours,
            self.refresh_interval_minutes
        )

    def login(self, command_processor, credentials: Dict[str, str]) -> Dict[str, Any]:
        """
        Handle login using configured parameters.

        Args:
            command_processor: Kitebase command processor
            credentials: {'username': '...', 'password': '...'}

        Returns:
            Response dict with token and user data
        """
        return handle_auth(
            command_processor,
            credentials,
            self.secret_key,
            self.jwt_expiration_hours,
            self.context_fields
        )

    def issue_token(self, user_data: Dict[str, Any]) -> Dict[str, Any]:
        """Sign a context the caller vouches for, with this app's settings."""
        return issue_token(user_data, self.secret_key,
                           self.jwt_expiration_hours, self.context_fields)

    def update_context(self, current_context: Dict[str, Any],
                       updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle a context update: reissue a JWT with the (allowlisted) updates.

        The allowlist is framework fields + this app's custom context fields, so
        a client can set op_date / declared custom fields but never identity.
        """
        return handle_update_context(
            current_context,
            updates,
            self.secret_key,
            self.jwt_expiration_hours,
            allowed_fields=custom_context_fields(self.config)
        )


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
        rebuilding it.
    """
    from kitebase.clientui import client_settings

    return {
        'status': 'success',
        'data': {
            'application': plugins_config.get('name', 'Unknown'),
            'version': plugins_config.get('version', '0.0.0'),
            'description': plugins_config.get('description', ''),
            'client': client_settings(plugins_config)._asdict(),
            'kitebase_api_prefix': api_prefix,
            'available_endpoints': {
                'home': '/',
                'app_info': '/info',
                'kitebase_auth': f'{api_prefix}/auth/login',
                'kitebase_auth_update': f'{api_prefix}/auth/update_context',
                'kitebase_database': f'{api_prefix}/db/<table>',
                'kitebase_query': f'{api_prefix}/query',
                'kitebase_files': f'{api_prefix}/read_file',
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

    from kitebase.db import BaseApp
    from kitebase.i18n import set_locale
    from kitebase.querybuilder import JSONEncoder

    config = plugins.config
    prefix, endpoint_prefix = _prefixes(config, prefix, endpoint_prefix)
    auth = auth or AuthMiddleware(config, secret_key)
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
        BaseApp.set_context(None)

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

    return auth


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
    """
    import json

    from fastapi import Depends, Request, Response

    from kitebase.db import BaseApp
    from kitebase.i18n import set_locale
    from kitebase.querybuilder import JSONEncoder

    config = plugins.config
    prefix, endpoint_prefix = _prefixes(config, prefix, endpoint_prefix)
    auth = auth or AuthMiddleware(config, secret_key)
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

    async def clear_context():
        """Leave the worker as it was found — see register_flask's teardown."""
        yield
        BaseApp.set_context(None)

    cleanup = [Depends(clear_context)]

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

    @target.get(f'{prefix}/info', dependencies=cleanup)
    def kitebase_info():
        return reply(get_app_info(config, prefix))

    @target.post(f'{prefix}/auth/login', dependencies=cleanup)
    def kitebase_login(data: dict):
        try:
            return reply(auth.login(command_processor, data))
        except Exception as e:
            return reply({'status': 'error', 'message': str(e)}, 500)

    @target.post(f'{prefix}/auth/update_context', dependencies=cleanup)
    def kitebase_update_context(data: dict, request: Request):
        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        return reply(auth.update_context(user, data), new_token=new_token)

    if host_session is not None:
        @target.post(f'{prefix}/auth/token', dependencies=cleanup)
        def kitebase_host_token(request: Request):
            """A JWT for the user of the host's session (see handle_host_token)."""
            content_type = request.headers.get('content-type', '')
            if not content_type.startswith('application/json'):
                return reply(HOST_TOKEN_NOT_JSON, 415)
            return reply(handle_host_token(host_session, request, auth))

    @target.post(f'{prefix}/{endpoint_prefix}/{{operation}}', dependencies=cleanup)
    def kitebase_dispatch(operation: str, data: dict, request: Request):
        """Everything that is not authentication: db, query, get_page, get_menu…"""
        user, refused, new_token = identify(request)
        if refused is not None:
            return refused
        return reply(
            handle_generic_endpoint(command_processor, operation, data, context=user),
            new_token=new_token)

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
