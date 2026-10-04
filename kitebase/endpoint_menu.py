"""
kitebase.endpoint_menu — the `get_menu` endpoint (symmetric to `get_page`).

The plugin loader composes the flat `menus:` (roots) and `menu_items:` sections
via deep_merge (multi-plugin). This endpoint turns that flat, layout-agnostic
data into a rendered tree for one named root:

  get_menu(id) pipeline (docs/pending/menu.md §6):
    1. Compose         — already done by the loader (multi-plugin merge)
    2. Resolve default — root/parent filled via the config cascade (§4)
    3. Auth filter ⏳  — recursively drop items whose `access:` isn't satisfied
                         (server-side; slot only until RBAC lands)
    4. Build tree      — nest items by their effective `parent`, order by `order`
    5. Resolve $ref    — expand refs embedded in item props

The client (Chrome sidebar) receives a composed, filtered, ordered tree and does
not know how it was built — identical to the `get_panel`/`get_page` contract.

Cross-root placement of the *same* item in multiple roots (§7, `menus.<id>.items`
with `$ref`) is a follow-up: this first slice implements the flat `parent` model
that the devtest walking skeleton exercises (§9).
"""
from typing import Any, Dict, List, Optional

import kitebase.utils
from kitebase.endpoints import endpoint

# Merge metadata (`$plugin`) plus keys consumed server-side (`parent`/`root` drive
# tree/cascade; `access` gates visibility) — none of these are sent to the client.
_INTERNAL_KEYS = frozenset({'$plugin', 'parent', 'root', 'access'})

# Items without an explicit `order` sort after ordered ones, then by label.
_ORDER_LAST = 1_000_000


def _current_context() -> Optional[Dict[str, Any]]:
    """The active request context (JWT-derived), or None outside a request."""
    try:
        import kitebase.db
        return kitebase.db.BaseApp.get_context()
    except Exception:
        return None


def _app_default_root(app: Any) -> str:
    """App-wide fallback root (config.yaml `menu.default_root`)."""
    return app.pm.config.get('menu', {}).get('default_root', 'main')


def _plugin_menu_cfg(app: Any, plugin_name: Optional[str]) -> Dict[str, Any]:
    """The `menu:` section of a plugin's config.yaml, or {} (also in tests where
    pm.plugins isn't populated)."""
    plugin = app.pm.plugins.get(plugin_name) if plugin_name else None
    if plugin is None:
        return {}
    return plugin.config.get('menu', {}) or {}


def _effective_root(app: Any, item: Dict[str, Any]) -> str:
    """Cascade (§4): item.root → plugin.menu.default_root → app.menu.default_root."""
    if item.get('root') is not None:
        return item['root']
    pcfg = _plugin_menu_cfg(app, item.get('$plugin'))
    if 'default_root' in pcfg:
        return pcfg['default_root']
    return _app_default_root(app)


def _effective_parent(app: Any, item: Dict[str, Any]) -> Optional[str]:
    """Cascade (§4): item.parent → plugin.menu.default_parent → None (root-level).

    An explicit `parent: ~` (present but None) pins the item to root level and
    overrides the plugin default — presence of the key is what matters.
    """
    if 'parent' in item:
        return item['parent']
    return _plugin_menu_cfg(app, item.get('$plugin')).get('default_parent')


def _passes_access(item: Dict[str, Any], context: Optional[Dict[str, Any]]) -> bool:
    """Auth filter slot (§6 step 3). No ACL/RBAC system yet → allow everything.

    When record-rule/RBAC lands this evaluates `item['access']` against `context`
    (same server-side, non-bypassable filtering as get_panel's role-filtering).
    """
    return True


@endpoint('get_menu')
def get_menu(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a composed, filtered, ordered menu tree for one root.

    Parameters:
        id: Root menu id (e.g. "main", "production"). Defaults to the app's
            `menu.default_root` when omitted.

    Returns:
        { status, data: { id, label, ...root_attrs, items: [<node>, ...] }, code }

        Each node carries the item's presentation/action attributes (label, icon,
        order, action, panel, props, op, params, style, badge, ...) plus its `id`,
        and a `children` list when it is a group with descendants. Internal keys
        ($plugin, parent, root, access) are stripped.
    """
    app = kitebase.utils.get_app()

    # Skip merge-metadata keys ($plugin and any future $-prefixed convention),
    # matching how the rest of the codebase iterates a merged section
    # (db.py _calc_tables, types.py, diagnostics.py all use startswith('$')).
    menus = {k: v for k, v in (app.pm.data.get('menus') or {}).items() if not k.startswith('$')}
    raw_items = {k: v for k, v in (app.pm.data.get('menu_items') or {}).items() if not k.startswith('$')}

    root_id = data.get('id') or _app_default_root(app)
    root_def = menus.get(root_id)
    if not isinstance(root_def, dict):
        return {'status': 'error', 'message': f"Menu not found: '{root_id}'", 'code': 404}

    context = _current_context()

    # 2/3. Select the items of this root that pass the auth filter, resolving any
    #      $ref embedded in their props along the way (§6 steps 2,3,5).
    selected: Dict[str, Dict[str, Any]] = {}
    for item_id, item in raw_items.items():
        if not isinstance(item, dict):
            continue
        if _effective_root(app, item) != root_id:
            continue
        if not _passes_access(item, context):
            continue
        selected[item_id] = app.pm.resolve_refs(item)

    # 4. Build the parent → children map. An item whose effective parent is not a
    #    selected node of this root (missing, self, or belongs elsewhere) attaches
    #    at top level — a graceful fallback; diagnostics flags the real orphan.
    children_of: Dict[Optional[str], List[str]] = {}
    for item_id, item in selected.items():
        parent = _effective_parent(app, item)
        if parent == item_id or parent not in selected:
            parent = None
        children_of.setdefault(parent, []).append(item_id)

    def build(parent_id: Optional[str], _seen: frozenset) -> List[Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        for item_id in children_of.get(parent_id, []):
            if item_id in _seen:  # cycle guard (a↔b parent loop)
                continue
            item = selected[item_id]
            node = {k: v for k, v in item.items()
                    if k not in _INTERNAL_KEYS and not k.startswith('$')}
            node['id'] = item_id
            kids = build(item_id, _seen | {item_id})
            if kids:
                node['children'] = kids
            nodes.append(node)
        nodes.sort(key=lambda n: (n.get('order', _ORDER_LAST), str(n.get('label', ''))))
        return nodes

    tree = build(None, frozenset())

    root_out = {k: v for k, v in root_def.items()
                if k not in _INTERNAL_KEYS and k != 'items'}
    root_out['id'] = root_id
    root_out['items'] = tree

    return {'status': 'success', 'data': root_out, 'code': 200}
