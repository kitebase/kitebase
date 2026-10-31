"""What a select item is: a column, a function call, or any other expression.

A column is a name alone and a function call is a name followed by the only
pair of parentheses; everything else is an expression, executed as written once
check_expression has accepted every word of it. Arithmetic without parentheses
and a CASE, bare or in parentheses, are expressions too.
"""
import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    price = Column(Integer)


class AppStub:
    query_behaviors = []


@pytest.fixture(autouse=True)
def no_behaviors(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)


@pytest.fixture
def builder():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Book(id=1, title='Cheap', price=10),
                         Book(id=2, title='Dear', price=30)])
        session.commit()
        yield DynamicQueryBuilder(session, {'Book': Book})


def rows(builder, *select):
    query = {'from': 'Book', 'select': list(select), 'order_by': ['id']}
    return builder.execute_query(query, 'tuples')[1]


@pytest.mark.parametrize('expression', [
    "case when price < 20 then 'low' else 'high' end as band",
    "(case when price < 20 then 'low' else 'high' end) as band",
    "case when Book.price < 20 then 'low' else 'high' end as band",
])
def test_a_case_is_an_expression(builder, expression):
    assert rows(builder, 'id', expression) == [[1, 'low'], [2, 'high']]


def test_arithmetic_without_parentheses(builder):
    assert rows(builder, 'price * 2 as double') == [[20], [60]]


def test_an_expression_in_parentheses(builder):
    assert rows(builder, '(price + 1) as next') == [[11], [31]]


def test_columns_and_functions_keep_their_meaning(builder):
    assert rows(builder, 'Book.title', 'lower(title) as low') == [['Cheap', 'cheap'], ['Dear', 'dear']]
    query = {'from': 'Book', 'select': ['count(id) as n', 'max(price) as top']}
    assert builder.execute_query(query, 'tuples')[1] == [[2, 30]]


def test_an_expression_is_still_checked_word_by_word(builder):
    with pytest.raises(ValueError, match='not allowed'):
        rows(builder, 'case when price < 20 then sqlite_version() end as v')
