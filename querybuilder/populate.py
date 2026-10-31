import datetime
from sqlalchemy.orm import sessionmaker
from model import create_northwind_db, get_models
from decimal import Decimal


def populate_sample_data(db_path):
    """
    Populate the Northwind database with sample data
    """

    engine = create_northwind_db(db_path)
    Session = sessionmaker(bind=engine)
    session = Session()

    models = get_models()

    try:
        categories = [
            models['Category'](category_id=1, category_name="Beverages",
                               description="Soft drinks, coffees, teas, beers, and ales"),
            models['Category'](category_id=2, category_name="Condiments",
                               description="Sweet and savory sauces, relishes, spreads, and seasonings"),
            models['Category'](category_id=3, category_name="Confections",
                               description="Desserts, candies, and sweet breads"),
            models['Category'](category_id=4, category_name="Dairy Products",
                               description="Cheeses"),
            models['Category'](category_id=5, category_name="Grains/Cereals",
                               description="Breads, crackers, pasta, and cereal")
        ]
        session.add_all(categories)

        suppliers = [
            models['Supplier'](supplier_id=1, company_name="Exotic Liquids",
                               contact_name="Charlotte Cooper", contact_title="Purchasing Manager",
                               address="49 Gilbert St.", city="London", postal_code="EC1 4SD",
                               country="UK", phone="(171) 555-2222"),
            models['Supplier'](supplier_id=2, company_name="New Orleans Cajun Delights",
                               contact_name="Shelley Burke", contact_title="Order Administrator",
                               address="P.O. Box 78934", city="New Orleans", region="LA",
                               postal_code="70117", country="USA", phone="(100) 555-4822"),
            models['Supplier'](supplier_id=3, company_name="Grandma Kelly's Homestead",
                               contact_name="Regina Murphy", contact_title="Sales Representative",
                               address="707 Oxford Rd.", city="Ann Arbor", region="MI",
                               postal_code="48104", country="USA", phone="(313) 555-5735")
        ]
        session.add_all(suppliers)

        products = [
            models['Product'](product_id=1, product_name="Chai", supplier_id=1, category_id=1,
                              quantity_per_unit="10 boxes x 20 bags", unit_price=Decimal("18.00"),
                              units_in_stock=39, units_on_order=0, reorder_level=10, discontinued=False),
            models['Product'](product_id=2, product_name="Chang", supplier_id=1, category_id=1,
                              quantity_per_unit="24 - 12 oz bottles", unit_price=Decimal("19.00"),
                              units_in_stock=17, units_on_order=40, reorder_level=25, discontinued=False),
            models['Product'](product_id=3, product_name="Aniseed Syrup", supplier_id=1, category_id=2,
                              quantity_per_unit="12 - 550 ml bottles", unit_price=Decimal("10.00"),
                              units_in_stock=13, units_on_order=70, reorder_level=25, discontinued=False),
            models['Product'](product_id=4, product_name="Chef Anton's Cajun Seasoning", supplier_id=2, category_id=2,
                              quantity_per_unit="48 - 6 oz jars", unit_price=Decimal("22.00"),
                              units_in_stock=53, units_on_order=0, reorder_level=0, discontinued=False),
            models['Product'](product_id=5, product_name="Grandma's Boysenberry Spread", supplier_id=3, category_id=2,
                              quantity_per_unit="12 - 8 oz jars", unit_price=Decimal("25.00"),
                              units_in_stock=120, units_on_order=0, reorder_level=25, discontinued=False),
            models['Product'](product_id=6, product_name="Uncle Bob's Organic Dried Pears", supplier_id=3,
                              category_id=3, quantity_per_unit="12 - 1 lb pkgs.", unit_price=Decimal("30.00"),
                              units_in_stock=15, units_on_order=0, reorder_level=10, discontinued=False),
            models['Product'](product_id=7, product_name="Queso Cabrales", supplier_id=2, category_id=4,
                              quantity_per_unit="1 kg pkg.", unit_price=Decimal("21.00"),
                              units_in_stock=0, units_on_order=0, reorder_level=30, discontinued=True)
        ]
        session.add_all(products)

        customers = [
            models['Customer'](customer_id="ALFKI", company_name="Alfreds Futterkiste",
                               contact_name="Maria Anders", contact_title="Sales Representative",
                               address="Obere Str. 57", city="Berlin", postal_code="12209",
                               country="Germany", phone="030-0074321"),
            models['Customer'](customer_id="ANATR", company_name="Ana Trujillo Emparedados y helados",
                               contact_name="Ana Trujillo", contact_title="Owner",
                               address="Avda. de la Constitución 2222", city="México D.F.",
                               postal_code="05021", country="Mexico", phone="(5) 555-4729"),
            models['Customer'](customer_id="ANTON", company_name="Antonio Moreno Taquería",
                               contact_name="Antonio Moreno", contact_title="Owner",
                               address="Mataderos 2312", city="México D.F.",
                               postal_code="05023", country="Mexico", phone="(5) 555-3932"),
            # No orders: the left join example keeps it, an inner join would not
            models['Customer'](customer_id="BLAUS", company_name="Blauer See Delikatessen",
                               contact_name="Hanna Moos", contact_title="Sales Representative",
                               address="Forsterstr. 57", city="Mannheim",
                               postal_code="68306", country="Germany", phone="0621-08460")
        ]
        session.add_all(customers)

        employees = [
            models['Employee'](employee_id=1, last_name="Davolio", first_name="Nancy",
                               title="Sales Representative", title_of_courtesy="Ms.",
                               birth_date=datetime.date(1968, 12, 8),
                               hire_date=datetime.date(1992, 5, 1),
                               address="507 - 20th Ave. E. Apt. 2A", city="Seattle",
                               region="WA", postal_code="98122", country="USA",
                               home_phone="(206) 555-9857", extension="5467"),
            models['Employee'](employee_id=2, last_name="Fuller", first_name="Andrew",
                               title="Vice President, Sales", title_of_courtesy="Dr.",
                               birth_date=datetime.date(1952, 2, 19),
                               hire_date=datetime.date(1992, 8, 14),
                               address="908 W. Capital Way", city="Tacoma",
                               region="WA", postal_code="98401", country="USA",
                               home_phone="(206) 555-9482", extension="3457"),
            models['Employee'](employee_id=3, last_name="Leverling", first_name="Janet",
                               title="Sales Representative", title_of_courtesy="Ms.",
                               birth_date=datetime.date(1963, 8, 30),
                               hire_date=datetime.date(1992, 4, 1),
                               address="722 Moss Bay Blvd.", city="Kirkland",
                               region="WA", postal_code="98033", country="USA",
                               home_phone="(206) 555-3412", extension="3355")
        ]
        employees[0].reports_to = 2  # Nancy reports to Andrew
        employees[2].reports_to = 2  # Janet reports to Andrew

        session.add_all(employees)

        shippers = [
            models['Shipper'](shipper_id=1, company_name="Speedy Express", phone="(503) 555-9831"),
            models['Shipper'](shipper_id=2, company_name="United Package", phone="(503) 555-3199"),
            models['Shipper'](shipper_id=3, company_name="Federal Shipping", phone="(503) 555-9931")
        ]
        session.add_all(shippers)

        # order_id, customer, employee, ordered, shipped (None: not yet), shipper, freight,
        # lines as (product, unit price, quantity, discount). Two orders on 5 March
        # 1997 at different hours show that a day in a filter is a whole day, the one
        # on the afternoon of 31 March that an upper bound "le 31 March" keeps it.
        order_rows = [
            (10248, "ALFKI", 1, (1997, 1, 6), (1997, 1, 14), 3, "32.38",
             [(1, "14.00", 12, 0), (3, "9.80", 10, 0)]),
            (10249, "ANATR", 3, (1997, 1, 20), (1997, 1, 24), 1, "11.61",
             [(2, "15.20", 5, 0), (4, "17.60", 9, 0)]),
            (10250, "ANTON", 2, (1997, 2, 11), (1997, 2, 15), 2, "65.83",
             [(5, "16.80", 20, 0.15)]),
            (10251, "ALFKI", 1, (1997, 3, 5, 10, 15), (1997, 3, 12), 2, "41.34",
             [(1, "18.00", 30, 0.05), (6, "30.00", 6, 0)]),
            (10252, "ANTON", 3, (1997, 3, 5, 16, 40), (1997, 3, 7), 1, "51.30",
             [(4, "22.00", 15, 0.05), (7, "21.00", 10, 0)]),
            (10253, "ANATR", 1, (1997, 3, 31, 14, 20), (1997, 4, 8), 3, "58.17",
             [(2, "19.00", 40, 0.1), (5, "25.00", 12, 0)]),
            (10254, "ALFKI", 2, (1997, 6, 10), (1997, 6, 23), 2, "22.98",
             [(3, "10.00", 25, 0), (7, "21.00", 8, 0.05)]),
            (10255, "ANTON", 1, (1997, 9, 2), (1997, 9, 5), 1, "148.33",
             [(1, "18.00", 50, 0.1), (2, "19.00", 30, 0.1), (4, "22.00", 20, 0)]),
            (10256, "ANATR", 3, (1997, 11, 18), (1997, 11, 28), 3, "13.97",
             [(5, "25.00", 10, 0)]),
            (10257, "ALFKI", 3, (1998, 1, 15), (1998, 1, 21), 2, "81.91",
             [(1, "18.00", 20, 0), (5, "25.00", 15, 0.05)]),
            (10258, "ANTON", 2, (1998, 2, 3), None, 1, "140.51",
             [(2, "19.00", 50, 0.2), (6, "30.00", 6, 0)]),
        ]
        customers_by_id = {c.customer_id: c for c in customers}
        for (order_id, customer_id, employee_id, ordered, shipped, shipper, freight,
             lines) in order_rows:
            customer = customers_by_id[customer_id]
            ordered = datetime.datetime(*ordered)
            session.add(models['Order'](
                order_id=order_id, customer_id=customer_id, employee_id=employee_id,
                order_date=ordered, required_date=ordered + datetime.timedelta(days=28),
                shipped_date=datetime.datetime(*shipped) if shipped else None,
                ship_via=shipper, freight=Decimal(freight),
                ship_name=customer.company_name, ship_address=customer.address,
                ship_city=customer.city, ship_postal_code=customer.postal_code,
                ship_country=customer.country))
            for product_id, price, quantity, discount in lines:
                session.add(models['OrderDetail'](
                    order_id=order_id, product_id=product_id, unit_price=Decimal(price),
                    quantity=quantity, discount=discount))

        session.commit()
        return True

    except Exception as e:
        session.rollback()
        print(f"Error while inserting the sample data: {e}")
        return False

    finally:
        session.close()
