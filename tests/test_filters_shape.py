"""A filter block whose conditions cannot be found is refused, not ignored.

`apply_filters` used to return the query untouched when `filters` had no
`conditions` key. The caller then got **every row** — and could not tell that
apart from a query that legitimately matched everything, because nothing was
said. It is the same family as the `is not None` guard already covered in
test_build_filters.py: there the shape was right and one condition was dropped,
here the shape is wrong and all of them are.

The mistake is easy to make because kitebase has two filter dialects: the
endpoint `db` takes flat filters under `query` ({'field': value}), the endpoint
`query` takes them under `filters.conditions`. Writing one where the other
belongs used to answer with the whole table.
"""
import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)


class AppStub:
    query_behaviors: list = []


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)
    return DynamicQueryBuilder(session=None, models={'Book': Book})


def sql(builder, query_def) -> str:
    return str(builder.build_query(query_def).compile(
        compile_kwargs={"literal_binds": True}))


BASE = {'table': 'Book', 'select': ['id', 'title']}


def test_the_flat_dialect_is_refused_instead_of_ignored(builder):
    """What someone writes when they remember the other endpoint's syntax."""
    with pytest.raises(ValueError) as e:
        builder.build_query({**BASE, 'filters': {'title': 'Dune'}})

    # The message has to carry the way out, not just the complaint.
    assert 'conditions' in str(e.value)
    assert 'title' in str(e.value)


def test_an_empty_block_stays_silent(builder):
    """Saying nothing means nothing, and is not an error."""
    assert 'WHERE' not in sql(builder, {**BASE, 'filters': {}})


def test_the_documented_shape_still_filters(builder):
    out = sql(builder, {**BASE,
                        'filters': {'conditions': {'column': 'title',
                                                   'op': 'eq', 'value': 'Dune'}}})
    assert 'WHERE' in out and 'Dune' in out


def test_a_list_where_an_object_belongs_is_refused(builder):
    with pytest.raises(ValueError):
        builder.build_query({**BASE, 'filters': [{'title': 'Dune'}]})


def test_having_is_held_to_the_same_rule(builder):
    with pytest.raises(ValueError) as e:
        builder.build_query({**BASE, 'group_by': ['title'],
                             'having': {'count': 3}})
    assert 'having' in str(e.value)
