"""
Orders from a spreadsheet: our layout, one row per order line.

The first row names the columns; `order` groups the rows into orders, the
publisher and the book are found by name and by ISBN, `title` is for whoever
reads the sheet. Every order goes through the form's phases (orders.py asks
about books of another publisher) and the sheet is written whole, or not at
all. Sample sheets: devtest/sample_data/purchase_orders*.xlsx.
"""
from kitebase import access
from kitebase.endpoints import endpoint
from kitebase.sheets import Ignored, Key, Lookup, Number, SheetImport


class OrderSheet(SheetImport):
    page = 'purchaseorder_form'
    collection = 'lines'
    sequence = 'sequence'
    noun, nouns = 'order', 'orders'
    columns = {
        'order':     Key(),
        'publisher': Lookup('Publisher', 'name', field='publisher_id', head=True, required=True),
        'isbn':      Lookup('Book', 'isbn', field='book_id', ignore='- ', required=True),
        'title':     Ignored(),
        'quantity':  Number(field='quantity', positive=True, required=True),
        'price':     Number(field='price', min=0),
        'discount':  Number(field='discount', min=0, max=100),
    }


@endpoint('purchase_order_import', access=access.PER_TABLE)
def purchase_order_import(data):
    """The list's command: ask for the sheet."""
    access.check_table('PurchaseOrder', 'create')
    return {'status': 'success',
            'data': {'action': 'upload', 'op': 'purchase_order_import_sheet', 'accept': ['.xlsx']}}


@endpoint('purchase_order_import_sheet', access=access.PER_TABLE)
def purchase_order_import_sheet(data):
    """The sheet, read and written as the order form would write its orders."""
    access.check_table('PurchaseOrder', 'create')
    return OrderSheet().run(data)
