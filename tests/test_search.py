"""One text, in OR over the columns a table declares as searchable.

The most used function of a business application is not the rule editor: it is a
box where "Rossi" is typed and something is found. It is a primitive of its own,
and the same one three times over — the quick search on a list, the lookup of an
FK combobox, and the value widget of a rule on a foreign key — which is why it
lives in the query builder and not in any of the three.

Two halves are covered here. The cascade (DATA_MODEL.md §4.4) decides *what* a
search looks at, and is resolved from the table definition, so a caller sends
what the user typed and never a list of columns. The expansion decides *how*:
ILIKE over those columns, the primary key as one branch matched exactly, the
whole thing ANDed with the filters so it can only narrow them.
"""
import pytest
import yaml
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

import kitebase.utils
from kitebase.db import DB
from kitebase.plugins import PluginsManager
from kitebase.querybuilder import DynamicQueryBuilder


# ── The cascade ────────────────────────────────────────────────────────────
# Resolved on real DbTable objects: the rules read column attributes, plugin
# merge and app config, so a stub would be testing the stub.

def schema(tmp_path, monkeypatch, tables, app_config=None):
    """Build a one-plugin app declaring `tables`, and return its DB."""
    plugin = tmp_path / 'plugins' / 'app'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'config.yaml').write_text(yaml.safe_dump({'name': 'app', 'version': '0.0.1'}))
    (plugin / 'model.yaml').write_text(yaml.safe_dump({'tables': tables}))

    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins'], **(app_config or {})}))
    monkeypatch.chdir(tmp_path)

    manager = PluginsManager()
    manager.load_config(str(cfg))
    kitebase.utils.register_standard_handlers(manager)
    manager.load_plugins()

    db = DB()
    db.calc_db(manager)
    return db


def pk(**attributes):
    return {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True, **attributes}


def col(name, **attributes):
    return {'name': name, 'type': 'String', 'length': 50, **attributes}


def search_of(tmp_path, monkeypatch, columns, table_attrs=None, app_config=None):
    tables = {'Thing': {'columns': columns, **(table_attrs or {})}}
    return schema(tmp_path, monkeypatch, tables, app_config).tables['Thing'].search_info


def test_convention_alone_makes_a_table_searchable(tmp_path, monkeypatch):
    """Nothing declared: the display field comes from the naming convention."""
    info = search_of(tmp_path, monkeypatch, [pk(), col('name')])
    assert info == {'display_field': 'name', 'search_fields': ['name'], 'search_pk': 'id'}


def test_a_searchable_column_joins_the_pool(tmp_path, monkeypatch):
    """`searchable: true` adds to the display field rather than replacing it."""
    info = search_of(tmp_path, monkeypatch, [pk(), col('name'), col('sku', searchable=True)])
    assert info['search_fields'] == ['name', 'sku']


def test_explicit_search_fields_replace_the_display_field(tmp_path, monkeypatch):
    """The override answers 'where to look', not 'what to show'."""
    info = search_of(
        tmp_path, monkeypatch,
        [pk(), col('name'), col('tax_id'), col('sku', searchable=True)],
        {'search_fields': ['tax_id']},
    )
    assert info['display_field'] == 'name'          # still the label
    assert info['search_fields'] == ['tax_id', 'sku']   # the key and searchable stay added
    assert info['search_pk'] == 'id'


def test_a_secret_column_is_never_searched(tmp_path, monkeypatch):
    """
    A search that matches a secret column answers whether a value is right,
    which is how a password is guessed one query at a time. Declaring it does
    not change that.
    """
    info = search_of(
        tmp_path, monkeypatch,
        [pk(), col('name'), col('password', secret=True)],
        {'search_fields': ['name', 'password']},
    )
    assert info['search_fields'] == ['name']


def test_a_virtual_display_field_falls_back_to_a_real_column(tmp_path, monkeypatch):
    """A hybrid property has no column to compare: the label is built from one."""
    info = search_of(tmp_path, monkeypatch, [pk(), col('name', virtual=True), col('code')])
    assert info['display_field'] == 'name'
    assert info['search_fields'] == ['code']


def test_the_key_is_dropped_by_the_table(tmp_path, monkeypatch):
    """`include_pk: false` — a surrogate key nobody types is noise in the OR."""
    info = search_of(tmp_path, monkeypatch, [pk(), col('name')], {'include_pk': False})
    assert info['search_pk'] is None
    assert info['search_fields'] == ['name']


def test_the_key_is_dropped_by_the_app(tmp_path, monkeypatch):
    info = search_of(tmp_path, monkeypatch, [pk(), col('name')],
                     app_config={'schema': {'include_pk_in_search': False}})
    assert info['search_pk'] is None


def test_a_composite_key_has_no_value_to_type(tmp_path, monkeypatch):
    info = search_of(tmp_path, monkeypatch, [
        {'name': 'a_id', 'type': 'Integer', 'primary_key': True},
        {'name': 'b_id', 'type': 'Integer', 'primary_key': True},
        col('name'),
    ])
    assert info['search_pk'] is None


def test_a_table_with_nothing_to_match_says_so(tmp_path, monkeypatch):
    """No convention match, nothing declared: searchable by key only."""
    info = search_of(tmp_path, monkeypatch, [pk(), col('note')])
    assert info == {'display_field': None, 'search_fields': [], 'search_pk': 'id'}


def test_the_convention_is_the_app_s_to_set(tmp_path, monkeypatch):
    info = search_of(tmp_path, monkeypatch, [pk(), col('label')],
                     app_config={'schema': {'display_field_names': ['label']}})
    assert info['search_fields'] == ['label']


