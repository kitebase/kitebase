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
from kitebase.endpoints import FileResult, endpoint
from kitebase.help import (focus_anchor, guide_root, help_manual, help_merge, render_html,
                           render_manual, target_page)
from kitebase.i18n import _, current_locale
from kitebase.pages import load_page, page_permissions

logger = logging.getLogger(__name__)


@endpoint('get_help', access=access.AUTHENTICATED)
def get_help(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    The guide of where the user is.

    Parameters:
        stack: the ids of the pages open, from the bottom: the guide is the one
               that tells the top (`help.guide_root`); empty, the head of the
               manual, for the home
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
    elif stack == []:
        return _head(app)
    elif isinstance(stack, list) and all(isinstance(p, str) and p for p in stack):
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


def _head(app: Any) -> Dict[str, Any]:
    """The home has no page of its own: the head of the manual, the texts of the
    application and the menu, each entry leading to its guide."""
    manual = help_manual(app, current_locale(), for_user=True, cards=False)
    rendered = render_manual(app, manual)
    for warning in rendered.warnings:
        logger.warning('[help] home: %s', warning)
    return {'status': 'success', 'code': 200, 'data': {
        'html': rendered.html, 'title': manual.title, 'root': None, 'anchor': 'top'}}


@endpoint('help_manual', access=access.AUTHENTICATED)
def help_manual_file(data: Dict[str, Any]) -> Any:
    """
    The manual of the current user, through the download route: one HTML file
    with what they may open, opened in a new tab, to read or print. It holds
    texts and the shape of the screens, never data.
    """
    app = kitebase.utils.get_app()
    lang = current_locale()
    rendered = render_manual(app, help_manual(app, lang, for_user=True))
    for warning in rendered.warnings:
        logger.warning('[help] manual: %s', warning)
    name = (app.pm.config.get('name') or 'manual').lower()
    return FileResult(rendered.html.encode('utf-8'), f'{name}-manual-{lang}.html',
                      'text/html; charset=utf-8', 'inline')
