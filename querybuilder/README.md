# querybuilder

`kitebase/querybuilder.py` turns a query described as data, a Python dict or
the same thing as JSON, into an SQLAlchemy statement. A web client can then
compose its own queries, with joins, groups and aggregates, without an
endpoint written for each one.

It is the query engine of kitebase, and it also works on its own: copy the
file into any project and it needs nothing but SQLAlchemy.

```python
from querybuilder import DynamicQueryBuilder

builder = DynamicQueryBuilder(session, {"Customer": Customer, "Order": Order})
headers, rows = builder.execute_query({
    "from": "Order",
    "select": ["order_id", "Customer.company_name"],
    "joins": [{"Customer": "Customer.customer_id = Order.customer_id"}],
    "filters": {"conditions": [{"order_date": "1997-03-05"}]},
}, "tuples")
```

The whole syntax is in the docstring of `DynamicQueryBuilder`.

## The examples

`query_examples.py` runs on a small Northwind database, created with sample
data on the first run. It loads `querybuilder.py` by path, as a copied file,
and prints the SQL and the rows of each example:

```bash
python query_examples.py       # all of them
python query_examples.py 6     # one
```

They cover: column lists and `*`; the operators (`like`, `in`, `between`,
`isnull`...) in concise and verbose form; nested AND/OR groups; dates as
periods (a day is the whole day, an upper bound includes it); filtering
through a relationship, with no join written; inner and left joins; group by
and having; expressions (`extract`, `case`, `julianday`, `round`); limit,
offset and `count_query`; the result formats (tuples, records, JSON, CSV);
and the expressions the builder refuses.

## On its own, and inside kitebase

Inside kitebase the builder answers to the application: access control on
tables and columns, columns that never leave the server (`secret`), the text
search over the columns a table declares, and the query behaviors such as
archiving.

Copied alone it has none of these. **It reads whatever the models you give it
can reach**: pass only the models a client may query, and expose them to a
client only behind your own authentication. Expressions are still checked word
by word, so a query cannot carry SQL of its own, but which tables and columns
it may read is your decision, not the builder's.

The `search` and `resolve` keys need the table definitions of kitebase, and
have no effect or are refused without them.
