# querybuilder

The script `querybuilder.py` is part of the Kitebase framework, but it is truly
independent.

It can be used to generate a query for an SQLAlchemy database starting from a
Python dictionary, written following specific rules.

The advantage is that the query can be generated outside the Python code
for instance, from a web client and transmitted as a JSON stream.

See `query_examples.py` for some query examples. It uses a database model
inspired by the popular `Northwind` database example. In it, you can see most
of the features and rules for dynamic query generation.

When you run the examples for the first time, an SQLite version of the database
will be generated and populated with sample data. Then, you can see how it
works.