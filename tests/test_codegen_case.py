"""Tests for `case:` — the column attribute that fixes the case of a string.

Normalising in the column type rather than in the setter is what makes the
guarantee hold: the tests below check that it survives the round trip to the
database, that it also applies to the parameters of a query (which is what lets
a lower-case search find an upper-case value), and that a column without the
attribute is left exactly as it was.
"""
import importlib.util

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, configure_mappers

import kitebase.utils
from kitebase.db import DB, Base
from kitebase.plugins import PluginsManager
from kitebase.source import Generator


@pytest.fixture(autouse=True)
def fresh_registry():
    """Each test maps its own classes onto the shared declarative Base."""
    yield
    Base.registry.dispose()
    Base.metadata.clear()


def generate(tmp_path, monkeypatch, tables, types=None):
    """Write a one-plugin app declaring `tables`, and return the generated source."""
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    model = {'tables': tables}
    if types:
        model['types'] = types
    (plugin / 'model.yaml').write_text(yaml.safe_dump(model))

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(manager)
    manager.load_plugins()

    db = DB()
    db.calc_db(manager)

    out = tmp_path / 'generated_model.py'
    Generator(db).generate(filename=str(out))
    return out.read_text()


def load(tmp_path, source):
    """Import the generated source and let SQLAlchemy configure the mappers."""
    path = tmp_path / 'generated_model.py'
    path.write_text(source)
    spec = importlib.util.spec_from_file_location('generated_model', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configure_mappers()
    return module


def pk():
    return {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True}


def session_for(module):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    return Session(engine), module.Subject


SUBJECT = {
    'Subject': {
        'columns': [
            pk(),
            {'name': 'tax_code', 'type': 'String', 'length': 16, 'case': 'upper'},
            {'name': 'slug', 'type': 'String', 'length': 16, 'case': 'lower'},
            {'name': 'name', 'type': 'String', 'length': 32},
        ]
    }
}


def test_generates_the_normalising_type_only_where_asked(tmp_path, monkeypatch):
    source = generate(tmp_path, monkeypatch, SUBJECT)

    assert "CaseString(length=16, case='upper')" in source
    assert "CaseString(length=16, case='lower')" in source
    # No attribute means the plain type, not CaseString with a neutral setting.
    assert "name: Mapped[str] = mapped_column(String(length=32), nullable=True)" in source
    assert 'from kitebase.db import Base, BaseApp, CaseString' in source


def test_value_is_stored_normalised(tmp_path, monkeypatch):
    module = load(tmp_path, generate(tmp_path, monkeypatch, SUBJECT))
    session, Subject = session_for(module)

    session.add(Subject(tax_code='rssmra80a01h501u', slug='Rossi-Mario', name='Rossi, Mario'))
    session.commit()

    row = session.scalars(select(Subject)).one()
    assert row.tax_code == 'RSSMRA80A01H501U'
    assert row.slug == 'rossi-mario'
    assert row.name == 'Rossi, Mario'


def test_query_parameters_are_normalised_too(tmp_path, monkeypatch):
    """The point of normalising in the type: the search does not have to know."""
    module = load(tmp_path, generate(tmp_path, monkeypatch, SUBJECT))
    session, Subject = session_for(module)

    session.add(Subject(tax_code='RSSMRA80A01H501U'))
    session.commit()

    found = session.scalars(
        select(Subject).where(Subject.tax_code == 'rssmra80a01h501u')).one_or_none()
    assert found is not None


def test_case_can_be_declared_on_a_type(tmp_path, monkeypatch):
    """Types are inherited by the columns that use them, `case` included."""
    source = generate(
        tmp_path, monkeypatch,
        {'Subject': {'columns': [pk(), {'name': 'code', 'type': 'UpperCode'}]}},
        types={'UpperCode': {'base': 'String', 'length': 8, 'case': 'upper'}},
    )
    assert "code: Mapped[str] = mapped_column(CaseString(length=8, case='upper'), nullable=True)" in source


def test_an_unknown_case_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='upper, lower or neutral'):
        generate(tmp_path, monkeypatch,
                 {'Subject': {'columns': [pk(), {'name': 'code', 'type': 'String',
                                                 'case': 'title'}]}})


def test_case_on_a_non_string_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='applies to strings'):
        generate(tmp_path, monkeypatch,
                 {'Subject': {'columns': [pk(), {'name': 'n', 'type': 'Integer',
                                                 'case': 'upper'}]}})
