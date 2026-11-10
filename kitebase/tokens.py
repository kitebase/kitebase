"""
The token of a request and the context it carries.

Issuing, decoding and refreshing the JWT, the login and update_context
handlers, and the fields a login context is made of. Framework-agnostic: the
handlers return the plain dicts of kitebase.http_common.
"""

from datetime import datetime, timezone, timedelta
import sys
import jwt
from typing import Any, Callable, Dict, Optional, Tuple

from kitebase.http_common import _error_from_exc
from kitebase.registry import Registry


# ============================================
# JWT Token Management
# ============================================

def decode_and_check_refresh(
    token: str,
    secret_key: str,
    jwt_expiration_hours: int = 24,
    refresh_interval_minutes: int = 20,
    reload: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None
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
        reload: called on refresh with the payload; returns the fields to
                read again (what the user table says now), or None when the
                user may no longer log in, which refuses the token

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
            if reload is not None:
                fresh = reload(payload)
                if fresh is None:
                    return None, None, 'Account is inactive'
                payload = {**payload, **fresh}

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
                context_fields: Optional[list] = None,
                source: str = 'login') -> Dict[str, Any]:
    """
    Sign a user context into a JWT: the answer of a successful login.

    Only `username` and the configured `context_fields` go into the token.
    No op_date: absent means today, read per request by defaults.op_date(); a
    token outlives the day it was issued on, so it carries only a date the
    user chose (update_context).

    `src` says who vouches for the context: `login` read it from the user
    table, and a refresh reads it again; `host` is the host application's
    word (auth/token), kept as it is.
    """
    now = datetime.now(timezone.utc)
    payload = {
        'username': user_data.get('username'),
        'src': source,
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
    return auth.issue_token(context, source='host')

def handle_auth(
    dispatcher,
    data: Dict[str, Any],
    secret_key: str,
    jwt_expiration_hours: int = 24,
    context_fields: Optional[list] = None
) -> Dict[str, Any]:
    """
    Framework-agnostic authentication handler.

    Args:
        dispatcher: Kitebase command processor instance
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

        result = dispatcher.send(command)

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


# Fields a plugin adds to every login context, beside the configured ones: the
# plugin that reads a column off the user puts it in the token itself. Each
# under its name, with the module that registered it.
_CONTEXT_FIELDS: Registry[str] = Registry('context_fields', 'context field')

# The claims the token carries for itself: a field by one of these names would
# overwrite them, or be overwritten.
RESERVED_CONTEXT_FIELDS = frozenset({'username', 'src', 'exp', 'last_refresh', 'op_date'})


def add_context_field(name: str, owner: Optional[str] = None) -> None:
    """
    Put a column of the user table in every login context and token.

    The context is what a row condition compares with (`$ctx.<name>`), so its
    names are not shared: a name another module already registered, or one
    the token uses for itself, is refused naming both. `owner` defaults to
    the calling module; registering again from the same one is harmless.
    """
    if owner is None:
        owner = sys._getframe(1).f_globals.get('__name__', '?')
    if name in RESERVED_CONTEXT_FIELDS:
        raise ValueError(f"Context field '{name}' ({owner}): the token uses that name "
                         f"for itself")
    known = _CONTEXT_FIELDS.owner(name)
    if known is not None and known != owner:
        raise ValueError(f"Context field '{name}' is registered by both {known} "
                         f"and {owner}")
    _CONTEXT_FIELDS.add(name, name, owner=owner)


def context_fields(auth_config: Dict[str, Any], default: Optional[list] = None) -> list:
    """The configured `context_fields`, then those plugins registered."""
    fields = list(auth_config.get('context_fields', default or []))
    return fields + [f for f in _CONTEXT_FIELDS if f not in fields]


def reload_user_context(config: Dict[str, Any], payload: Dict[str, Any],
                        fields: list) -> Optional[Dict[str, Any]]:
    """
    The `fields` of a context that are columns of the user table, read again;
    None when the user is gone or no longer active.

    What a token says about its user is a copy taken at login: read again on
    each refresh, a role taken away or an account disabled counts within one
    refresh interval instead of when the token expires. Only for a token of
    the login: what the host vouched for, or a token from before `src`
    existed, stays as it is.
    """
    import kitebase.utils
    if payload.get('src') != 'login':
        return {}
    auth = config.get('authentication', {})
    user_table = auth.get('user_table', 'User')
    user_model = kitebase.utils.get_app().models.get(user_table)
    if user_model is None:
        return {}
    user = kitebase.utils.seek(user_table, {auth.get('username_field', 'username'):
                                            payload.get('username')})
    if user is None or (hasattr(user, 'is_active') and not user.is_active):
        return None
    columns = {c.key for c in user_model.__table__.columns}
    return {f: getattr(user, f) for f in fields if f in columns}


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

    @property
    def context_fields(self) -> list:
        # Read when used: plugins register theirs after this is built.
        return context_fields(self.config.get('authentication', {}))

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
            self.refresh_interval_minutes,
            reload=lambda payload: reload_user_context(self.config, payload,
                                                       self.context_fields)
        )

    def login(self, dispatcher, credentials: Dict[str, str]) -> Dict[str, Any]:
        """
        Handle login using configured parameters.

        Args:
            dispatcher: Kitebase command processor
            credentials: {'username': '...', 'password': '...'}

        Returns:
            Response dict with token and user data
        """
        return handle_auth(
            dispatcher,
            credentials,
            self.secret_key,
            self.jwt_expiration_hours,
            self.context_fields
        )

    def issue_token(self, user_data: Dict[str, Any], source: str = 'login') -> Dict[str, Any]:
        """Sign a context the caller vouches for, with this app's settings."""
        return issue_token(user_data, self.secret_key,
                           self.jwt_expiration_hours, self.context_fields, source)

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
