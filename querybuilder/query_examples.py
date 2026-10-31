"""
A tour of the query builder, on a small Northwind database.

querybuilder.py needs nothing but SQLAlchemy: this script loads it straight
from the kitebase package, the way it runs once copied into another project.
Each example prints the SQL it generates and the rows it returns; the first
run creates northwind.sqlite with the sample data of populate.py.

    python query_examples.py         every example
    python query_examples.py 7       only the seventh
"""
import importlib.util
import os
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from model import get_models, northwind_engine
from populate import populate_sample_data

# The module as a copied file: loaded by path, under its own name, so it runs
# without the rest of kitebase.
QUERYBUILDER = Path(__file__).resolve().parent.parent / "kitebase" / "querybuilder.py"


def load_querybuilder():
    spec = importlib.util.spec_from_file_location("querybuilder", QUERYBUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Each example is (title, query). A query is a dict, or the same thing as a
# JSON string: what a web client would send.
EXAMPLES = [
    ("All the columns of a table, ordered", {
        "from": "Shipper",
        "order_by": ["company_name"],
    }),

    ("Some columns and a filter, sent as JSON", '''
    {
        "from": "Customer",
        "select": ["customer_id", "company_name", "city"],
        "filters": {"conditions": [{"country": "Germany"}]},
        "order_by": ["company_name"]
    }'''),

    ("Operators: like, in, between; conditions in a list are ANDed", {
        "from": "Product",
        "select": ["product_name", "category_id", "unit_price"],
        "filters": {"conditions": [
            {"product_name": ["like", "Ch%"]},
            {"category_id": ["in", [1, 2]]},
            {"unit_price": ["between", 15, 25]},
        ]},
    }),

    ("Nested groups: OR inside AND", {
        "from": "Product",
        "select": ["product_name", "units_in_stock", "discontinued"],
        "filters": {"conditions": [
            {"discontinued": False},
            {"op": "or", "conditions": [
                {"units_in_stock": ["<", 16]},
                {"unit_price": [">=", 25]},
            ]},
        ]},
        "order_by": ["product_name"],
    }),

    ("The verbose form of a condition: orders not shipped yet", {
        "from": "Order",
        "select": ["order_id", "order_date", "customer_id"],
        "filters": {"conditions": [
            {"column": "shipped_date", "op": "isnull"},
        ]},
    }),

    ("A date names a whole day: both orders of 5 March, at any hour", {
        "from": "Order",
        "select": ["order_id", "order_date"],
        "filters": {"conditions": [{"order_date": "1997-03-05"}]},
    }),

    ("An upper bound is inclusive: le '1997-03-31' takes the whole 31st", {
        "from": "Order",
        "select": ["order_id", "order_date"],
        "filters": {"conditions": [
            {"order_date": [">=", "1997-03-01"]},
            {"order_date": ["<=", "1997-03-31"]},
        ]},
    }),

    ("Filtering through a relationship, towards one: no join written", {
        "from": "Order",
        "select": ["order_id", "customer_id", "order_date"],
        "filters": {"conditions": [{"customer.country": "Mexico"}]},
        "order_by": ["order_id"],
    }),

    ("Towards many it becomes EXISTS: customers who ordered in 1998, once each", {
        "from": "Customer",
        "select": ["company_name"],
        "filters": {"conditions": [{"orders.order_date": [">=", "1998-01-01"]}]},
    }),

    ("Inner joins across four tables, with a computed column", {
        "from": "OrderDetail",
        "select": [
            "Order.order_id",
            "Customer.company_name as customer",
            "Employee.last_name as employee",
            "Product.product_name",
            "OrderDetail.quantity",
            "round(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount), 2) as line_total",
        ],
        "joins": [
            {"Order": "Order.order_id = OrderDetail.order_id"},
            {"Customer": "Customer.customer_id = Order.customer_id"},
            {"Employee": "Employee.employee_id = Order.employee_id"},
            {"Product": "Product.product_id = OrderDetail.product_id"},
        ],
        "filters": {"conditions": [{"Order.order_date": ["between", "1997-03-01", "1997-04-30"]}]},
        "order_by": ["Order.order_id", "Product.product_name"],
    }),

    ("A left join keeps the customer without orders", {
        "from": "Customer",
        "select": ["company_name", "count(Order.order_id) as orders"],
        "joins": [{"Order": {"type": "left", "on": "Order.customer_id = Customer.customer_id"}}],
        "group_by": ["Customer.company_name"],
        "order_by": [["orders", "desc"], "company_name"],
    }),

    ("Group by and having: sales by country and year, above 1000", {
        "from": "Order",
        "select": [
            "Customer.country",
            "extract(year from Order.order_date) as year",
            "count(distinct Order.order_id) as orders",
            "round(sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount)), 2) as sales",
        ],
        "joins": [
            {"Customer": "Customer.customer_id = Order.customer_id"},
            {"OrderDetail": "OrderDetail.order_id = Order.order_id"},
        ],
        "group_by": ["Customer.country", "extract(year from Order.order_date)"],
        "having": {"conditions": [
            {"sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount))": [">", 1000]},
        ]},
        "order_by": ["Customer.country", "year"],
    }),

    ("Expressions: days to ship, and a count with case", {
        "from": "Order",
        "select": [
            "Shipper.company_name as shipper",
            "count(Order.order_id) as orders",
            "sum(case when Order.shipped_date is null then 1 else 0 end) as not_shipped",
            "round(avg(julianday(Order.shipped_date) - julianday(Order.order_date)), 1) as avg_days",
        ],
        "joins": [{"Shipper": "Shipper.shipper_id = Order.ship_via"}],
        "group_by": ["Shipper.company_name"],
        "order_by": ["avg_days"],
    }),

    ("Top 3 products by revenue in 1997", {
        "from": "Product",
        "select": [
            "Product.product_name",
            "Category.category_name",
            "sum(OrderDetail.quantity) as units",
            "round(sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount)), 2) as revenue",
        ],
        "joins": [
            {"OrderDetail": "OrderDetail.product_id = Product.product_id"},
            {"Category": "Category.category_id = Product.category_id"},
            {"Order": "Order.order_id = OrderDetail.order_id"},
        ],
        "filters": {"conditions": [{"Order.order_date": ["between", "1997-01-01", "1997-12-31"]}]},
        "group_by": ["Product.product_name", "Category.category_name"],
        "order_by": [["revenue", "desc"]],
        "limit": 3,
    }),

    ("A page of rows: limit and offset; count_query gives the total", {
        "from": "Order",
        "select": ["order_id", "order_date"],
        "order_by": ["order_date"],
        "limit": 4,
        "offset": 4,
    }),
]

# Expressions are checked word by word: columns of the models, the functions
# and keywords of a fixed list, aliases. Anything else is refused before SQL.
REFUSED = [
    {"from": "Product", "select": ["product_name; drop table products"]},
    {"from": "Product", "select": ["lower(product_name) -- comment"]},
    {"from": "Product", "select": ["load_extension('x')"]},
    {"from": "Product", "select": ["Employee.password"]},
]


def show(builder, number, title, query):
    print(f"\n{'=' * 78}\n{number}. {title}\n{'-' * 78}")
    print(builder.get_sql(query))
    headers, rows = builder.execute_query(query, "tuples")
    print(f"\n{headers}")
    for row in rows:
        print(f"  {row}")


def main():
    os.chdir(Path(__file__).resolve().parent)
    if not os.path.exists("northwind.sqlite"):
        populate_sample_data("northwind.sqlite")

    querybuilder = load_querybuilder()
    session = Session(northwind_engine("northwind.sqlite"))
    builder = querybuilder.DynamicQueryBuilder(session, get_models())

    only = int(sys.argv[1]) if len(sys.argv) > 1 else None
    for number, (title, query) in enumerate(EXAMPLES, 1):
        if only in (None, number):
            show(builder, number, title, query)
    if only is not None:
        return

    paged = EXAMPLES[-1][1]
    print(f"\nTotal rows without limit and offset: {builder.count_query(paged)}")

    print(f"\n{'=' * 78}\nOther result formats\n{'-' * 78}")
    small = {"from": "Shipper", "select": ["shipper_id", "company_name"], "limit": 2}
    print("records:", builder.execute_query(small, "records"))
    print("json:   ", builder.execute_query(small, "json"))
    print("csv:")
    print(builder.execute_query(small, "csv"))

    print(f"\n{'=' * 78}\nRefused\n{'-' * 78}")
    for query in REFUSED:
        try:
            builder.build_query(query)
            print("NOT refused:", query["select"])
        except ValueError as error:
            print(f"  {error}")


if __name__ == "__main__":
    main()
