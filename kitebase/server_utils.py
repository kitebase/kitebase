"""
What the server file of an application imports.

    import kitebase.server_utils as srv

    srv.setup_logging(...)
    srv.register_flask(app, kitebase_app, plugins, SECRET_KEY)

The names live in the modules they belong to and are gathered here:
kitebase.tokens (the JWT and the login context), kitebase.http_common (the
framework-agnostic handlers), kitebase.http_flask and kitebase.http_fastapi
(the two adapters).
"""

import logging
import logging.handlers
import sys
from typing import Optional

from kitebase.http_common import (  # noqa: F401
    _error_from_exc, _error_from_result, _error_response, _prefixes, file_headers,
    get_app_info, handle_download, handle_generic_endpoint, handle_media_get,
    handle_upload, handle_upload_op, media_chunks, report_checks,
)
from kitebase.http_fastapi import register_fastapi, serve_client_fastapi  # noqa: F401
from kitebase.http_flask import register_flask, serve_client_flask  # noqa: F401
from kitebase.tokens import (  # noqa: F401
    FRAMEWORK_UPDATABLE_FIELDS, HOST_TOKEN_NOT_JSON, RESERVED_CONTEXT_FIELDS,
    AuthMiddleware,
    add_context_field, context_fields, custom_context_fields,
    decode_and_check_refresh, extract_bearer_token, handle_auth,
    handle_host_token, handle_update_context, issue_token, reload_user_context,
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
