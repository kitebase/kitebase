# acquisitions: the bench of the document form

Orders to the publishers and the receipts against them. Not a purchasing
module: quantities are numbers, nothing counts stock, and what is still to
arrive would be reckoned from the rows. The plugin exists to prove, one by
one, the mechanisms a document form needs (a header, rows, steps, commands),
on a case small enough to read whole. Sign in as `buyer` / `buyer`.

Each feature below says what real need it stands for and which mechanism it
exercises. The derived plugin `acq_publisher_sheet` adds the spreadsheet of
a publisher without this plugin knowing anything about it.

## The order in three steps

`purchaseorder_form` declares `phases:` header, lines, footer. Each step is
judged by the server on the way out (`orders.py`), and each judges its own
phase: an error is told where it can be mended, never a step later.

- **Header to lines** (`purchase_order_head`): a publisher changed once the
  lines are in leaves lines of another publisher behind. The server asks
  whether to remove them (`ctx.confirm`), or stays on the header.
- **Lines to footer** (`purchase_order_lines`): at least one line, all of the
  order's publisher. A refusal stays on the lines.
- **Footer to save** (`purchase_order_close`): the last step's `next` runs
  before anything is written. The expected delivery cannot come before the
  order. A refusal stays on the footer; a judge may also name another phase
  (`ctx.stay(text, phase=...)`) when the problem lies elsewhere.

A proposal writes nothing: it answers with a patch of the draft, and the
order is written once, when it is saved.

## Lines ordered by hand

The lines collection declares `sequence: sequence`. The reader decides the
order: the reorder button of the grid shows the handles, and Alt+Up /
Alt+Down move the current line. The column is of the `Sequence` type of the
commons: a number of service, hidden from generated forms and lists, and
numbered 1, 2, 3 by the form when the order is saved. A line number to quote
outside would be another column, one that never moves.

## Commands on the lines

Commands of the lines grid work on the draft, not on the database: the form
sends the document with where the command was given (ticked lines, lines left
by the column filters, or all), and applies what comes back.

- **Discount on lines** (`purchase_order_discount`): asks the percentage
  through a form view (`ctx.form`) and applies it to the chosen lines.
- **Sort by publication date** (`purchase_order_sort_by_publication`): an
  order computed from a fact the grid does not show, the book's date. It
  always works on all the lines: sorting some of them would leave the rest
  nowhere.

## A field of the form only

"Discount for new lines" is declared `transient` in the header: no column
holds it, the new lines take it through the collection's `prefill`, and it is
never saved. A help for writing the lines, not a fact of the order.

## Duplicate

A command on a row of the list opens a copy of the order as a new draft
(`duplicate`, a core endpoint). The date and the supplier's reference say
`duplicate: false`, and so do the timestamps of the commons: a copy is a new
order. The duplicator in `orders.py` asks first, with a dialog whose date is
proposed a month after the order copied, and whether to copy the lines.

## Orders from a spreadsheet

**Import from spreadsheet** (`order_import.py`) writes the orders of a sheet in
our own layout, one row per line, grouped by the `order` column. Everything
is checked first (header, types, keys), every mistake told by row and column,
then the whole sheet is written in one transaction, each order through the
same steps its form would take. The header row is found, not assumed, and
columns the layout does not know are left out and named. Sample sheets are
in `devtest/sample_data/`.

The publisher's own sheet (`acq_publisher_sheet`) is not written but opened:
from the list as a new order to finish, its publisher proposed from its
books; from the lines of an order already open, added after the lines there
or in their place.

## Still to come

The receipt against the orders: the lines still to receive proposed when the
receipt steps from its header to its lines, received in full or in part, and
a rule of the table that refuses more than was ordered, whatever port writes
the receipt.
