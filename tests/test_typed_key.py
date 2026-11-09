"""A primary key someone types, rather than one the database assigns.

What has to hold: the generated form lets it be typed and asks for it, where an
assigned key stays read-only; a create on a key already taken is refused on the
key's field, naming the value and saying when the row holding it is archived;
an update cannot change it; a table with an assigned key is untouched by both.
"""
import pytest
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base

from kitebase import transforms
from kitebase.endpoint_db import refuse_duplicate_key, refuse_key_change, typed_key
from kitebase.pages import field_from_column

Base = declarative_base()


class Country(Base):
    __tablename__ = 'countries'
    iso = Column(String(2), primary_key=True)
    name = Column(String)


class Rate(Base):
    __tablename__ = 'rates'
    country = Column(String(2), primary_key=True)
    year = Column(Integer, primary_key=True, autoincrement=False)
    value = Column(Integer)


class Unit(Base):
    __tablename__ = 'units'
    code = Column(String(8), primary_key=True)
    active = Column(Boolean, default=True)

    @property
    def is_archived(self):
        return self.active is False


class Member(Base):
    __tablename__ = 'members'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)


@pytest.fixture
def session():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([Country(iso='IT', name='Italy'), Rate(country='IT', year=2026, value=22),
                   Unit(code='KG', active=True), Unit(code='LB', active=False),
                   Member(id=1, name='Erin')])
        s.commit()
        yield s


# ── Which keys are typed ──────────────────────────────────────────────────

def test_a_key_the_database_assigns_is_not_typed():
    assert typed_key(Member) == []
    assert [c.key for c in typed_key(Country)] == ['iso']
    assert [c.key for c in typed_key(Rate)] == ['country', 'year']


# ── The generated form ────────────────────────────────────────────────────

class FakeColumn:
    def __init__(self, name, **attributes):
        self.name = name
        self.attributes = attributes


def test_the_form_asks_for_a_typed_key_and_locks_an_assigned_one():
    typed = field_from_column(FakeColumn('id', type='ShortID', primary_key=True))
    assigned = field_from_column(FakeColumn('id', type='ID', primary_key=True, autoincrement=True))

    assert typed.get('readonly') is None and typed['required'] is True
    assert assigned['readonly'] is True


# ── Create ────────────────────────────────────────────────────────────────

def test_a_taken_key_is_refused_on_its_field(session):
    with pytest.raises(transforms.ValidationError) as caught:
        refuse_duplicate_key(session, Country, {'iso': 'IT', 'name': 'Again'})

    assert caught.value.errors == {'iso': 'IT already exists'}


def test_a_key_held_by_an_archived_row_says_so(session):
    with pytest.raises(transforms.ValidationError) as caught:
        refuse_duplicate_key(session, Unit, {'code': 'LB'})

    assert caught.value.errors == {'code': 'LB already exists, archived'}


def test_a_compound_key_is_refused_on_all_its_fields(session):
    with pytest.raises(transforms.ValidationError) as caught:
        refuse_duplicate_key(session, Rate, {'country': 'IT', 'year': 2026})

    assert caught.value.errors == {'country': 'IT, 2026 already exists',
                                   'year': 'IT, 2026 already exists'}


def test_a_free_key_and_an_assigned_one_pass(session):
    refuse_duplicate_key(session, Country, {'iso': 'FR'})
    refuse_duplicate_key(session, Rate, {'country': 'IT', 'year': 2027})
    refuse_duplicate_key(session, Member, {'id': 1, 'name': 'Erin'})


# ── Update ────────────────────────────────────────────────────────────────

def test_an_update_cannot_change_a_typed_key(session):
    italy = session.get(Country, 'IT')
    with pytest.raises(transforms.ValidationError) as caught:
        refuse_key_change(italy, Country, {'iso': 'FR', 'name': 'France'})

    assert set(caught.value.errors) == {'iso'}


def test_an_update_that_repeats_the_key_passes(session):
    refuse_key_change(session.get(Country, 'IT'), Country, {'iso': 'IT', 'name': 'Italia'})
    refuse_key_change(session.get(Member, 1), Member, {'id': 2})
