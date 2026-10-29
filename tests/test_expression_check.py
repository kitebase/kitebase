"""What an expression in select, group_by and order_by may say.

An expression with operators or parentheses reaches the database as SQL text,
and inside text the checks on the statement see nothing: a subquery there
would read any table and any column, secret or forbidden. So every word of an
expression must be a column, a function or keyword of a closed list, or an
alias of the select. These tests run real queries: the expressions people
write keep working, and the ones that try to leave the columns are refused.
"""
import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Session, declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class StubTable:
    secret_columns = frozenset({'secret_code'})


class Publisher(Base):
    __tablename__ = 'publisher'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    secret_code = Column(String)

    @classmethod
    def get_table_definition(cls):
        return StubTable()


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    pages = Column(Integer)
    publisher_id = Column(Integer, ForeignKey('publisher.id'))

    @hybrid_property
    def double_pages(self):         # a virtual column
        return self.pages * 2


MODELS = {'Book': Book, 'Publisher': Publisher}
JOIN = [{'Publisher': 'Publisher.id = Book.publisher_id'}]


class AppStub:
    query_behaviors = []


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Publisher(id=1, name='Einaudi', secret_code='S3GRETO'))
    session.add(Book(id=1, title='Palomar', pages=128, publisher_id=1))
    session.add(Book(id=2, title='Marcovaldo', pages=160, publisher_id=1))
    session.commit()
    builder = DynamicQueryBuilder(session, MODELS)
    yield lambda query: builder.execute_query({'table': 'Book', **query})
    session.close()


# ── What people write keeps working ──────────────────────────────────────────

def test_the_case_of_the_devtest_book_list(run):
    rows = run({'select': ["CASE WHEN COUNT(DISTINCT Book.id) > 1 THEN 'Various' "
                           "ELSE MIN(Book.title) END as titles"],
                'joins': JOIN, 'group_by': ['Publisher.id']})
    assert rows == [('Various',)]


def test_arithmetic_and_an_alias_ordered_by(run):
    rows = run({'select': ['Book.title', '(Book.pages * 2) + 1 as doubled'],
                'order_by': [['doubled', 'desc']]})
    assert rows == [('Marcovaldo', 321), ('Palomar', 257)]


def test_a_column_of_the_main_table_needs_no_prefix(run):
    assert run({'select': ['sum(pages) as total']}) == [(288,)]


def test_a_grouped_aggregate_ordered_by_its_alias(run):
    rows = run({'select': ['Publisher.name', 'count(Book.id) as n'], 'joins': JOIN,
                'group_by': ['Publisher.name'], 'order_by': ['n']})
    assert rows == [('Einaudi', 2)]


# ── What tries to leave the columns is refused ───────────────────────────────

@pytest.mark.parametrize('expr', [
    "Book.title || (select secret_code from publisher)",
    "Book.title || (select group_concat(name) from sqlite_master)",
    "Book.pages + 1; drop table book",
    "Book.pages + 1 -- and the rest",
    "Book.pages /* hidden */ + 1",
    'Book.pages + "secret_code"',
    "pg_sleep(10) + Book.pages",
])
def test_a_word_outside_the_lists_is_refused(run, expr):
    with pytest.raises(ValueError, match='Expression'):
        run({'select': [f'{expr} as x']})


def test_a_secret_column_inside_an_expression_is_refused(run):
    with pytest.raises(ValueError, match='not readable'):
        run({'select': ["Book.title || Publisher.secret_code as x"], 'joins': JOIN})


def test_a_bare_name_must_be_a_column_of_the_main_table(run):
    """Bare, `secret_code` would resolve to the joined publisher's."""
    with pytest.raises(ValueError, match="'secret_code' is not allowed"):
        run({'select': ["Book.title || secret_code as x"], 'joins': JOIN})


def test_group_by_is_checked(run):
    with pytest.raises(ValueError, match='Expression'):
        run({'select': ['count(Book.id) as n'],
             'group_by': ['(select secret_code from publisher)']})


def test_order_by_is_checked(run):
    with pytest.raises(ValueError, match='Expression'):
        run({'order_by': ['(select secret_code from publisher)']})


def test_ordering_by_a_secret_column_is_refused(run):
    """An order is an oracle: sorted rows tell the secret apart."""
    with pytest.raises(ValueError, match='not readable'):
        run({'joins': JOIN, 'order_by': ['Publisher.secret_code']})
    with pytest.raises(ValueError, match='not readable'):
        run({'table': 'Publisher', 'order_by': ['secret_code']})


@pytest.mark.parametrize('name', ['double_pages', 'Book.double_pages'])
def test_a_virtual_column_is_a_column(run, name):
    assert run({'select': ['title', name], 'order_by': ['id']}) == [
        ('Palomar', 256), ('Marcovaldo', 320)]


@pytest.mark.parametrize('expr', ['Book.metadata', 'Book.registry', 'metadata'])
def test_an_attribute_that_is_not_a_column_is_refused(run, expr):
    with pytest.raises(ValueError):
        run({'select': [f'{expr} as x']})
