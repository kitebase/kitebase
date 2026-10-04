"""Filtering through a relationship: a path, translated by the arity of each step.

A filter may name a column of another table by walking there — `publisher.name`
from a book, `books.publisher_id` from an author. The client sends the path and
never a join, because deciding how to reach the table is exactly what it must
not know.

The direction of the step decides the translation, and the reason is the
cardinality of the result. Towards *one* nothing multiplies. Towards *many* it
does: joining an author to their books returns that author once per book, which
inflates counts, shifts pagination, and leaves "authors who never published with
Penguin" inexpressible. So a step towards many becomes an EXISTS.
"""
import pytest
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class StubTable:
    """The part of a DbTable the secret check reads."""
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
    publisher_id = Column(Integer, ForeignKey('publisher.id'))
    author_id = Column(Integer, ForeignKey('author.id'))
    publisher = relationship('Publisher', back_populates='books')
    author = relationship('Author', back_populates='books')


class Author(Base):
    __tablename__ = 'author'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    books = relationship('Book', back_populates='author')


Publisher.books = relationship('Book', back_populates='publisher')


class AppStub:
    query_behaviors = []


@pytest.fixture(autouse=True)
def no_behaviors(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)


MODELS = {'Author': Author, 'Book': Book, 'Publisher': Publisher}


def where(table, conditions) -> str:
    builder = DynamicQueryBuilder(session=None, models=MODELS)
    query = builder.build_query({
        'table': table,
        'select': ['id'],
        'filters': {'conditions': conditions},
    })
    sql = str(query.compile(compile_kwargs={"literal_binds": True}))
    return ' '.join(sql.split('WHERE', 1)[1].split()) if 'WHERE' in sql else ''


# ── Direction ──────────────────────────────────────────────────────────────

def test_step_towards_one_is_a_correlated_exists():
    """A book has one publisher: nothing multiplies, and the row count is safe."""
    clause = where('Book', [{'publisher.name': 'Penguin'}])
    assert clause == (
        "EXISTS (SELECT 1 FROM publisher WHERE publisher.id = book.publisher_id "
        "AND publisher.name = 'Penguin')"
    )


def test_step_towards_many_is_an_exists_not_a_join():
    """
    An author has many books. A join would return the author once per matching
    book; the EXISTS asks the question without bringing the rows in.
    """
    clause = where('Author', [{'books.title': ['ilike', '%rossi%']}])
    assert clause.startswith('EXISTS (SELECT 1 FROM book WHERE author.id = book.author_id')
    assert 'JOIN' not in clause


def test_the_penguin_case_nests_both_directions():
    """Authors who published with Penguin: many, then one."""
    clause = where('Author', [{'books.publisher.name': 'Penguin'}])
    assert clause.count('EXISTS') == 2
    assert "publisher.name = 'Penguin'" in clause
    assert 'JOIN' not in clause


def test_the_result_carries_no_duplicates_by_construction():
    """
    The reason the arity rule exists: nothing is added to the FROM clause, so an
    author with three matching books is still one row and DISTINCT is moot.
    """
    builder = DynamicQueryBuilder(session=None, models=MODELS)
    sql = str(builder.build_query({
        'table': 'Author',
        'select': ['id'],
        'filters': {'conditions': [{'books.publisher.name': 'Penguin'}]},
    }).compile(compile_kwargs={"literal_binds": True}))
    from_clause = sql.split('FROM', 1)[1].split('WHERE', 1)[0]
    assert from_clause.strip() == 'author'


# ── Not a path ─────────────────────────────────────────────────────────────

def test_table_qualified_columns_keep_working():
    """`Table.column` shares the dot with a path; it must stay what it was."""
    assert where('Book', [{'Book.title': 'x'}]) == "book.title = 'x'"


def test_a_bare_column_is_untouched():
    assert where('Book', [{'title': 'x'}]) == "book.title = 'x'"


def test_a_qualified_path_is_not_a_path():
    """The verbose form names its table explicitly, which settles the question."""
    clause = where('Book', {'table': 'Publisher', 'column': 'name', 'op': 'eq', 'value': 'Penguin'})
    assert clause == "publisher.name = 'Penguin'"


# ── Refusals ───────────────────────────────────────────────────────────────

def test_an_unknown_segment_names_itself_and_where_it_stood():
    """The message names the model the walk had reached, not the one it started from."""
    with pytest.raises(ValueError, match="'nope' is not a relationship of Publisher"):
        where('Book', [{'publisher.nope.name': 'x'}])


def test_an_unknown_column_at_the_end_names_itself():
    with pytest.raises(ValueError, match="'Publisher' has no column 'nope'"):
        where('Book', [{'publisher.nope': 'x'}])


def test_a_secret_column_stays_unreachable_through_a_path():
    """Being one table away is not a way around `secret`."""
    with pytest.raises(ValueError, match="not readable"):
        where('Book', [{'publisher.secret_code': 'x'}])


def test_a_name_that_is_both_a_model_and_a_relationship_is_refused():
    """
    Deciding for the caller would silently pick one of two readings; naming both
    is the only answer that cannot be wrong.
    """
    models = dict(MODELS, books=Book)   # a model registered under the relationship's name
    builder = DynamicQueryBuilder(session=None, models=models)
    with pytest.raises(ValueError, match="both a model and a relationship"):
        builder.build_query({
            'table': 'Author',
            'select': ['id'],
            'filters': {'conditions': [{'books.title': 'x'}]},
        })


# ── Operators along a path ─────────────────────────────────────────────────

@pytest.mark.parametrize('condition, expected', [
    ({'publisher.name': ['ilike', '%pen%']}, "lower(publisher.name) LIKE lower('%pen%')"),
    ({'publisher.id': ['in', [1, 2]]}, 'publisher.id IN (1, 2)'),
    ({'publisher.name': ['isnull']}, 'publisher.name IS NULL'),
    ({'publisher.id': ['between', 1, 9]}, 'publisher.id BETWEEN 1 AND 9'),
])
def test_operators_apply_at_the_far_end(condition, expected):
    assert expected in where('Book', [condition])


def test_two_conditions_on_one_collection_are_two_independent_exists():
    """
    Each condition becomes its own EXISTS, so two conditions on the same
    collection may be satisfied by *different* rows of it. Asking for a book
    whose authors include the first name of one author and the surname of
    another returns that book, though nobody by that name exists.

    Not a defect of the translation — one condition, one predicate — but the
    reason it is pinned here: whether conditions sharing a collection should
    instead refer to the same row is a decision for whoever composes them, and
    changing it must be deliberate rather than incidental.
    """
    clause = where('Author', [
        {'books.title': 'A'},
        {'books.id': 1},
    ])
    assert clause.count('EXISTS') == 2
    assert ' AND ' in clause


def test_a_path_composes_with_or_like_any_other_condition():
    clause = where('Author', [{'op': 'or', 'conditions': [
        [{'books.publisher.name': 'Penguin'}],
        [{'name': 'Rossi'}],
    ]}])
    assert 'EXISTS' in clause and "author.name = 'Rossi'" in clause
    assert ' OR ' in clause
