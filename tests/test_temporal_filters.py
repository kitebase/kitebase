"""Filters on temporal columns receive ISO strings and must compare as dates.

A filter value arrives from JSON as text. Bound as text on SQLite it is compared
with the stored text, whose spelling differs from the browser's ('2026-09-29
18:15:18' against '2026-09-29T08:10'): the range looked right and returned no
rows. The defect only shows when the query runs, so these tests execute it.
"""
import datetime

import pytest
from sqlalchemy import Column, Date, DateTime, Integer, Time, create_engine
from sqlalchemy.orm import Session, declarative_base

import kitebase.utils
from kitebase.endpoint_db import build_filters
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Visit(Base):
    __tablename__ = 'visit'
    id = Column(Integer, primary_key=True)
    start = Column(DateTime)
    day = Column(Date)
    hour = Column(Time)


class AppStub:
    query_behaviors = []


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Visit(id=1, start=datetime.datetime(2026, 9, 29, 18, 15, 18),
                  day=datetime.date(2026, 9, 29), hour=datetime.time(18, 15)),
            Visit(id=2, start=datetime.datetime(2026, 9, 30, 9, 0),
                  day=datetime.date(2026, 9, 30), hour=datetime.time(9, 0)),
            Visit(id=3, start=datetime.datetime(2026, 10, 1, 7, 30),
                  day=datetime.date(2026, 10, 1), hour=datetime.time(7, 30)),
        ])
        s.commit()
        yield s


def ids(session, conditions):
    builder = DynamicQueryBuilder(session=session, models={'Visit': Visit})
    query = builder.build_query({
        'table': 'Visit',
        'select': ['id'],
        'filters': {'conditions': conditions},
        'order_by': ['id'],
    })
    return [row[0] for row in session.execute(query)]


def test_datetime_range_from_browser(session):
    # The case found on SAD: datetime-local values, 'T' separator, no seconds
    assert ids(session, [{'start': ['between', '2026-09-29T08:10', '2026-09-30T23:10']}]) == [1, 2]


def test_datetime_single_bounds(session):
    assert ids(session, [{'start': ['ge', '2026-09-30T00:00']}]) == [2, 3]
    assert ids(session, [{'start': ['lt', '2026-09-30T00:00']}]) == [1]


def test_datetime_equality_is_the_minute(session):
    assert ids(session, [{'start': '2026-09-30T09:00'}]) == [2]
    assert ids(session, [{'start': '2026-09-29T18:15'}]) == [1]   # stored 18:15:18


# ── A value counts for its precision: a day, a minute ──────────────────────

def test_date_on_datetime_is_the_whole_day(session):
    assert ids(session, [{'start': '2026-09-29'}]) == [1]
    assert ids(session, [{'start': ['ne', '2026-09-29']}]) == [2, 3]


def test_range_of_days_includes_the_last_day(session):
    assert ids(session, [{'start': ['between', '2026-09-29', '2026-09-30']}]) == [1, 2]


def test_one_sided_bounds_by_day(session):
    assert ids(session, [{'start': ['le', '2026-09-30']}]) == [1, 2]
    assert ids(session, [{'start': ['gt', '2026-09-30']}]) == [3]
    assert ids(session, [{'start': ['ge', '2026-09-30']}]) == [2, 3]
    assert ids(session, [{'start': ['lt', '2026-09-30']}]) == [1]


def test_upper_minute_is_inclusive(session):
    # 18:15 as the end keeps 18:15:18: no 23:59 trick needed for the end of a day
    assert ids(session, [{'start': ['between', '2026-09-29T08:00', '2026-09-29T18:15']}]) == [1]
    assert ids(session, [{'start': ['between', '2026-09-29T08:00', '2026-09-29T18:14']}]) == []


def test_days_in_a_list(session):
    assert ids(session, [{'start': ['in', ['2026-09-29', '2026-10-01']]}]) == [1, 3]


def test_instant_compares_exactly(session):
    assert ids(session, [{'start': '2026-09-29T18:15:18.000001'}]) == []


def test_date_and_time_columns(session):
    assert ids(session, [{'day': ['between', '2026-09-29', '2026-09-30']}]) == [1, 2]
    assert ids(session, [{'day': ['in', ['2026-09-29', '2026-10-01']]}]) == [1, 3]
    assert ids(session, [{'hour': ['lt', '10:00']}]) == [2, 3]


def test_null_checks_untouched(session):
    assert ids(session, [{'start': ['isnotnull']}]) == [1, 2, 3]


def test_db_endpoint_filters(session):
    clause = build_filters(Visit, {'start__gte': '2026-09-29T08:10', 'start__lte': '2026-09-30T23:10'})
    found = session.query(Visit.id).filter(clause).order_by(Visit.id).all()
    assert [r[0] for r in found] == [1, 2]
    clause = build_filters(Visit, {'start': '2026-09-30'})
    assert [r[0] for r in session.query(Visit.id).filter(clause)] == [2]
