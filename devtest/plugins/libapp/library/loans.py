"""
The rules of a loan, and its ticket.

The bench for the rules of a table (kitebase.flow), one of each kind: a
refusal, a question, a line for the person. They hold on every port that writes
a loan: the form, a call to `db`, a tree, a batch. The ticket is not one of
them: printing it is a reaction of the desk's form (`on_saved` in loans.yaml),
and the list reprints it on demand.
"""
import html
from datetime import datetime

import kitebase
from kitebase import access
from kitebase.endpoints import FileResult, endpoint
from kitebase.flow import on_write

OPEN_LIMIT = 3


def _open_loans(ctx, **where):
    Loan = kitebase.utils.get_app().model.Loan
    query = ctx.session.query(Loan).filter(Loan.returned_at.is_(None))
    for column, value in where.items():
        query = query.filter(getattr(Loan, column) == value)
    if ctx.mode != 'insert':
        query = query.filter(Loan.id != ctx.record['id'])
    return query


@on_write('Loan')
def loan_rules(ctx):
    if ctx.mode == 'delete' or ctx.record.get('returned_at'):
        return
    app = kitebase.utils.get_app()

    # A refusal: a book is lent to one member at a time
    other = _open_loans(ctx, book_id=ctx.record['book_id']).first()
    if other is not None:
        book = ctx.session.get(app.model.Book, ctx.record['book_id'])
        holder = ctx.session.get(app.model.LibraryUser, other.library_user_id)
        ctx.stay(f"'{book.title}' is already on loan to {holder.name}")

    # A question, only when lending: the limit is a habit, not a law
    if ctx.mode == 'insert':
        count = _open_loans(ctx, library_user_id=ctx.record['library_user_id']).count()
        if count >= OPEN_LIMIT:
            member = ctx.session.get(app.model.LibraryUser, ctx.record['library_user_id'])
            if not ctx.confirm('many_open',
                               f"{member.name} already has {count} loans open.",
                               yes='Lend all the same', no='Do not lend'):
                ctx.stay('Not lent')

    due = ctx.record.get('due_date')
    if ctx.mode == 'insert' and isinstance(due, datetime):
        ctx.info(f'Due back on {due:%d %B %Y}')


@endpoint('loan_print_ticket', access=access.PER_TABLE)
def loan_print_ticket(data):
    """The list's command: the ticket again, for a print that did not come out."""
    access.check_table('Loan', 'read')
    if data.get('id') is None:
        return {'status': 'error', 'message': 'Select a loan first', 'code': 400}
    return {'status': 'success',
            'data': {'action': 'download', 'op': 'loan_ticket', 'params': {'id': data['id']}}}


@endpoint('loan_ticket', access=access.PER_TABLE)
def loan_ticket(data):
    """The ticket of a loan, a plain page the browser opens ready to print."""
    app = kitebase.utils.get_app()
    with app.get_session() as session:
        access.guard_session(session)
        loan = session.get(app.model.Loan, data.get('id'))
        if loan is None:
            return {'status': 'error', 'message': f"No loan {data.get('id')}", 'code': 404}
        book = session.get(app.model.Book, loan.book_id)
        member = session.get(app.model.LibraryUser, loan.library_user_id)
        rows = [('Loan', f'#{loan.id}'), ('Member', member.name), ('Book', book.title),
                ('Borrowed', f'{loan.borrowed_at:%d/%m/%Y}'),
                ('Due back', f'{loan.due_date:%d/%m/%Y}')]

    cells = ''.join(f'<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>'
                    for k, v in rows)
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Loan #{loan.id}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
  table {{ border-collapse: collapse; }}
  th {{ text-align: left; padding: .3rem 1rem .3rem 0; color: #555; font-weight: normal; }}
  td {{ padding: .3rem 0; font-weight: 600; }}
</style></head>
<body onload="window.print()">
  <h1>Library loan</h1>
  <table>{cells}</table>
</body></html>"""
    return FileResult(page.encode('utf-8'), f'loan_{loan.id}.html', 'text/html', 'inline')
