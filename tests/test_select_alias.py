"""An alias is the key of a column in every record, so it arrives as written.

The client reads the key from the descriptor exactly as the author wrote it; a
server that lowercased it would send `publishername` to a grid waiting for
`publisherName`, and the column would stay empty with nothing to signal it.
"""
import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base

import kitebase.utils
from kitebase.querybuilder import DynamicQueryBuilder

Base = declarative_base()


class Book(Base):
    __tablename__ = 'book'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    price = Column(String)


class AppStub:
    query_behaviors = []


@pytest.fixture(autouse=True)
def no_behaviors(monkeypatch):
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: AppStub(), raising=False)


def labels(*select):
    builder = DynamicQueryBuilder(session=None, models={'Book': Book})
    query = builder.build_query({'table': 'Book', 'select': list(select)})
    return [c['name'] for c in query.column_descriptions]


def test_the_alias_keeps_its_case():
    assert labels('id', 'name as bookName') == ['id', 'bookName']

