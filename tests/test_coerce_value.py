"""
A value typed in a form arrives as text; the db port turns it into what the
column holds before writing, since a driver may refuse text (SQLite takes no
string for a Time). What cannot be read stays as it was, for the database or a
rule to refuse in its own words.
"""
import datetime

from sqlalchemy import Column, Date, DateTime, Integer, String, Time
from sqlalchemy.orm import declarative_base

from kitebase.endpoint_db import coerce_value

Base = declarative_base()


class Shift(Base):
    __tablename__ = 'shifts'
    id = Column(Integer, primary_key=True)
    day = Column(Date)
    stamped_at = Column(DateTime)
    starts = Column(Time)
    note = Column(String)


def test_dates_times_and_numbers_are_read_from_text():
    assert coerce_value(Shift, 'day', '2026-11-24') == datetime.date(2026, 11, 24)
    assert coerce_value(Shift, 'stamped_at', '2026-11-24T09:30') == \
        datetime.datetime(2026, 11, 24, 9, 30)
    assert coerce_value(Shift, 'starts', '22:00') == datetime.time(22, 0)
    assert coerce_value(Shift, 'starts', '22:00:15') == datetime.time(22, 0, 15)
    assert coerce_value(Shift, 'id', '7') == 7


def test_what_cannot_be_read_stays_as_it_was():
    assert coerce_value(Shift, 'starts', '9 o clock') == '9 o clock'
    assert coerce_value(Shift, 'starts', '') == ''
    assert coerce_value(Shift, 'note', '22:00') == '22:00'
    assert coerce_value(Shift, 'starts', datetime.time(8, 0)) == datetime.time(8, 0)
