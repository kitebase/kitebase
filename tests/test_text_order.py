"""Text is ordered as a person reads it, whatever the case.

SQLite compares bytes, so a list that mixes upper and mixed case (a legacy
import: `BIANCHI ANNA` beside `Bellini Luca`) would read as two lists. Only
the ordering changes: equality still tells `Rossi` from `ROSSI`.
"""
import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Person(Base):
    __tablename__ = 'person'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    age = Column(Integer)


class AppStub:
    query_behaviors = []


@pytest.fixture(autouse=True)
def no_behaviors(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)


@pytest.fixture
def session():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([Person(id=1, name='BENETTI MARCO', age=30), Person(id=2, name='Bellini Luca', age=9),
                   Person(id=3, name='BIANCHI ANNA', age=100), Person(id=4, name='Rossi', age=40),
                   Person(id=5, name='ROSSI', age=40)])
        s.commit()
        yield s


def rows(session, **query):
    builder = DynamicQueryBuilder(session, {'Person': Person})
    return [r[0] for r in session.execute(builder.build_query({'table': 'Person', **query}))]


def test_names_in_alphabetical_order_whatever_the_case(session):
    assert rows(session, select=['name'], order_by=['name'])[:3] == [
        'Bellini Luca', 'BENETTI MARCO', 'BIANCHI ANNA']


def test_the_direction_still_holds(session):
    assert rows(session, select=['name'], order_by=[['name', 'desc']])[-3:] == [
        'BIANCHI ANNA', 'BENETTI MARCO', 'Bellini Luca']


def test_numbers_are_left_alone(session):
    assert rows(session, select=['age'], order_by=['age']) == [9, 30, 40, 40, 100]


def test_equality_still_tells_the_case_apart(session):
    assert rows(session, select=['id'], filters={'conditions': [{'name': 'Rossi'}]}) == [4]


def test_without_an_engine_nothing_is_added():
    """The other engines order case-insensitively by their own collation."""
    sql = str(DynamicQueryBuilder(None, {'Person': Person})
              .build_query({'table': 'Person', 'select': ['name'], 'order_by': ['name']}))
    assert 'COLLATE' not in sql
