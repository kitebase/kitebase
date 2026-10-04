"""Tests for the `resolve` flag in kitebase.querybuilder.DynamicQueryBuilder.

Registered query behaviors (Archivable and friends) filter every query built by
the builder, including a lookup by primary key. That is right for a picklist
and wrong for reading back a value that is already stored: a document saved
before its partner was archived must still render the partner's name, even
though the partner may no longer be selected.

`resolve: true` marks the second case — an absolute lookup — and skips the
behaviors entirely. Covered here: behaviors apply by default, `resolve` skips
them, and `resolve` leaves the caller's own filters untouched.
"""
import pytest
from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.orm import declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Partner(Base):
    __tablename__ = 'partner'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    active = Column(Boolean, default=True)


class ArchivableStub:
    """Minimal stand-in for the Archivable query behavior."""

    @classmethod
    def applies_to(cls, model_class) -> bool:
        return hasattr(model_class, 'active')

    @classmethod
    def apply(cls, model_class, query_def, query):
        if query_def.get('include_archived'):
            return query
        return query.where(model_class.active == True)  # noqa: E712


class AppStub:
    def __init__(self, behaviors):
        self.query_behaviors = behaviors


@pytest.fixture
def builder(monkeypatch):
    """DynamicQueryBuilder with ArchivableStub registered, no session or database."""
    monkeypatch.setattr(kitebase.utils, 'get_app',
                        lambda: AppStub([ArchivableStub]), raising=False)
    return DynamicQueryBuilder(session=None, models={'Partner': Partner})


def sql(builder, query_def) -> str:
    """Compile a query definition to a literal-bound SQL string."""
    query = builder.build_query(query_def)
    return str(query.compile(compile_kwargs={"literal_binds": True}))


LOOKUP = {
    'table': 'Partner',
    'select': ['id', 'name'],
    'filters': {'conditions': {'column': 'id', 'op': 'eq', 'value': 7}},
}


def test_behaviors_apply_by_default(builder):
    """Without the flag, a lookup by primary key is still filtered."""
    assert 'partner.active' in sql(builder, LOOKUP)


def test_resolve_skips_behaviors(builder):
    """With resolve, the archived record is reachable by primary key."""
    assert 'partner.active' not in sql(builder, {**LOOKUP, 'resolve': True})


def test_resolve_keeps_explicit_filters(builder):
    """resolve drops the implicit filter only — the caller's own filter stays."""
    assert 'partner.id = 7' in sql(builder, {**LOOKUP, 'resolve': True})


def test_resolve_false_is_not_a_bypass(builder):
    """An explicit false must behave like an absent flag, not like a bypass."""
    assert 'partner.active' in sql(builder, {**LOOKUP, 'resolve': False})
