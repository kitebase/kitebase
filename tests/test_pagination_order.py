"""A page is a slice of an order, and an order with ties is not one.

Ordering by a column with repeated values leaves the rows that share a value
undecided between themselves, and the database may settle them differently on
each execution. Pagination is one execution per page: the row that came last on
page one can come first on page two, so the reader sees it twice and never sees
the one it displaced — with no error and nothing to signal it.

Ending the ordering with the primary key makes it total: no two rows are tied,
every page is a slice of the same sequence, and rows that look identical always
come back in the same order.
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
    price = Column(Integer)


class Loan(Base):
    """Composite key: the tiebreaker is both columns, and still total."""
    __tablename__ = 'loan'
    book_id = Column(Integer, primary_key=True)
    member_id = Column(Integer, primary_key=True)
    day = Column(String)


class AppStub:
    query_behaviors = []


@pytest.fixture(autouse=True)
def no_behaviors(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)


MODELS = {'Book': Book, 'Loan': Loan}


def order_clause(sql: str) -> str:
    """The ORDER BY of a compiled query, without the pagination that follows it."""
    if 'ORDER BY' not in sql:
        return ''
    tail = sql.split('ORDER BY', 1)[1]
    for keyword in (' LIMIT', ' OFFSET'):
        tail = tail.split(keyword, 1)[0]
    return ' '.join(tail.split())


def order_of(table='Book', select=('id',), **query) -> str:
    builder = DynamicQueryBuilder(session=None, models=MODELS)
    sql = str(builder.build_query({'table': table, 'select': list(select), **query})
              .compile(compile_kwargs={"literal_binds": True}))
    return order_clause(sql)


def test_the_key_ends_the_ordering():
    assert order_of(order_by=['title'], limit=10) == 'book.title ASC, book.id ASC'


def test_it_follows_the_direction_of_nothing_but_itself():
    """Descending by title still ascends by key: the key breaks ties, it does
    not take part in the sort the user asked for."""
    assert order_of(order_by=[['title', 'desc']], limit=10) == 'book.title DESC, book.id ASC'


def test_a_key_already_named_is_not_named_twice():
    assert order_of(order_by=['title', 'id'], limit=10) == 'book.title ASC, book.id ASC'


def test_the_verbose_form_counts_as_naming_it():
    clause = order_of(order_by=[{'column': 'id', 'direction': 'desc'}], limit=10)
    assert clause == 'book.id DESC'


def test_a_paginated_query_without_an_order_gets_one():
    """
    Without ORDER BY a page is even less defined than with a tied one: nothing
    at all promises the same rows twice.
    """
    assert order_of(limit=10) == 'book.id ASC'
    assert order_of(offset=10) == 'book.id ASC'


def test_an_unpaginated_query_is_left_alone():
    """No pages, no boundary to fall inside: the query says what it says."""
    assert order_of(order_by=['title']) == 'book.title ASC'
    assert order_of() == ''


def test_a_composite_key_contributes_all_of_its_columns():
    clause = order_of(table='Loan', select=['day'], order_by=['day'], limit=5)
    assert clause == 'loan.day ASC, loan.book_id ASC, loan.member_id ASC'


def test_a_grouped_query_keeps_out_of_it():
    """
    A group is not a row and has no key: naming one would be a column outside
    the grouping, which is an error rather than a tiebreaker.
    """
    clause = order_of(select=['price', 'count(id) as n'], group_by=['price'],
                      order_by=['price'], limit=10)
    assert clause == 'book.price ASC'


def test_an_aggregate_without_grouping_keeps_out_of_it():
    assert order_of(select=['count(id) as n'], limit=1) == ''
