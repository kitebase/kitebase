"""
A publisher's order sheet: their layout, one order, opened as a draft.

The sheet holds no order number and no publisher: one sheet is one order,
and the publisher is proposed from its books. When the books belong to more
than one publisher nothing is proposed: the person chooses, and the step to
the rows offers to drop the others (acquisitions/orders.py). Nothing is
written until the form is saved. Sample sheet: devtest/sample_data/publisher_order.xlsx.
"""
import kitebase
from kitebase import access
from kitebase.endpoints import endpoint
from kitebase.sheets import Ignored, Lookup, Number, SheetImport


class Percent(Number):
    """A number that may carry a '%' sign: '15%' is 15."""

    def clean(self, text: str) -> str:
        return text.rstrip('%').strip()


class PublisherSheet(SheetImport):
    page = 'purchaseorder_form'
    collection = 'lines'
    sequence = 'sequence'
    noun, nouns = 'order', 'orders'
    columns = {
        'EAN':         Lookup('Book', 'isbn', field='book_id', ignore='- ', required=True),
        'Description': Ignored(),
        'Qty':         Number(field='quantity', positive=True, required=True),
        'Unit price':  Number(field='price', min=0, optional=True),
        'Disc.':       Percent(field='discount', min=0, max=100, optional=True),
        'Notes':       Ignored(),
    }

    def propose(self, session, tree):
        Book = kitebase.utils.get_app().model.Book
        book_ids = {row['values']['book_id'] for row in tree['children'][self.collection]}
        publishers = {p for (p,) in session.query(Book.publisher_id).filter(Book.id.in_(book_ids))}
        if len(publishers) == 1:
            tree['values']['publisher_id'] = publishers.pop()


@endpoint('publisher_sheet', access=access.PER_TABLE)
def publisher_sheet(data):
    """The list's command: ask for the sheet."""
    access.check_table('PurchaseOrder', 'create')
    return {'status': 'success',
            'data': {'action': 'upload', 'op': 'publisher_sheet_draft', 'accept': ['.xlsx']}}


@endpoint('publisher_sheet_draft', access=access.PER_TABLE)
def publisher_sheet_draft(data):
    """The sheet, opened in the order form as a new order to finish."""
    access.check_table('PurchaseOrder', 'create')
    return PublisherSheet().draft(data)
