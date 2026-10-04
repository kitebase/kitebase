from typing import Any, Dict
import kitebase.utils
from kitebase.endpoints import endpoint
from kitebase.pages import load_page, strip_meta
# Note: context is set globally via BaseApp.set_context() before each call —
# endpoint functions receive only (data).


@endpoint('get_page')
def get_page(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a resolved page descriptor by id.

    Resolves all $ref fields, completes collection nodes (a node's `model:` fills
    its view's `source.model`), and strips internal $plugin metadata before
    sending to the client.

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
        return {'status': 'error', 'message': f"Panel not found: '{page_id}'", 'code': 404}

    return {'status': 'success', 'data': strip_meta(page), 'code': 200}
