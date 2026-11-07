"""
kitebase.endpoint_help: the `get_help` endpoint, the guide inside the application.

The same page the command line writes, composed for the current user and in
their language. The client shows it in a frame, without scripts in it: anchors
work by themselves, and the client intercepts the links that carry `data-help`
by sending their value back here as `ref`.
"""
import logging
from typing import Any, Dict

import kitebase.utils
from kitebase import access
from kitebase.endpoints import endpoint
from kitebase.help import focus_anchor, guide_root, help_merge, render_html, target_page
from kitebase.i18n import _, current_locale
from kitebase.pages import load_page, page_permissions

logger = logging.getLogger(__name__)


@endpoint('get_help', access=access.AUTHENTICATED)
def get_help(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    The guide of where the user is.

    Parameters:
        stack: the ids of the pages open, from the bottom: the guide is the one
               that tells the top (`help.guide_root`)
        ref:   instead of `stack`, what a link of a guide points to: a page,
               `Table` or `Table.column`
        focus: where to open it, a field (`Book.title`) or a screen; by default
               the top of the stack, or the reference

    Opening the guide of a page asks what opening the page asks: a refusal is a
    403. What the user may not open is left out of it.

    Returns:
        { status, data: {html, title, root, anchor}, code }
    """
    app = kitebase.utils.get_app()
    stack = data.get('stack')
    ref = data.get('ref')
    focus = data.get('focus')
    if isinstance(ref, str) and ref:
        root = target_page(app, ref)
        if root is None:
            return {'status': 'error', 'message': f"Nothing answers to '{ref}'", 'code': 404}
        focus = focus or ref
    elif isinstance(stack, list) and stack and all(isinstance(p, str) and p for p in stack):
        root = guide_root(app, stack)
        focus = focus or stack[-1]
    else:
        return {'status': 'error', 'message': 'stack or ref is required', 'code': 400}

    if load_page(app, root) is None:
        return {'status': 'error', 'message': f"Page not found: '{root}'", 'code': 404}
    for permission in page_permissions(app, root) or []:
        access.check(permission)

    doc = help_merge(app, root, current_locale(), for_user=True)
    rendered = render_html(app, doc)
    for warning in rendered.warnings:
        logger.warning('[help] %s: %s', root, warning)
    return {'status': 'success', 'code': 200, 'data': {
        'html': rendered.html,
        'title': _(doc.title),
        'root': root,
        'anchor': focus_anchor(doc, focus),
    }}
