"""Tests for kitebase.endpoint_db.build_filters — the `db` endpoint filter DSL.

This path had zero coverage, which let a real bug survive: handle_get used
`if filter_conditions:` on a SQLAlchemy clause (bool() raises), crashing every
filtered list request. Covered here: the clause has no boolean value
(regression), implicit AND of siblings, `$or`, and `$and` nesting
— (A OR B) AND (C OR D), which the flat implicit-AND form cannot express.

Uses a standalone SQLAlchemy model (no kitebase model machinery needed): filters
are compiled to literal SQL and asserted on structure.
"""
import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

from kitebase.endpoint_db import build_filters

Base = declarative_base()


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    isbn = Column(String)
    pages = Column(Integer)


def sql(clause) -> str:
    """Compile a clause to a literal-bound SQL string for structural asserts."""
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def test_empty_filters_return_none():
    assert build_filters(Book, {}) is None
    assert build_filters(Book, None) is None


def test_unknown_field_skipped():
    # No matching column → no conditions → None (not a crash)
    assert build_filters(Book, {'ghost': 'x'}) is None


def test_guard_must_be_is_not_none_regression():
    """Regression for the handle_get bug: the guard MUST be `is not None`,
    never `if clause:`. The buggy form failed two different ways:

      * single condition → and_(one) returns a BinaryExpression whose bool()
        is False → the filter is silently DROPPED (all rows returned);
      * multiple / $or → a BooleanClauseList whose bool() raises TypeError
        → the request CRASHES with a 500.

    Both clauses are non-None and must be applied.
    """
    single = build_filters(Book, {'title': 'x'})
    assert single is not None
    assert bool(single) is False                 # silent-drop mode

    multi = build_filters(Book, {'title': 'a', 'pages__gt': 1})
    assert multi is not None
    with pytest.raises(TypeError):               # crash mode
        bool(multi)


def test_eq_and_operators():
    assert "book.title = 'x'" in sql(build_filters(Book, {'title': 'x'}))
    assert 'book.pages >= 100' in sql(build_filters(Book, {'pages__gte': 100}))
    assert 'book.pages != 5' in sql(build_filters(Book, {'pages__neq': 5}))
    assert "book.isbn LIKE '978%'" in sql(build_filters(Book, {'isbn__like': '978%'}))
    assert 'book.pages IN (1, 2, 3)' in sql(build_filters(Book, {'pages__in': [1, 2, 3]}))
    assert 'book.pages BETWEEN 10 AND 20' in sql(build_filters(Book, {'pages__between': [10, 20]}))


def test_implicit_and_of_siblings():
    s = sql(build_filters(Book, {'title': 'a', 'pages__gt': 1}))
    assert "book.title = 'a'" in s
    assert 'book.pages > 1' in s
    assert ' AND ' in s


def test_or_group():
    s = sql(build_filters(Book, {'$or': [{'title': 'a'}, {'isbn': 'b'}]}))
    assert "book.title = 'a' OR book.isbn = 'b'" in s


def test_or_combines_with_implicit_and():
    # id >= 1 AND (title = 'a' OR title = 'b')
    s = sql(build_filters(Book, {
        'pages__gte': 1,
        '$or': [{'title': 'a'}, {'title': 'b'}],
    }))
    assert ' AND ' in s
    assert s.count(' OR ') == 1
    assert 'book.pages >= 1' in s


def test_and_enables_nesting():
    """The headline case: (A OR B) AND (C OR D) — impossible without $and,
    since a dict can hold at most one $or key."""
    s = sql(build_filters(Book, {'$and': [
        {'$or': [{'title': 'a'}, {'title': 'b'}]},
        {'$or': [{'isbn': 'c'}, {'isbn': 'd'}]},
    ]}))
    # Two OR groups, AND-ed together, all four literals present
    assert s.count(' OR ') == 2
    assert ' AND ' in s
    for lit in ("'a'", "'b'", "'c'", "'d'"):
        assert lit in s


def test_and_folds_into_surrounding_and():
    # pages > 1 AND (title = 'a' OR title = 'b')
    s = sql(build_filters(Book, {
        'pages__gt': 1,
        '$and': [{'$or': [{'title': 'a'}, {'title': 'b'}]}],
    }))
    assert 'book.pages > 1' in s
    assert s.count(' OR ') == 1
    assert ' AND ' in s
