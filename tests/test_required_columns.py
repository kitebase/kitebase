"""A new row that leaves out a column the database requires.

The answer is an error by field, before the insert: the database's own error
quotes the statement and its values, and a form cannot place it under a field.
"""
import pytest
from sqlalchemy import Column, Integer, String, text
from sqlalchemy.orm import declarative_base

from kitebase import transforms
from kitebase.endpoint_db import handle_create, require_columns

Base = declarative_base()


class Member(Base):
    __tablename__ = 'members'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    password = Column(String, nullable=False)          # secret: no form shows it
    status = Column(String, nullable=False, default='active')
    code = Column(String, nullable=False, server_default=text("'M'"))
    notes = Column(String, nullable=True)


class Country(Base):
    __tablename__ = 'countries'
    iso = Column(String(2), primary_key=True)          # typed by the user
    name = Column(String, nullable=False)


def test_every_column_left_out_is_reported():
    with pytest.raises(transforms.ValidationError) as caught:
        require_columns(Member, {'notes': 'x'})

    assert set(caught.value.errors) == {'name', 'password'}


def test_a_none_counts_as_left_out():
    with pytest.raises(transforms.ValidationError) as caught:
        require_columns(Member, {'name': 'Erin', 'password': None})

    assert set(caught.value.errors) == {'password'}


def test_defaults_and_the_assigned_key_are_not_asked_for():
    require_columns(Member, {'name': 'Erin', 'password': 'hash'})


def test_a_key_the_user_types_is_asked_for():
    with pytest.raises(transforms.ValidationError) as caught:
        require_columns(Country, {'name': 'Italy'})

    assert set(caught.value.errors) == {'iso'}


def test_create_answers_by_field_without_reaching_the_database():
    result = handle_create(None, Member, {'data': {'name': 'Erin'}})

    assert result['code'] == 400
    assert set(result['data']['errors']) == {'password'}
