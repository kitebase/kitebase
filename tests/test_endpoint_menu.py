"""Tests for kitebase.endpoint_menu.get_menu — flat menu_items → rendered tree.

Uses a PluginsManager populated via merge_dicts (real $plugin attribution) and
drives the endpoint through kitebase.utils.get_app, monkeypatched to a FakeApp.
Covers: default cascade (§4), tree building by parent, ordering, root selection,
group convergence, orphan fallback, and internal-key stripping.
"""
import pytest

import kitebase.utils
from kitebase.plugins import PluginsManager
from kitebase.endpoint_menu import get_menu


class FakeApp:
    def __init__(self, pm):
        self.pm = pm


def make_app(menus, menu_items, plugin='menuplugin', app_config=None):
    pm = PluginsManager()
    pm.config = app_config if app_config is not None else {'menu': {'default_root': 'main'}}
    pm.merge_dicts({'menus': menus, 'menu_items': menu_items}, plugin)
    return FakeApp(pm)


@pytest.fixture
def patch_app(monkeypatch):
    """Return a setter that points kitebase.utils.get_app at a given app."""
    def _set(app):
        monkeypatch.setattr(kitebase.utils, 'get_app', lambda: app)
    return _set


# ── Fixture data: two groups, leaves nested by parent, one item in another root ──
DEVTEST_MENUS = {
    'main': {'label': 'DevTest', 'home_page': 'hello_demo'},
    'admin': {'label': 'Admin'},
}
DEVTEST_ITEMS = {
    'catalog': {'label': 'Catalog', 'icon': 'book', 'order': 20},
    'masters': {'label': 'Masters', 'icon': 'database', 'order': 10},
    'books':   {'label': 'Books', 'parent': 'catalog', 'order': 30,
                'action': 'stack_push', 'panel': 'book_list'},
    'authors': {'label': 'Authors', 'parent': 'catalog', 'order': 10,
                'action': 'stack_push', 'panel': 'author_list'},
    'users':   {'label': 'Users', 'parent': 'masters', 'order': 10,
                'action': 'stack_push', 'panel': 'user_list'},
    'audit':   {'label': 'Audit', 'root': 'admin', 'action': 'stack_push',
                'panel': 'audit_list'},
}


def test_root_attributes_and_selection(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    res = get_menu({'id': 'main'})
    assert res['status'] == 'success' and res['code'] == 200
    data = res['data']
    assert data['id'] == 'main'
    assert data['label'] == 'DevTest'
    assert data['home_page'] == 'hello_demo'
    # 'audit' belongs to root 'admin' → not present in main
    top_ids = {n['id'] for n in data['items']}
    assert top_ids == {'catalog', 'masters'}


def test_default_root_when_id_omitted(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    res = get_menu({})
    assert res['data']['id'] == 'main'  # from app config menu.default_root


def test_tree_nesting_and_ordering(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    items = get_menu({'id': 'main'})['data']['items']
    # Top-level groups ordered by `order`: masters(10) before catalog(20)
    assert [n['id'] for n in items] == ['masters', 'catalog']
    catalog = next(n for n in items if n['id'] == 'catalog')
    # Children ordered: authors(10) before books(30)
    assert [c['id'] for c in catalog['children']] == ['authors', 'books']
    # Leaf carries its action; group has no action but has children
    books = catalog['children'][1]
    assert books['action'] == 'stack_push' and books['panel'] == 'book_list'
    assert 'action' not in catalog and 'children' in catalog


def test_leaf_has_no_children_key(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    items = get_menu({'id': 'main'})['data']['items']
    users = next(n for n in items if n['id'] == 'masters')['children'][0]
    assert users['id'] == 'users'
    assert 'children' not in users


def test_internal_keys_stripped(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    data = get_menu({'id': 'main'})['data']

    def walk(nodes):
        for n in nodes:
            assert not (_INTERNAL := {'$plugin', 'parent', 'root', 'access'}) & n.keys()
            walk(n.get('children', []))
    walk(data['items'])


def test_other_root(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    data = get_menu({'id': 'admin'})['data']
    assert [n['id'] for n in data['items']] == ['audit']


def test_unknown_root_404(patch_app):
    patch_app(make_app(DEVTEST_MENUS, DEVTEST_ITEMS))
    res = get_menu({'id': 'nope'})
    assert res['status'] == 'error' and res['code'] == 404


def test_group_convergence_across_plugins(patch_app):
    """Two plugins append leaves to the same group id → they converge."""
    pm = PluginsManager()
    pm.config = {'menu': {'default_root': 'main'}}
    pm.merge_dicts({'menus': {'main': {'label': 'M'}},
                    'menu_items': {'catalog': {'label': 'Catalog', 'order': 10},
                                   'books': {'label': 'Books', 'parent': 'catalog', 'order': 10}}},
                   'plugin_a')
    pm.merge_dicts({'menu_items': {'wizards': {'label': 'Wizards', 'parent': 'catalog', 'order': 20}}},
                   'plugin_b')
    app = FakeApp(pm)
    import kitebase.utils as u
    orig = u.get_app
    u.get_app = lambda: app
    try:
        catalog = get_menu({'id': 'main'})['data']['items'][0]
    finally:
        u.get_app = orig
    assert catalog['id'] == 'catalog'
    assert [c['id'] for c in catalog['children']] == ['books', 'wizards']


def test_orphan_parent_falls_back_to_top_level(patch_app):
    """An item whose parent isn't in this root attaches at top level, not dropped."""
    patch_app(make_app(
        {'main': {'label': 'M'}},
        {'stray': {'label': 'Stray', 'parent': 'ghost_group',
                   'action': 'stack_push', 'panel': 'x'}}))
    items = get_menu({'id': 'main'})['data']['items']
    assert [n['id'] for n in items] == ['stray']


def test_plugin_default_cascade(patch_app):
    """A plugin's menu.default_root/default_parent apply to its item-less-of-those."""
    pm = PluginsManager()
    pm.config = {'menu': {'default_root': 'main'}}
    pm.merge_dicts({'menus': {'main': {'label': 'M'}},
                    'menu_items': {'grp': {'label': 'Grp', 'order': 10},
                                   'leaf': {'label': 'Leaf', 'action': 'stack_push', 'panel': 'p'}}},
                   'contentplugin')

    class PluginCfg:
        config = {'menu': {'default_parent': 'grp'}}
    pm.plugins['contentplugin'] = PluginCfg()

    app = FakeApp(pm)
    import kitebase.utils as u
    orig = u.get_app
    u.get_app = lambda: app
    try:
        items = get_menu({'id': 'main'})['data']['items']
    finally:
        u.get_app = orig
    # 'leaf' has no explicit parent → plugin default_parent 'grp'
    grp = next(n for n in items if n['id'] == 'grp')
    assert [c['id'] for c in grp['children']] == ['leaf']
