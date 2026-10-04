"""Contract test for the payloads the client's rule editor emits.

`dataview.rules.ts` turns the user-built WHERE clause into querybuilder
conditions. The TypeScript side has its own tests, but they only prove the
module emits what it means to emit — whether the querybuilder *accepts* it is a
question that can only be answered here, and it is the one that matters: a
payload the server misreads produces a wrong set silently, never an error the
user can see.

Every payload below is copied verbatim from the expectations in
`dataview.rules.test.ts`. The two files must be changed together.
"""
import pytest
from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.orm import declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    title = Column(String)
    code = Column(String)
    year = Column(Integer)
    price = Column(Integer)
    note = Column(String)
    active = Column(Boolean)
    province = Column(String)
    method_id = Column(Integer)
    amount = Column(Integer)


class AppStub:
    query_behaviors = []


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)
    return DynamicQueryBuilder(session=None, models={'Book': Book})


def where(builder, conditions) -> str:
    """Compile a rules group into the WHERE clause it produces."""
    query = builder.build_query({
        'table': 'Book',
        'select': ['id'],
        'filters': {'conditions': conditions},
    })
    sql = str(query.compile(compile_kwargs={"literal_binds": True}))
    return sql.split('WHERE', 1)[1].strip() if 'WHERE' in sql else ''


# ── One condition per operator ─────────────────────────────────────────────

def test_equality_short_form(builder):
    assert where(builder, [{'title': 'x'}]) == "book.title = 'x'"


def test_contains_and_startswith(builder):
    assert where(builder, [{'title': ['ilike', '%ros%']}]) == \
        "lower(book.title) LIKE lower('%ros%') ESCAPE '\\'"
    assert where(builder, [{'title': ['ilike', 'ros%']}]) == \
        "lower(book.title) LIKE lower('ros%') ESCAPE '\\'"


def test_escaped_wildcards_reach_the_pattern(builder):
    """
    A percent typed by the user must be matched, not act as a wildcard. The
    client escapes it with a backslash, which means nothing unless the pattern
    says so: hence the ESCAPE clause on every LIKE the builder emits.
    """
    clause = where(builder, [{'title': ['ilike', '%50\\%%']}])
    assert '%50\\%%' in clause
    assert "ESCAPE '\\'" in clause


def test_comparison(builder):
    assert where(builder, [{'price': ['ge', 100]}]) == 'book.price >= 100'


def test_between_takes_its_two_bounds(builder):
    assert where(builder, [{'year': ['between', 2020, 2024]}]) == 'book.year BETWEEN 2020 AND 2024'


def test_in_takes_one_list(builder):
    assert where(builder, [{'province': ['in', ['TV', 'UD']]}]) == \
        "book.province IN ('TV', 'UD')"


def test_null_checks_carry_no_value(builder):
    assert where(builder, [{'note': ['isnull']}]) == 'book.note IS NULL'
    assert where(builder, [{'note': ['isnotnull']}]) == 'book.note IS NOT NULL'


def test_boolean_operators_resolve_to_a_value(builder):
    assert where(builder, [{'active': True}]) == 'book.active = true'
    assert where(builder, [{'active': False}]) == 'book.active = false'


# ── Blocks ─────────────────────────────────────────────────────────────────

def test_single_block_is_a_flat_conjunction(builder):
    assert where(builder, [{'method_id': ['in', [3, 7]]}, {'amount': ['ge', 1000]}]) == \
        'book.method_id IN (3, 7) AND book.amount >= 1000'


def test_two_blocks_become_an_or_of_conjunctions(builder):
    """AND binds tighter than OR: the parentheses must land around the blocks."""
    conditions = [{'op': 'or', 'conditions': [
        [{'province': ['in', ['TV', 'UD']]}, {'amount': ['ge', 100000]}],
        [{'code': 'X'}],
    ]}]
    assert where(builder, conditions) == \
        "book.province IN ('TV', 'UD') AND book.amount >= 100000 OR book.code = 'X'"


def test_rules_stay_beside_a_domain_instead_of_joining_it(builder):
    """
    The three slots go in as sibling groups. A domain concatenated with a rule
    set that opens with OR would end up inside the disjunction, widening the
    view instead of narrowing it — this is what mergeDomain protects against.
    """
    domain = [{'active': True}]
    rules = [{'op': 'or', 'conditions': [[{'code': 'A'}], [{'code': 'B'}]]}]
    clause = where(builder, [domain, rules])
    assert clause == "book.active = true AND (book.code = 'A' OR book.code = 'B')"


def test_the_dict_or_survives_being_flattened(builder):
    """
    Why the editor emits `{op: or, conditions: [...]}` and not the list form:
    a dict node carries its own boundary, so even a caller that concatenates it
    with the domain instead of placing it beside cannot dissolve it.
    """
    domain = {'active': True}
    rules = {'op': 'or', 'conditions': [[{'code': 'A'}], [{'code': 'B'}]]}
    assert where(builder, [domain, rules]) == where(builder, [[domain], [rules]])


def test_the_list_or_marker_is_the_one_that_dissolves(builder):
    """
    The other OR form marks the group with a leading 'op', 'or' pair, and that
    marker only means anything at the head of its own list. Concatenated with a
    domain the group loses its boundary, and the domain becomes one more branch
    of the disjunction: the view widens instead of narrowing. This is the hazard
    the sibling grouping exists for, and descriptors may still be hand-written
    in this form.
    """
    marked_or = ['op', 'or', {'code': 'A'}, {'code': 'B'}]

    beside = where(builder, [[{'active': True}], marked_or])
    assert beside == "book.active = true AND (book.code = 'A' OR book.code = 'B')"

    flattened = where(builder, ['op', 'or', {'code': 'A'}, {'code': 'B'}, {'active': True}])
    assert flattened == "book.code = 'A' OR book.code = 'B' OR book.active = true"
