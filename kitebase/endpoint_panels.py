from typing import Any, Dict
import kitebase.utils
from kitebase import access, settings
from kitebase.endpoints import endpoint
from kitebase.errors import Invalid, NotFound
from kitebase.outbound import to_client
from kitebase.pages import load_page, page_permissions, restrict_page
# Note: context is set globally via kitebase.context.replace() before each call —
# endpoint functions receive only (data).


@endpoint('get_page', access=access.AUTHENTICATED)
def get_page(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a resolved page descriptor by id.

    Resolves all $ref fields, completes collection nodes (a node's `model:` fills
    its view's `source.model`), gives commands without a label the one their id
    spells, strips internal $plugin metadata, and translates the labels to the
    current locale before sending to the client. A `$setting.<key>` string is
    replaced by the setting's value (kitebase.settings).

    Opening a page asks `<Model>.read` on the model it shows and the page's own
    `access:`; a refusal is a 403 naming the permission. What the user cannot
    use inside it is taken out (`pages.restrict_page`).

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
        raise Invalid('id is required')

    app = kitebase.utils.get_app()
    page = load_page(app, page_id)
    if page is None:
        raise NotFound(f"Page not found: '{page_id}'")

    for permission in page_permissions(app, page_id) or []:
        access.check(permission)
    restrict_page(app, page)
    settings.resolve_tokens(page)

    return {'status': 'success', 'data': to_client(page), 'code': 200}
