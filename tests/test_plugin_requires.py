"""The third-party packages a plugin declares under `requires`.

The plugin declares, kitebase checks, the application installs. What is
covered: a package that is there, one that is not, an extra whose dependencies
are missing, a malformed declaration, and the consequence that matters most:
a plugin with a requirement missing is merged but its Python is not imported,
so the error names the package instead of an import failing half-way through.
"""
import pytest
import yaml

from kitebase.plugins import PluginsManager, unmet_requirement


def load(tmp_path, monkeypatch, requires=None, source=None):
    """An app with one plugin, `shelf`, declaring `requires`."""
    plugin = tmp_path / 'plugins' / 'shelf'
    plugin.mkdir(parents=True)
    config = {'name': 'shelf', 'version': '0.0.1'}
    if requires is not None:
        config['requires'] = requires
    (plugin / 'config.yaml').write_text(yaml.safe_dump(config))
    if source is not None:
        (plugin / 'shelf_code.py').write_text(source)

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    return pm


def codes(pm):
    return [i['code'] for i in pm.issues]


# ── The check ─────────────────────────────────────────────────────────────────

def test_an_installed_package_is_met():
    assert unmet_requirement('pyyaml') is None


def test_the_name_is_compared_normalized():
    assert unmet_requirement('PyYAML') is None


def test_a_missing_package_is_named():
    assert unmet_requirement('no-such-package-here') == "'no-such-package-here' is not installed"


def test_an_extra_and_a_version_are_read_past():
    assert unmet_requirement('kitebase[flask, fastapi] >= 0.1') is None


def test_an_extra_the_package_does_not_declare():
    assert unmet_requirement('kitebase[nope]') == "'kitebase' declares no extra 'nope'"


def test_an_extra_whose_dependency_is_missing(monkeypatch):
    """`andrep` installed without `[playwright]` is not andrep[playwright]."""
    from importlib import metadata

    monkeypatch.setattr(metadata, 'requires', lambda name: [
        'playwright; extra == "playwright"', 'sqlalchemy>=2.0; extra == "sqlalchemy"'])
    real = metadata.distribution

    def distribution(name):
        if name == 'playwright':
            raise metadata.PackageNotFoundError(name)
        return real(name)
    monkeypatch.setattr(metadata, 'distribution', distribution)

    assert unmet_requirement('andrep[sqlalchemy]') is None
    assert unmet_requirement('andrep[playwright]') == (
        "'andrep[playwright]' needs 'playwright', which is not installed")


def test_something_that_is_not_a_requirement():
    assert unmet_requirement('!!') == "'!!' is not a requirement"


# ── On a plugin ───────────────────────────────────────────────────────────────

def test_met_requirements_leave_no_issue(tmp_path, monkeypatch):
    pm = load(tmp_path, monkeypatch, {'python': ['pyyaml']})

    assert codes(pm) == []
    assert pm.plugins['shelf'].unmet == []


def test_a_missing_requirement_is_an_error_naming_plugin_and_package(tmp_path, monkeypatch):
    pm = load(tmp_path, monkeypatch, {'python': ['no-such-package-here>=1']})

    [issue] = pm.issues
    assert issue['severity'] == 'error'
    assert issue['code'] == 'requirement-missing'
    assert issue['plugin'] == 'shelf'
    assert "Plugin 'shelf' requires no-such-package-here>=1" in issue['message']
    assert 'uv add "no-such-package-here>=1"' in issue['message']


def test_a_plugin_missing_a_requirement_loads_no_python(tmp_path, monkeypatch):
    """The import would fail; the error has already said why."""
    pm = load(tmp_path, monkeypatch, {'python': ['no-such-package-here']},
              source='import no_such_package_here\n')

    assert pm.get_sources() == []
    assert 'shelf' in pm.plugins          # its YAML is still merged


def test_a_plugin_whose_requirements_are_met_loads_its_python(tmp_path, monkeypatch):
    pm = load(tmp_path, monkeypatch, {'python': ['pyyaml']}, source='import yaml\n')

    assert [p.name for p in pm.get_sources()] == ['shelf_code.py']


@pytest.mark.parametrize('requires', [
    ['pyyaml'],                      # a list, not a mapping
    {'pip': ['pyyaml']},             # a key it does not know
    {'python': 'pyyaml'},            # a string, not a list
    {'python': [{'name': 'pyyaml'}]},
])
def test_a_malformed_declaration_is_an_error(tmp_path, monkeypatch, requires):
    pm = load(tmp_path, monkeypatch, requires)

    assert codes(pm) == ['requires-malformed']


def test_no_declaration_is_nothing_to_check(tmp_path, monkeypatch):
    assert codes(load(tmp_path, monkeypatch)) == []
