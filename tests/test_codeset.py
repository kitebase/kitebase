"""Tests for `CodeSet`: a list of codes in Python, a delimited string stored.

What has to hold: the generated model uses the core type; a list makes the round
trip and is stored as `",C,S,"`, an empty one as NULL; `has` finds a code with
the delimiters, so `C` never matches inside `CO`, in the querybuilder and in the
filter DSL of `db` alike; and `has` refuses a column that is not a code set.
"""
import importlib.util

import pytest
import yaml
from sqlalchemy import Column, Integer, String, create_engine, select, text
from sqlalchemy.orm import Session, configure_mappers, declarative_base

import kitebase.utils
from kitebase.app import App
from kitebase.model_base import Base
from kitebase.sqltypes import CodeSet, codeset_codes, codeset_text
from kitebase.endpoint_db import build_filters
from kitebase.plugins import PluginsManager
from kitebase.querybuilder import DynamicQueryBuilder, codeset_has
from kitebase.source import Generator


# ── The stored form ───────────────────────────────────────────────────────

def test_text_and_codes_are_each_other_inverse():
    assert codeset_text(['C', 'S']) == ',C,S,'
    assert codeset_codes(',C,S,') == ['C', 'S']
    assert codeset_text([]) is None
    assert codeset_codes(None) == []
    assert codeset_codes('') == []


def test_duplicates_go_and_the_order_stays():
    assert codeset_text(['S', 'C', 'S']) == ',S,C,'


@pytest.mark.parametrize('bad', [['C,S'], [''], [3], 'C'])
def test_a_code_with_the_delimiter_or_not_a_string_is_refused(bad):
    with pytest.raises(ValueError):
        codeset_text(bad)


# ── The column, on its own ────────────────────────────────────────────────

Local = declarative_base()


class Partner(Local):
    __tablename__ = 'partner'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    roles = Column(CodeSet(length=64))


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: type('A', (), {'query_behaviors': []})(),
                        raising=False)
    engine = create_engine('sqlite://')
    Local.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([Partner(id=1, name='Smith', roles=['C']),
                   Partner(id=2, name='Riverside', roles=['S']),
                   Partner(id=3, name='Northwind', roles=['C', 'S']),
                   Partner(id=4, name='Doe', roles=[]),
                   Partner(id=5, name='Coop', roles=['CO'])])
        s.commit()
        yield s


def test_a_list_is_stored_delimited_and_read_back_as_a_list(session):
    stored = dict(session.execute(text('select name, roles from partner')).all())
    assert stored['Northwind'] == ',C,S,'
    assert stored['Doe'] is None
    session.expire_all()
    assert session.get(Partner, 3).roles == ['C', 'S']
    assert session.get(Partner, 4).roles == []


def names(session, condition):
    return sorted(session.scalars(select(Partner.name).where(condition)))


def test_has_matches_the_code_and_never_inside_another(session):
    assert names(session, codeset_has(Partner.roles, 'C')) == ['Northwind', 'Smith']
    assert names(session, codeset_has(Partner.roles, 'CO')) == ['Coop']


def test_has_with_a_list_matches_any_of_them(session):
    assert names(session, codeset_has(Partner.roles, ['S', 'CO'])) == ['Coop', 'Northwind', 'Riverside']


def test_has_escapes_the_wildcards(session):
    assert names(session, codeset_has(Partner.roles, '_')) == []


def test_has_refuses_a_column_that_is_not_a_code_set(session):
    with pytest.raises(ValueError, match='code set'):
        codeset_has(Partner.name, 'C')


def test_has_refuses_a_code_with_the_delimiter(session):
    with pytest.raises(ValueError, match='codes'):
        codeset_has(Partner.roles, 'C,S')


def test_has_in_the_querybuilder(session):
    builder = DynamicQueryBuilder(session, {'Partner': Partner})
    rows = builder.execute_query({'table': 'Partner', 'select': ['name'],
                                  'filters': {'conditions': [{'roles': ['has', 'S']}]}},
                                 result_format='records')
    assert sorted(r['name'] for r in rows) == ['Northwind', 'Riverside']


def test_has_in_the_filter_dsl_of_db(session):
    condition = build_filters(Partner, {'roles__has': 'C'})
    assert names(session, condition) == ['Northwind', 'Smith']


def test_equality_compares_the_stored_form(session):
    assert names(session, Partner.roles == ['C', 'S']) == ['Northwind']


# ── Generated from YAML ───────────────────────────────────────────────────

@pytest.fixture
def fresh_registry():
    yield
    Base.registry.dispose()
    Base.metadata.clear()


def test_the_generated_model_uses_the_core_type(tmp_path, monkeypatch, fresh_registry):
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': {'Subject': {'columns': [
        {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
        {'name': 'roles', 'type': 'Roles'},
    ]}}, 'types': {'Roles': {'base': 'CodeSet', 'length': 255}}}))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(manager)
    manager.load_plugins()
    db = App()
    db.calc_db(manager)
    out = tmp_path / 'generated_model.py'
    Generator(db).generate(filename=str(out))
    source = out.read_text()

    assert 'roles: Mapped[list] = mapped_column(CodeSet(length=255), nullable=True)' in source
    assert 'CodeSet' in source.split('from kitebase.sqltypes import')[1].splitlines()[0]

    spec = importlib.util.spec_from_file_location('generated_model', out)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configure_mappers()
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(module.Subject(roles=['A', 'B']))
        s.commit()
        s.expire_all()
        assert s.scalars(select(module.Subject)).one().roles == ['A', 'B']
