"""References to a record of any table: read only as the caller may read the
record, with its permissions and its record rules."""
import importlib.util

import pytest
import yaml

import kitebase.utils
from kitebase import access, refs
from kitebase.app import App
from kitebase.errors import NotFound
from kitebase.model_base import Base
from kitebase.plugins import PluginsManager
from kitebase.source import Generator

TABLES = {
    'Book': {
        'name': 'books',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'title', 'type': 'String', 'length': 120},
            {'name': 'shelf', 'type': 'String', 'length': 10},
        ],
    },
    # No column a label is taken from by convention
    'Stamp': {
        'name': 'stamps',
        'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'code', 'type': 'String', 'length': 10},
        ],
    },
}


class Policy(access.Guard):
    """Grants what it is given, each permission with its conditions or None."""

    def __init__(self, grants):
        self.grants = grants

    def allows(self, permission, context):
        return permission in self.grants

    def allows_undeclared(self, endpoint, context):
        return True

    def conditions(self, permission, context):
        return self.grants.get(permission)


@pytest.fixture(autouse=True)
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()
    access.GUARD.reset()


@pytest.fixture
def app(tmp_path, monkeypatch):
    plugin = tmp_path / 'plugins' / 'bench'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'bench', 'version': '0.0.1'}), encoding='utf-8')
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': TABLES}), encoding='utf-8')
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'bench', 'plugins': ['plugins']}), encoding='utf-8')
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    instance = App()
    instance.calc_db(pm)
    source = tmp_path / 'model.py'
    Generator(instance).generate(filename=str(source))
    spec = importlib.util.spec_from_file_location('refs_model', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    instance.initialize_db(f'sqlite:///{tmp_path / "bench.sqlite"}', module, check_schema=False)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: instance)

    with instance.get_session() as session:
        session.add(instance.models['Book'](id=1, title='Il barone rampante', shelf='a'))
        session.add(instance.models['Book'](id=2, title='Le città invisibili', shelf='b'))
        session.add(instance.models['Stamp'](id=1, code='X1'))
        session.commit()
    return instance


def policy(grants):
    access.set_guard(Policy(grants))


# ── check_readable ───────────────────────────────────────────────────────────

def test_a_readable_record_passes_with_its_key_as_text(app):
    policy({'Book.read': None})
    assert refs.check_readable('Book', '1') is True


def test_without_read_the_table_is_refused(app):
    policy({})
    with pytest.raises(access.AccessDenied):
        refs.check_readable('Book', 1)


def test_a_missing_record_is_not_found(app):
    policy({'Book.read': None})
    with pytest.raises(NotFound):
        refs.check_readable('Book', 99)


def test_a_record_a_rule_hides_is_not_found(app):
    policy({'Book.read': [{'shelf': 'a'}]})
    assert refs.check_readable('Book', 1) is True
    with pytest.raises(NotFound):
        refs.check_readable('Book', 2)


def test_a_name_that_is_no_model_is_a_label(app):
    policy({})
    assert refs.check_readable('Nowhere', 1) is False


# ── any_readable ─────────────────────────────────────────────────────────────

def test_one_readable_record_is_enough(app):
    policy({'Book.read': [{'shelf': 'a'}]})
    assert refs.any_readable([('Stamp', 1), ('Book', 2), ('Book', '1')]) is True


def test_none_readable(app):
    policy({'Book.read': [{'shelf': 'a'}]})
    assert refs.any_readable([('Stamp', 1), ('Book', 2), ('Book', 99), ('Nowhere', 1)]) is False
    assert refs.any_readable([]) is False


# ── label ────────────────────────────────────────────────────────────────────

def test_the_label_is_the_display_field(app):
    policy({'Book.read': None})
    assert refs.label('Book', '2') == 'Le città invisibili'


def test_label_permission_is_enough(app):
    policy({'Book.label': None})
    assert refs.label('Book', 1) == 'Il barone rampante'


def test_a_table_without_a_display_field_is_named_with_its_key(app):
    policy({'Stamp.read': None})
    assert refs.label('Stamp', 1) == 'Stamp 1'


def test_no_label_for_what_the_user_cannot_read(app):
    policy({'Book.read': [{'shelf': 'a'}]})
    assert refs.label('Book', 2) is None
    assert refs.label('Book', 99) is None
    assert refs.label('Stamp', 1) is None
    assert refs.label('Nowhere', 1) is None