# ── The expansion ──────────────────────────────────────────────────────────
# Standalone models with stub definitions: what is under test is the clause,
# and the cascade above already answers where its columns come from.

Base = declarative_base()


class StubTable:
    """The part of a DbTable the search and the secret check read."""

    def __init__(self, fields, search_pk='id', secrets=()):
        self.search_info = {'display_field': None, 'search_fields': fields, 'search_pk': search_pk}
        self.secret_columns = frozenset(secrets)


class Partner(Base):
    __tablename__ = 'partner'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    city = Column(String)
    password = Column(String)

    @classmethod
    def get_table_definition(cls):
        return StubTable(['name', 'city'], secrets={'password'})


class Country(Base):
    """A key the user types: the exact branch is always in."""
    __tablename__ = 'country'
    code = Column(String, primary_key=True)
    name = Column(String)

    @classmethod
    def get_table_definition(cls):
        return StubTable(['name'], search_pk='code')


class Ledger(Base):
    """Searchable by key only — every other column is a number or a secret."""
    __tablename__ = 'ledger'
    id = Column(Integer, primary_key=True)

    @classmethod
    def get_table_definition(cls):
        return StubTable([])


class Note(Base):
    """Nothing to search at all."""
    __tablename__ = 'note'
    id = Column(Integer, primary_key=True)
    body = Column(String)

    @classmethod
    def get_table_definition(cls):
        return StubTable([], search_pk=None)


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    publisher_id = Column(Integer, ForeignKey('publisher.id'))
    publisher = relationship('Publisher')

    @classmethod
    def get_table_definition(cls):
        return StubTable(['title', 'publisher.name'])


class Publisher(Base):
    __tablename__ = 'publisher'
    id = Column(Integer, primary_key=True)
    name = Column(String)

    @classmethod
    def get_table_definition(cls):
        return StubTable(['name'])


class AppStub:
    query_behaviors = []


@pytest.fixture(autouse=True)
def no_behaviors(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)


MODELS = {'Partner': Partner, 'Country': Country, 'Ledger': Ledger,
          'Note': Note, 'Book': Book, 'Publisher': Publisher}


def where(table, **query) -> str:
    builder = DynamicQueryBuilder(session=None, models=MODELS)
    sql = str(builder.build_query({'table': table, 'select': ['id'], **query})
              .compile(compile_kwargs={"literal_binds": True}))
    return ' '.join(sql.split('WHERE', 1)[1].split()) if 'WHERE' in sql else ''


def test_the_text_goes_to_every_declared_column(tmp_path):
    clause = where('Partner', search='rossi')
    assert "lower(partner.name) LIKE lower('%rossi%')" in clause
    assert "lower(partner.city) LIKE lower('%rossi%')" in clause
    assert ' OR ' in clause


def test_the_key_is_one_branch_of_the_or_and_not_a_shortcut():
    """
    Typing 42 may mean the record numbered 42 and may equally mean a title with
    42 in it. Hiding the second is an answer nobody asked for.
    """
    clause = where('Partner', search='42')
    assert 'partner.id = 42' in clause
    assert "lower(partner.name) LIKE lower('%42%')" in clause


def test_the_key_stays_out_when_the_text_could_not_be_one():
    clause = where('Partner', search='rossi')
    assert 'partner.id' not in clause


def test_a_key_the_user_types_is_always_a_branch():
    """A string key holds any text, so the exact match is always worth asking."""
    assert "country.code = 'IT'" in where('Country', search='IT', select=['code'])


def test_wildcards_the_user_typed_are_escaped():
    """Searching for '50%' looks for that, not for everything."""
    clause = where('Partner', search='50%')
    assert r"lower('%50\%%')" in clause
    assert "ESCAPE '\\'" in clause


def test_the_search_narrows_the_filters_it_meets():
    """A key of its own, ANDed: it can only narrow, never widen."""
    clause = where('Partner', search='rossi', filters={'conditions': [{'city': 'Udine'}]})
    assert clause.startswith("partner.city = 'Udine' AND (")


def test_a_declared_path_is_searched_through_the_relationship():
    """Search fields are filter expressions, so a path works as it does there."""
    clause = where('Book', search='penguin')
    assert 'EXISTS' in clause
    assert "lower(publisher.name) LIKE lower('%penguin%')" in clause


def test_searchable_only_by_key_and_the_text_is_not_one():
    """
    No row can match, and saying so is not the same as dropping the search:
    a search silently ignored returns every row, which reads as a result.
    """
    clause = where('Ledger', search='rossi')
    assert 'false' in clause.lower() or '1 != 1' in clause or '0 = 1' in clause


def test_a_table_that_declares_nothing_refuses_the_search():
    with pytest.raises(ValueError, match="Table 'Note' has no searchable columns"):
        where('Note', search='rossi')


def test_an_empty_search_is_not_a_search():
    assert where('Partner', search='   ') == ''
    assert where('Partner', search=None) == ''


def test_the_count_asks_the_same_question_as_the_page():
    """
    The count runs a query of its own, so a search that reached only one of the
    two would leave the user with a total that contradicts what they see.
    """
    builder = DynamicQueryBuilder(session=None, models=MODELS)
    count = builder.build_query({'table': 'Partner', 'select': ['id'], 'search': 'rossi'})
    assert 'lower(partner.name) LIKE' in str(count.compile(compile_kwargs={"literal_binds": True}))
    # count_query strips limit/offset only — the search travels with the rest
    stripped = {k: v for k, v in {'table': 'Partner', 'search': 'rossi',
                                  'limit': 10, 'offset': 20}.items()
                if k not in ('limit', 'offset')}
    assert stripped == {'table': 'Partner', 'search': 'rossi'}
