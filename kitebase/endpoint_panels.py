from typing import Any, Dict
import kitebase.utils
from kitebase import access
from kitebase.endpoints import endpoint
from kitebase.i18n import translate_labels
from kitebase.pages import load_page, page_permissions, restrict, restrict_columns, strip_meta
# Note: context is set globally via BaseApp.set_context() before each call —
# endpoint functions receive only (data).


@endpoint('get_page', access=access.AUTHENTICATED)
def get_page(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a resolved page descriptor by id.

    Resolves all $ref fields, completes collection nodes (a node's `model:` fills
    its view's `source.model`), gives commands without a label the one their id
    spells, strips internal $plugin metadata, and translates the labels to the
    current locale before sending to the client.

    Opening a page asks `<Model>.read` on the model it shows and the page's own
    `access:`; a refusal is a 403 naming the permission. What the user cannot
    use inside it is taken out (`pages.restrict`).

    Resolution order:
      1. Explicit pages dict (YAML plugin pages)
      2. Auto-generated fallback:
           {table}_list  →  minimal table view for that DB table
           {table}_form  →  auto form descriptor for that table

    Parameters:
        id: Page id (e.g. "book_list", "book_form", "Author_list")

    Returns:
        { status, data: <resolved page descriptor>, code }
    """
    page_id = data.get('id')
    if not page_id:
        return {'status': 'error', 'message': 'id is required', 'code': 400}

    app = kitebase.utils.get_app()
    try:
        page = load_page(app, page_id)
    except ValueError as e:
        return {'status': 'error', 'message': str(e), 'code': 400}

    if page is None:
        return {'status': 'error', 'message': f"Page not found: '{page_id}'", 'code': 404}

    for permission in page_permissions(app, page_id) or []:
        access.check(permission)
    restrict(app, page)
    restrict_columns(app, page)

    return {'status': 'success', 'data': translate_labels(strip_meta(page)), 'code': 200}
