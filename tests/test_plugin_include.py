"""Tests for per-root plugin selection — the `include` key of a plugins root.

A plugin root used to mean "every plugin in this directory", which held while
the root was the application's own. A shared root breaks the assumption: an app
wants some of what the repository carries, and what the repository gains later
must not arrive unasked.

`include` names the plugins wanted from a root and pulls in their dependencies.
Covered here: whole-root default, selection, dependency closure, unknown names,
malformed entries, and the fact that a name collision only counts between
plugins actually selected.
"""
import pytest
import yaml

from kitebase.plugins import PluginsManager


def make_plugin(root, name, depends_on=None, table=None):
    """Write a minimal plugin directory into `root`."""
    d = root / name
    d.mkdir(parents=True)
    config = {'name': name, 'version': '0.0.1'}
    if depends_on is not None:
        config['depends_on'] = depends_on
    (d / 'config.yaml').write_text(yaml.safe_dump(config))
    (d / 'model.yaml').write_text(yaml.safe_dump(
        {'tables': {table or name.capitalize(): {'columns': []}}}))
    return d


@pytest.fixture
def commons(tmp_path):
    """A shared root: common, users -> common, partners -> common, dms -> partners."""
    root = tmp_path / 'commons'
    make_plugin(root, 'common')
    make_plugin(root, 'users', depends_on=['common'])
    make_plugin(root, 'partners', depends_on=['common'])
    make_plugin(root, 'dms', depends_on=['partners'])
    return root


def load(tmp_path, monkeypatch, roots):
    """Run PluginsManager against a config whose `plugins:` is `roots`."""
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': roots}))
    monkeypatch.chdir(tmp_path)
    manager = PluginsManager()
    manager.load_config(str(cfg))
    manager.load_plugins()
    return manager


def test_bare_path_takes_the_whole_root(tmp_path, monkeypatch, commons):
    manager = load(tmp_path, monkeypatch, ['commons'])
    assert set(manager.plugins) == {'common', 'users', 'partners', 'dms'}


def test_include_narrows_the_root(tmp_path, monkeypatch, commons):
    manager = load(tmp_path, monkeypatch,
                   [{'path': 'commons', 'include': ['users']}])
    assert set(manager.plugins) == {'common', 'users'}  # common pulled in as a dep


def test_include_resolves_transitive_dependencies(tmp_path, monkeypatch, commons):
    """dms -> partners -> common: naming the leaf is enough."""
    manager = load(tmp_path, monkeypatch,
                   [{'path': 'commons', 'include': ['dms']}])
    assert set(manager.plugins) == {'common', 'partners', 'dms'}


def test_excluded_plugin_is_absent_from_the_schema(tmp_path, monkeypatch, commons):
    """Selection must keep the merged data out, not just the plugin registry."""
    manager = load(tmp_path, monkeypatch,
                   [{'path': 'commons', 'include': ['users']}])
    assert 'Dms' not in manager.data.get('tables', {})
    assert 'Users' in manager.data.get('tables', {})


def test_dependency_is_pulled_across_roots(tmp_path, monkeypatch, commons):
    """A plugin in one root may depend on one the other root did not include.

    Selection runs over every root at once for this reason: resolving the
    closure root by root would leave `partners` out — the commons root was only
    asked for `users` — and turn it into a missing-dependency error.
    """
    app = tmp_path / 'app'
    make_plugin(app, 'billing', depends_on=['partners'])

    manager = load(tmp_path, monkeypatch,
                   [{'path': 'commons', 'include': ['users']}, 'app'])
    assert set(manager.plugins) == {'common', 'users', 'partners', 'billing'}
    assert 'dms' not in manager.plugins       # not asked for, not referenced


def test_ambiguous_dependency_is_an_error(tmp_path, monkeypatch, commons):
    """Reached as a dependency with two providers: the name alone cannot choose."""
    other = tmp_path / 'other'
    make_plugin(other, 'partners', table='OtherPartners')
    app = tmp_path / 'app'
    make_plugin(app, 'billing', depends_on=['partners'])

    with pytest.raises(ValueError, match='more than one'):
        load(tmp_path, monkeypatch,
             [{'path': 'commons', 'include': ['users']},
              {'path': 'other', 'include': []}, 'app'])


def test_alternative_plugins_are_chosen_by_including_one(tmp_path, monkeypatch, commons):
    """Two roots may hold alternative plugins under the same name.

    Naming the one you want is how you choose; the other stays inert. Only when
    nothing says which — both roots taken whole — is it a conflict.
    """
    other = tmp_path / 'other'
    make_plugin(other, 'dms', table='OtherDms')
    make_plugin(other, 'reporting')

    manager = load(tmp_path, monkeypatch,
                   [{'path': 'commons', 'include': ['dms']},
                    {'path': 'other', 'include': ['reporting']}])
    assert set(manager.plugins) == {'common', 'partners', 'dms', 'reporting'}
    assert manager.plugins['dms'].plugin_dir.parent.name == 'commons'
    assert 'OtherDms' not in manager.data.get('tables', {})

    with pytest.raises(ValueError, match="use 'include'"):
        load(tmp_path, monkeypatch, ['commons', 'other'])


def test_selection_narrows_the_order_without_reshuffling(tmp_path, monkeypatch, commons):
    """Registration order decides deep_merge precedence between independent
    plugins, and the topological sort is only a partial order — so selecting a
    subset must leave the surviving plugins in the same relative order."""
    full = list(load(tmp_path, monkeypatch, ['commons']).plugins)
    subset = list(load(tmp_path, monkeypatch,
                       [{'path': 'commons', 'include': ['dms', 'users']}]).plugins)

    assert subset == [name for name in full if name in subset]


def test_unknown_include_name_is_an_error(tmp_path, monkeypatch, commons):
    with pytest.raises(ValueError, match='crm'):
        load(tmp_path, monkeypatch,
             [{'path': 'commons', 'include': ['users', 'crm']}])


def test_missing_dependency_across_roots_still_raises(tmp_path, monkeypatch, commons):
    """Including a dependent without its dependency available is a hard error."""
    root = tmp_path / 'app'
    make_plugin(root, 'billing', depends_on=['accounting'])
    with pytest.raises(ValueError, match='accounting'):
        load(tmp_path, monkeypatch, ['app'])


def test_collision_only_counts_between_selected_plugins(tmp_path, monkeypatch, commons):
    """A shared root may carry a name you also use — not including it is the way out."""
    app = tmp_path / 'app'
    make_plugin(app, 'dms', table='AppDms')

    with pytest.raises(ValueError, match='Duplicate plugin name'):
        load(tmp_path, monkeypatch, ['commons', 'app'])

    manager = load(tmp_path, monkeypatch,
                   [{'path': 'commons', 'include': ['users']}, 'app'])
    assert set(manager.plugins) == {'common', 'users', 'dms'}
    assert 'AppDms' in manager.data.get('tables', {})


def test_include_must_be_a_list(tmp_path, monkeypatch, commons):
    with pytest.raises(ValueError, match='must be a list'):
        load(tmp_path, monkeypatch, [{'path': 'commons', 'include': 'users'}])


def test_root_entry_needs_a_path(tmp_path, monkeypatch, commons):
    with pytest.raises(ValueError, match="no 'path'"):
        load(tmp_path, monkeypatch, [{'include': ['users']}])


def test_unknown_key_in_root_entry_is_an_error(tmp_path, monkeypatch, commons):
    """Typos must not degrade into 'take everything' in silence."""
    with pytest.raises(ValueError, match='exclude'):
        load(tmp_path, monkeypatch, [{'path': 'commons', 'exclude': ['dms']}])
