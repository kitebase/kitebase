"""
What the order form proposes on its way from the header to the rows.

An order goes to one publisher, so its rows are that publisher's books. The
case is a publisher changed once the rows are in: the step to the rows
offers to take away those of another publisher, and stays on the header
otherwise. A proposal writes nothing: the form applies the patch, and the
order is written when it is saved.
"""
from datetime import date, timedelta

import kitebase
from kitebase import access
from kitebase.duplicate import duplicator
from kitebase.endpoints import endpoint
from kitebase.flow import proposal


@endpoint('purchase_order_head', access=access.PER_TABLE)
@proposal('PurchaseOrder')
def purchase_order_head(ctx):
    publisher_id = ctx.record.get('publisher_id')
    lines = ctx.rows.get('lines', [])
    book_ids = {r['book_id'] for r in lines if r.get('book_id') is not None}
    if publisher_id is None or not book_ids:
        return

    Book = kitebase.utils.get_app().model.Book
    theirs = {book_id for (book_id,) in ctx.session.query(Book.id)
              .filter(Book.id.in_(book_ids), Book.publisher_id == publisher_id)}
    foreign = [r for r in lines if r.get('book_id') is not None and r['book_id'] not in theirs]
    if not foreign:
        return

    count = len(foreign)
    text = (f"{count} row is a book of another publisher." if count == 1
            else f"{count} rows are books of another publisher.")
    if not ctx.confirm('drop_foreign', text, yes='Remove it' if count == 1 else 'Remove them',
                       no='Back to the header'):
        ctx.stay(text)
    ctx.rows['lines'] = [r for r in lines if r not in foreign]
    ctx.info(f"Removed: {', '.join(_titles(ctx, foreign))}")


@duplicator('purchaseorder_form', options='purchaseorder_duplicate')
def order_duplicate(tree, choices):
    """A new order from another: the same publisher, the date chosen, the lines if asked."""
    tree['values']['order_date'] = choices['order_date']
    if not choices.get('lines', True):
        tree['children']['lines'] = []


@order_duplicate.options_values
def order_duplicate_values(source):
    """The date proposed: a month after the order copied, the usual rhythm of a reorder."""
    return {'order_date': _month_after(source['values']['order_date']).isoformat()}


def _month_after(when):
    day = date.fromisoformat(str(when)[:10])
    year, month = divmod(day.month, 12)
    following = date(day.year + year, month + 1, 1)
    last = (date(following.year + following.month // 12, following.month % 12 + 1, 1)
            - timedelta(days=1)).day
    return following.replace(day=min(day.day, last))


def _titles(ctx, rows):
    Book = kitebase.utils.get_app().model.Book
    ids = [r['book_id'] for r in rows]
    titles = dict(ctx.session.query(Book.id, Book.title).filter(Book.id.in_(ids)))
    return [titles.get(i, f'#{i}') for i in ids]
