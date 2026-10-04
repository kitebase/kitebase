import os
import sys
from model import get_models, northwind_engine
from sqlalchemy.orm import sessionmaker
from populate import populate_sample_data
sys.path.append("..")

# Various query samples
query_Customer_all = {
    "from": "Customer",
    "select": ["*"]
}

query_Customer_germany = '''
{
    "from": "Customer",
    "select": ["customer_id", "company_name", "contact_name", "city"],
    "filters": {
        "conditions": [
            {"country": "Germany"}
        ]
    },
    "order_by": ["company_name"]
}
'''

query_Order_with_Customer = '''
{
    "from": "Order",
    "select": [
        "order_id",
        "order_date",
        "Customer.company_name",
        "Customer.contact_name"
    ],
    "joins": [
        {"Customer": "Customer.customer_id = Order.customer_id"}
    ],
    "filters": {
        "conditions": [
            {"order_date": [">=", "1997-01-01"]}
        ]
    },
    "order_by": [
        ["order_date", "desc"]
    ],
    "limit": 20
}
'''

query_sales_by_category = '''
{
    "from": "OrderDetail",
    "select": [
        "Category.category_name",
        "sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount)) as total_sales"
    ],
    "joins": [
        {"Product": "Product.product_id = OrderDetail.product_id"},
        {"Category": "Category.category_id = Product.category_id"}
    ],
    "group_by": ["Category.category_name"],
    "order_by": [
        ["total_sales", "desc"]
    ]
}
'''

query_OrderDetail_full = '''
{
    "from": "OrderDetail",
    "select": [
        "Order.order_id",
        "Order.order_date",
        "Customer.company_name as customer",
        "Employee.last_name as employee",
        "Product.product_name",
        "OrderDetail.quantity",
        "OrderDetail.unit_price",
        "OrderDetail.discount",
        "(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount)) as line_total"
    ],
    "joins": [
        {"Order": "Order.order_id = OrderDetail.order_id"},
        {"Customer": "Customer.customer_id = Order.customer_id"},
        {"Employee": "Employee.employee_id = Order.employee_id"},
        {"Product": "Product.product_id = OrderDetail.product_id"}
    ],
    "filters": {
        "conditions": [
            {"Order.order_date": ["between", "1997-01-01", "1997-12-31"]}
        ]
    },
    "order_by": [
        "Order.order_id",
        "Product.product_name"
    ]
}
'''

query_Product_low_stock = '''
{
    "from": "Product",
    "select": [
        "product_id",
        "product_name",
        "units_in_stock",
        "reorder_level",
        "Supplier.company_name as supplier",
        "Category.category_name"
    ],
    "joins": [
        {"Supplier": "Supplier.supplier_id = Product.supplier_id"},
        {"Category": "Category.category_id = Product.category_id"}
    ],
    "filters": {
        "conditions": [
            {"discontinued": 0},
            {"op": "or", "conditions": [
                {"units_in_stock": ["<", 10]},
                {"op": "and", "conditions": [
                    {"units_in_stock": ["<", "reorder_level"]},
                    {"units_in_stock": [">", 0]}
                ]}
            ]}
        ]
    },
    "order_by": [
        "Supplier.company_name",
        "Product.product_name"
    ]
}
'''

query_sales_dashboard = '''
{
    "from": "Order",
    "select": [
        "Customer.country",
        "EXTRACT(YEAR FROM Order.order_date) as year",
        "count(Order.order_id) as order_count",
        "sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount)) as total_sales"
    ],
    "joins": [
        {"Customer": "Customer.customer_id = Order.customer_id"},
        {"OrderDetail": "OrderDetail.order_id = Order.order_id"}
    ],
    "filters": {
        "conditions": [
            {"Order.order_date": [">=", "1996-01-01"]},
            {"Order.shipped_date": ["is not null"]}
        ]
    },
    "group_by": [
        "Customer.country",
        "EXTRACT(YEAR FROM Order.order_date)"
    ],
    "having": {
        "conditions": [
            {"sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount))": [">", 10000]}
        ]
    },
    "order_by": [
        "Customer.country",
        ["year", "asc"]
    ]
}
'''

query_shipping_performance = '''
{
    "from": "Order",
    "select": [
        "Shipper.company_name as shipper",
        "count(Order.order_id) as shipment_count",
        "avg(EXTRACT(DAY FROM (Order.shipped_date - Order.order_date))) as avg_days_to_ship"
    ],
    "joins": [
        {"Shipper": "Shipper.shipper_id = Order.ship_via"}
    ],
    "filters": {
        "conditions": [
            {"Order.shipped_date": ["is not null"]},
            {"Order.order_date": [">=", "1997-01-01"]}
        ]
    },
    "group_by": [
        "Shipper.company_name"
    ],
    "order_by": [
        ["avg_days_to_ship", "asc"]
    ]
}
'''

query_best_selling_Product = '''
{
    "from": "Product",
    "select": [
        "Product.product_id",
        "Product.product_name",
        "Category.category_name",
        "Supplier.company_name as supplier",
        "sum(OrderDetail.quantity) as units_sold",
        "sum(OrderDetail.quantity * OrderDetail.unit_price * (1 - OrderDetail.discount)) as revenue"
    ],
    "joins": [
        {"OrderDetail": "OrderDetail.product_id = Product.product_id"},
        {"Category": "Category.category_id = Product.category_id"},
        {"Supplier": "Supplier.supplier_id = Product.supplier_id"},
        {"Order": "Order.order_id = OrderDetail.order_id"}
    ],
    "filters": {
        "conditions": [
            {"Order.order_date": ["between", "1997-01-01", "1997-12-31"]}
        ]
    },
    "group_by": [
        "Product.product_id",
        "Product.product_name",
        "Category.category_name",
        "Supplier.company_name"
    ],
    "order_by": [
        ["units_sold", "desc"]
    ],
    "limit": 10
}
'''


if __name__ == "__main__":
    from kitebase.querybuilder import DynamicQueryBuilder

    db_path = "northwind.sqlite"
    if not os.path.exists(db_path):
        populate_sample_data(db_path)

    engine = northwind_engine(db_path)
    Session = sessionmaker(bind=engine)
    session = Session()
    models = get_models()
    builder = DynamicQueryBuilder(session, models)

    queries = [
        query_Customer_all,
        query_Customer_germany,
        query_Order_with_Customer,
        query_sales_by_category,
        query_OrderDetail_full,
        query_Product_low_stock,
        query_sales_dashboard,
        query_shipping_performance,
        query_best_selling_Product
    ]

    for q in queries:
        print("\n=== sql string ===============================================\n")
        sql_string = builder.get_sql(q)
        print(sql_string)

        print("\n=== query results ============================================\n")
        results = builder.execute_query(q)
        print(results)

        print("\n=== headers ==================================================\n")
        headers = builder.get_query_headers(q)
        print(headers)

        if False:  # switch to True to see other formats
            print("\n=== various formats ==========================================\n")
            for format in ('default', 'dict', 'records', 'tuples', 'json'):
                result = builder.execute_query(q, result_format=format)
                print(f"Format {format}:", result)
