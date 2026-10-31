"""querybuilder.py copied out of kitebase works with SQLAlchemy alone.

The file is copied into an empty directory and run in a fresh interpreter where
`import kitebase` fails: an import of the framework outside the block at the
top of the module breaks this test, not a user who copied the file.
"""
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import kitebase.access
import kitebase.querybuilder

SCRIPT = textwrap.dedent('''
    import datetime
    import sys
    sys.modules['kitebase'] = None  # any import of the framework now fails

    from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, create_engine
    from sqlalchemy.orm import Session, declarative_base

    from querybuilder import DynamicQueryBuilder

    Base = declarative_base()

    class Customer(Base):
        __tablename__ = 'customers'
        id = Column(Integer, primary_key=True)
        name = Column(String)

    class Order(Base):
        __tablename__ = 'orders'
        id = Column(Integer, primary_key=True)
        customer_id = Column(Integer, ForeignKey('customers.id'))
        placed = Column(DateTime)

    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Customer(id=1, name='Ada'), Customer(id=2, name='Bob'),
            Order(id=1, customer_id=1, placed=datetime.datetime(1997, 3, 5, 18, 30)),
            Order(id=2, customer_id=2, placed=datetime.datetime(1997, 3, 6, 9, 0)),
        ])
        session.commit()

        builder = DynamicQueryBuilder(session, {'Customer': Customer, 'Order': Order})
        rows = builder.execute_query({
            'from': 'Order',
            'select': ['id', 'Customer.name'],
            'joins': [{'Customer': 'Customer.id = Order.customer_id'}],
            'filters': {'conditions': [{'placed': '1997-03-05'}]},
        }, 'tuples')
        print(rows[1])
''')


def test_copied_file_needs_only_sqlalchemy(tmp_path: Path):
    shutil.copy(Path(kitebase.querybuilder.__file__), tmp_path / 'querybuilder.py')
    (tmp_path / 'run.py').write_text(SCRIPT)

    done = subprocess.run([sys.executable, 'run.py'], cwd=tmp_path,
                          capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    # The day names a period: the order at 18:30 is in it, the next day's is not
    assert done.stdout.strip() == "[[1, 'Ada']]"


def test_inside_the_package_the_framework_is_wired():
    assert kitebase.querybuilder.access is kitebase.access
