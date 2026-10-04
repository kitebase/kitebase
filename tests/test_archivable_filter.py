"""A query behavior steps aside when the caller filters on its own column.

`Archivable` hides archived rows from every query, and its contract has a way
out: a caller that filters on `active` explicitly knows what it wants, and the
behavior must not AND its own `active = True` on top — that answered a rule
"active is false" with zero rows. The way out used to look for `active` among
the keys of `filters`, a flat shape the query builder refuses anyway, so it
never fired. The question "does this filter mention that column" belongs to the
query builder, which owns the syntax: concise keys, the `Table.column` form,
verbose dicts, nested and/or groups. The mixin asks; it does not parse.

The behavior under test is the real one, imported from the copy `devtest`
carries, against an in-memory model: a stub would be testing the stub.
"""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder, filters_mention

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON_MODEL = REPO_ROOT / 'devtest' / 'commons' / 'common' / 'model.py'

spec = importlib.util.spec_from_file_location('common_model', COMMON_MODEL)
common_model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common_model)
Archivable = common_model.Archivable

Base = declarative_base()


class Book(Base, Archivable):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    active = Column(Boolean, nullable=False, default=True)


class AppStub:
    query_behaviors = [Archivable]


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([Book(id=1, title='Dune', active=True),
                   Book(id=2, title='Emma', active=False)])
        s.commit()
        yield s


def titles(session, query_def):
    builder = DynamicQueryBuilder(session, {'Book': Book})
    rows = builder.execute_query({'table': 'Book', 'select': ['title'], **query_def},
                                 result_format='records')
    return sorted(r['title'] for r in rows)


# ── The behavior ───────────────────────────────────────────────────────────

def test_nothing_said_hides_the_archived(session):
    assert titles(session, {}) == ['Dune']


def test_include_archived_shows_everything(session):
    assert titles(session, {'include_archived': True}) == ['Dune', 'Emma']


def test_archived_picks_the_view(session):
    """What the `archivable` command sets: a word the view carries unread."""
    assert titles(session, {'archived': 'all'}) == ['Dune', 'Emma']
    assert titles(session, {'archived': 'only'}) == ['Emma']
    assert titles(session, {'archived': None}) == ['Dune']


def test_a_rule_on_the_column_is_obeyed_as_written(session):
    """What the rule editor sends: the concise form under `conditions`."""
    assert titles(session, {'filters': {'conditions': [{'active': False}]}}) == ['Emma']
    assert titles(session, {'filters': {'conditions': [{'active': True}]}}) == ['Dune']


def test_the_rule_is_found_inside_a_group(session):
    """The rule editor nests its blocks; a mention two levels down still counts."""
    query = {'filters': {'conditions': [
        [{'title': ['ilike', '%m%']}],
        {'op': 'or', 'conditions': [{'active': False}, {'title': 'Nobody'}]},
    ]}}
    assert titles(session, {'filters': {'conditions': [[{'active': False}]]}}) == ['Emma']
    assert titles(session, query) == ['Emma']


def test_a_rule_on_another_column_keeps_the_default(session):
    assert titles(session, {'filters': {'conditions': [{'title': ['ilike', '%m%']}]}}) == []


# ── The question, on its own ───────────────────────────────────────────────

@pytest.mark.parametrize('filters', [
    {'conditions': [{'active': False}]},
    {'conditions': [{'Book.active': False}]},
    {'conditions': {'column': 'active', 'op': 'eq', 'value': False}},
    {'conditions': [{'table': 'Book', 'column': 'active', 'op': 'eq', 'value': False}]},
    {'conditions': [{'op': 'or', 'conditions': [{'title': 'x'}, {'active': False}]}]},
])
def test_every_shape_of_a_mention_is_seen(filters):
    assert filters_mention(filters, 'Book', 'active')


@pytest.mark.parametrize('filters', [
    None,
    {},
    {'conditions': []},
    {'conditions': [{'title': 'x'}]},
    {'conditions': [{'Author.active': False}]},
    {'conditions': [{'table': 'Author', 'column': 'active', 'op': 'eq', 'value': False}]},
    {'conditions': [{'author.active': False}]},
])
def test_another_table_s_column_is_not_a_mention(filters):
    """`Author.active` is a filter on the joined table: the book's own default stays."""
    assert not filters_mention(filters, 'Book', 'active')
