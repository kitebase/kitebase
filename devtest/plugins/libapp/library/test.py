import kitebase
from kitebase.endpoints import endpoint


def ok():
    print("ok")
    return True


@endpoint('sayhello')
def say_hello(data):
    name = data.get("name", "World")
    lang = data.get("lang", "en")

    if lang == "it":
        message = f"Ciao, '{name}'!"
    elif lang == "es":
        message = f"Hola, '{name}'!"
    else:
        message = f"Hello, '{name}'!"

    return {
        "status": "success",
        "data": message,
        "code": 200
    }


@endpoint('book_stats')
def book_stats(data):
    """What a button in a form can call: it acts, and reports in one line.

    The bench for the fourth family (relations.md §19.1). The record it works on
    arrives as an id resolved from the form's draft (`$record.id`), which is also
    why it must say something sensible when there is none: a book being entered
    has no key yet, and the button is on screen all the same.
    """
    book_id = data.get('id')
    if not book_id or (isinstance(book_id, int) and book_id < 0):
        return {'status': 'error', 'message': 'Save the book first — it has no key yet',
                'code': 400}

    app = kitebase.utils.get_app()
    with app.get_session() as session:
        book = session.get(app.model.Book, book_id)
        if book is None:
            return {'status': 'error', 'message': f'No book {book_id}', 'code': 404}

        loans = session.query(app.model.Loan).filter_by(book_id=book_id).count()
        out = session.query(app.model.Loan).filter_by(book_id=book_id, returned_at=None).count()
        reviews = session.query(app.model.Review).filter_by(book_id=book_id).all()
        ratings = [r.rating for r in reviews if r.rating is not None]
        average = round(sum(ratings) / len(ratings), 1) if ratings else None

    message = f'{loans} loans ({out} out), {len(reviews)} reviews'
    if average is not None:
        message += f', average rating {average}'

    return {'status': 'success', 'message': message, 'code': 200,
            'data': {'loans': loans, 'on_loan': out,
                     'reviews': len(reviews), 'average_rating': average}}


@endpoint('books')
def query_books(data):
    app = kitebase.utils.get_app()

    with app.get_session() as session:
        books = session.query(app.model.Book).all()
        data = []
        for book in books:
            if len(data) > 10:
                break
            data.append(book.title)

    return {
        "status": "success",
        "data": data,
        "code": 200
    }


@endpoint('color_chooser')
def color_chooser(data):
    """A chain of questions composed by the server, one answer at a time.

    The bench for the result vocabulary (`resultAction.ts`): every answer is
    data, and the client turns it into a dialog without knowing what a colour
    is. Three steps, each a call to this same endpoint with the next `step`:

      (none)   → an `endpoint` action with `confirm`: "Do you want to see the
                 choices?" — yes calls step `list`, no is silence
      list     → a `choose` whose entries carry the call for step `picked`
      picked   → the report, "You chose Red"

    Dismissing the menu is not an answer, so nothing happens: a closed menu
    means nothing, and the server never hears about it.
    """
    step = data.get('step')
    colours = [('red', 'Red'), ('green', 'Green'), ('blue', 'Blue')]

    if step == 'list':
        return {'status': 'success', 'data': {
            'action': 'choose',
            'title': 'Colours',
            'message': 'Pick one',
            'options': [
                {'label': label,
                 'current': data.get('current') == code,
                 'then': {'action': 'endpoint', 'op': 'color_chooser',
                          'params': {'step': 'picked', 'color': code}}}
                for code, label in colours
            ],
        }}

    if step == 'picked':
        label = dict(colours).get(data.get('color'), '?')
        return {'status': 'success', 'data': {
            'message': f'You chose {label}',
            'detail': f'color = {data.get("color")!r}',
            'detail_label': 'What the server received',
        }}

    return {'status': 'success', 'data': {
        'action': 'endpoint',
        'op': 'color_chooser',
        'params': {'step': 'list', 'current': data.get('current')},
        'confirm': 'Do you want to see the choices?',
    }}
