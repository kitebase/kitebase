"""
The development data of devtest, in the order it builds on itself.

    users        admin and one user per library role
    catalogue    publishers, authors and books from sample_data/*.csv
    covers       the covers on disk, linked to the books without one
    members      readers of the library, invented
    circulation  loans and reviews, over the last months
    acquisitions two orders and a receipt

Each step runs while its own table is empty, so what is entered from the UI is
left alone, and a database made before a step gets it on the next start. The
movements are dated back from today and drawn with a fixed seed: a database
rebuilt today and one rebuilt next month look the same, each fresh on its day.

Rebuilding: delete devtest.sqlite and start a server, or run devtest.py.
The catalogue comes from sample_data/fetch_openlibrary.py (see its README).

Passwords are seeded in plaintext on purpose. Writing straight through
SQLAlchemy skips the `on_write` transform of the endpoints, which leaves
devtest in the state a database predating hashing is in: the one case the
login path would otherwise never exercise here.
"""
import csv
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func

from kitebase import apptime

SAMPLE = Path(__file__).parent / 'sample_data'
COVERS = Path(__file__).parent / 'media' / 'books' / 'covers'


def seed(app, model=None):
    """Fill what is empty, step by step; each step commits on its own."""
    model = model or app.model
    rng = random.Random(42)
    steps = [seed_users, seed_catalogue, seed_covers, seed_members, seed_circulation,
             seed_acquisitions]
    for step in steps:
        with app.get_session() as session:
            added = step(session, model, rng)
            if added:
                session.commit()
                print(f'seed: {step.__name__.removeprefix("seed_")} +{added}')


# ── Users ──────────────────────────────────────────────────────────────────────

# One user per role of the library (plugins/libapp/library/acl.yaml), the
# password the same as the username, like admin.
STAFF = [
    ('catalog', 'Carol Hayes', ['library.catalog']),
    ('desk', 'Dennis Ward', ['library.desk']),
    ('librarian', 'Laura Bennett', ['library.librarian']),
    ('stats', 'Steve Norris', ['library.stats']),
    ('buyer', 'Bianca Moretti', ['acquisitions.buyer']),
]


def seed_users(session, model, rng=None) -> int:
    """admin/admin and the users of STAFF a database does not have yet."""
    have = {u for (u,) in session.query(model.User.username)}
    added = 0
    if 'admin' not in have:
        session.add(model.User(name='Admin User', email='admin@example.com',
                               username='admin', password='admin', is_admin=True))
        added += 1
    for username, name, roles in STAFF:
        if username not in have:
            session.add(model.User(name=name, email=f'{username}@example.com',
                                   username=username, password=username, roles=roles))
            added += 1
    return added


# ── Catalogue ──────────────────────────────────────────────────────────────────

def _rows(name: str) -> list[dict]:
    with open(SAMPLE / name, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _date(text: str) -> date | None:
    return date.fromisoformat(text) if text else None


def _int(text: str) -> int | None:
    return int(text) if text else None


def seed_catalogue(session, model, rng=None) -> int:
    """The books of sample_data, with their publishers and authors, ids as in the files."""
    if session.query(model.Book).first():
        return 0
    publishers = [model.Publisher(id=int(r['id']), name=r['name'],
                                  country=r['country'] or None, website=r['website'] or None)
                  for r in _rows('publishers.csv')]
    authors = [model.Author(id=int(r['id']), first_name=r['first_name'], last_name=r['last_name'],
                            birth_date=_date(r['birth_date']), death_date=_date(r['death_date']),
                            nationality=r['nationality'] or None, wikidata=r['wikidata'] or None)
               for r in _rows('authors.csv')]
    session.add_all(publishers + authors)
    session.flush()

    from tags.model import name_key as tag_key    # one tag per name, as the plugin decides
    books, links, tags, tagged = [], [], {}, []
    for r in _rows('books.csv'):
        books.append(model.Book(
            id=int(r['id']), title=r['title'], original_title=r['original_title'] or None,
            isbn=r['isbn'], publication_date=_date(r['publication_date']),
            first_published=_int(r['first_published']), price=Decimal(r['price']),
            language=r['language'], pages=_int(r['pages']),
            publisher_id=_int(r['publisher_id']), status='A'))
        links += [model.BookAuthor(book_id=int(r['id']), author_id=int(a))
                  for a in r['authors'].split()]
        # The first tag is the main genre: links keep the order they are written in
        names = [name.strip() for name in r['tags'].split(',') if name.strip()]
        for name in names:
            if tag_key(name) not in tags:
                tags[tag_key(name)] = model.BookTag(name=name)
        tagged.append((int(r['id']), [tag_key(name) for name in names]))
    session.add_all(books + list(tags.values()))
    session.flush()
    session.add_all(links)
    session.add_all(model.BookTagLink(book_id=book_id, tag_id=tags[name].id)
                    for book_id, names in tagged for name in names)
    return len(publishers) + len(authors) + len(books) + len(tags)


def seed_covers(session, model, rng=None) -> int:
    """Link media/books/covers/<isbn>.jpg to the books that have no cover.

    `fetch_openlibrary.py --covers` fills the folder, before or after the
    database is made: the next start links what arrived.
    """
    if not COVERS.is_dir():
        return 0
    on_disk = {f.stem for f in COVERS.glob('*.jpg')}
    added = 0
    for book in session.query(model.Book).filter(model.Book.cover.is_(None)):
        if book.isbn in on_disk:
            book.cover = f'books:covers/{book.isbn}.jpg'
            added += 1
    return added


# ── Members ────────────────────────────────────────────────────────────────────

FIRST_NAMES = [
    'Alice', 'Bruno', 'Chiara', 'Davide', 'Elena', 'Fabio', 'Giulia', 'Hugo', 'Irene',
    'Jacopo', 'Katia', 'Luca', 'Marta', 'Nicola', 'Olivia', 'Paolo', 'Rita', 'Sara',
    'Tommaso', 'Ugo', 'Valeria', 'Walter', 'Zoe', 'Emma', 'Leo', 'Nina', 'Oscar', 'Pia',
]
LAST_NAMES = [
    'Bianchi', 'Colombo', 'Ferrari', 'Galli', 'Greco', 'Lombardi', 'Marino', 'Moretti',
    'Ricci', 'Romano', 'Bruno', 'Conti', 'Costa', 'Fontana', 'Gatti', 'Leone', 'Longo',
    'Mancini', 'Rinaldi', 'Serra', 'Vitale', 'Carter', 'Hughes', 'Novak', 'Weber',
]
CITIES = [('Udine', '33100', 'UD'), ('Trieste', '34121', 'TS'), ('Pordenone', '33170', 'PN'),
          ('Gorizia', '34170', 'GO'), ('Venezia', '30121', 'VE'), ('Treviso', '31100', 'TV')]
STREETS = ['Via Roma', 'Via Mazzini', 'Via Garibaldi', 'Viale Venezia', 'Via Dante',
           'Piazza Libertà', 'Via Cavour', 'Via Manin', 'Via Verdi', 'Borgo Grazzano']
MEMBERS = 40


def seed_members(session, model, rng) -> int:
    """MEMBERS readers with invented names; Mario Rossi first, as devtest.main() logs in as him.

    Joined in the last three years, renewed yearly; about one in six let it lapse.
    """
    if session.query(model.LibraryUser).first():
        return 0
    today = apptime.today()
    people = [('Mario', 'Rossi', 'mrossi', 'hashed_password_here')]
    names = set()
    while len(people) < MEMBERS:
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        if (first, last) in names:
            continue
        names.add((first, last))
        username = f'{first[0]}{last}'.lower()
        people.append((first, last, username, username))

    for n, (first, last, username, password) in enumerate(people, 1):
        city, zip_code, province = rng.choice(CITIES)
        joined = today - timedelta(days=rng.randint(10, 3 * 365))
        years = (today - joined).days // 365 + (0 if rng.random() < 0.17 else 1)
        session.add(model.LibraryUser(
            name=f'{first} {last}', username=username, password=password,
            email=f'{username}@example.com', is_student=rng.random() < 0.3,
            membership_number=f'M{n:04d}', membership_date=joined,
            membership_expires=joined + timedelta(days=365 * max(years, 1)),
            phone=f'+39 3{rng.randint(20, 49)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}',
            a_address=f'{rng.choice(STREETS)} {rng.randint(1, 120)}', a_city=city,
            a_zip=zip_code, a_state=province, a_country='Italy'))
    return len(people)


# ── Circulation ────────────────────────────────────────────────────────────────

LOAN_DAYS = 30
HISTORY_DAYS = 180
LOANS = 700
OPEN_PER_MEMBER = 3      # the habit of the desk (library/loans.py)
FRONT_BOOKS, FRONT_MEMBERS = 20, 10

COMMENTS = {
    5: ['A wonderful read, highly recommended.', 'Could not put it down.',
        'A true masterpiece.', 'Everyone should read this at least once.'],
    4: ['Well written, with characters that stay with you.', 'Very good, a little long.',
        'The narrative style is unique and captivating.', 'Read it in a weekend.'],
    3: ['Interesting, though slow to start.', 'Good in parts.', 'Fine, not memorable.'],
    2: ['Somewhat disappointing, I expected more.', 'Hard going.'],
    1: ['Not for me.'],
}


def seed_circulation(session, model, rng) -> int:
    """Loans over the last HISTORY_DAYS and the reviews of who read the book.

    Lent as the desk lends: one member at a time per book, at most
    OPEN_PER_MEMBER open per member. The older loans are mostly back, a few
    are overdue, the recent ones mostly still out. A review follows a return.
    Half the loans go to the first FRONT books and members, so the records
    opened first have a history to show.
    """
    if session.query(model.Loan).first():
        return 0
    members = [m for (m,) in session.query(model.LibraryUser.id).order_by(model.LibraryUser.id)]
    books = [b for (b,) in session.query(model.Book.id).order_by(model.Book.id)]
    if not members or not books:
        return 0
    # Readers favour a part of the shelf, as they do.
    front, popular = books[:FRONT_BOOKS], rng.sample(books, min(len(books), 150))
    now = apptime.now().replace(second=0, microsecond=0)

    starts = sorted(now - timedelta(minutes=rng.randint(0, HISTORY_DAYS * 24 * 60))
                    for _ in range(LOANS))
    busy_until: dict[int, datetime] = {}       # book -> when it is back
    open_by_member: dict[int, list[datetime]] = {}
    loans, reviews, reviewed = [], [], set()
    for borrowed in starts:

        member = rng.choice(members[:FRONT_MEMBERS] if rng.random() < 0.5 else members)
        still_open = [t for t in open_by_member.get(member, []) if t > borrowed]
        if len(still_open) >= OPEN_PER_MEMBER:
            continue
        pick = rng.random()
        book = rng.choice(front if pick < 0.5 else popular if pick < 0.85 else books)
        if busy_until.get(book, borrowed) > borrowed:
            continue
        age = (now - borrowed).days
        returned = None
        if rng.random() < (0.95 if age > LOAN_DAYS else 0.35):
            kept = rng.randint(3, max(3, min(LOAN_DAYS + 10, age - 1)))
            returned = borrowed + timedelta(days=kept, minutes=rng.randint(0, 360))
            if returned > now:
                returned = None
        back = returned or datetime.max
        busy_until[book] = back
        open_by_member[member] = still_open + [back]
        loans.append(model.Loan(book_id=book, library_user_id=member, borrowed_at=borrowed,
                                due_date=borrowed + timedelta(days=LOAN_DAYS),
                                returned_at=returned))
        if returned and (book, member) not in reviewed and rng.random() < 0.6:
            reviewed.add((book, member))
            rating = rng.choices([5, 4, 3, 2, 1], weights=[35, 35, 18, 8, 4])[0]
            reviews.append(model.Review(
                book_id=book, library_user_id=member, rating=rating,
                comment=rng.choice(COMMENTS[rating]),
                review_date=(returned + timedelta(days=rng.randint(0, 5))).date()))
    session.add_all(loans + reviews)
    return len(loans) + len(reviews)


# ── Acquisitions ───────────────────────────────────────────────────────────────

def seed_acquisitions(session, model, rng=None) -> int:
    """Two orders to the publisher with the most books, the first half received.

    Enough to open a document, and to have something still to arrive. It needs
    a publisher with three books, and skips quietly without.
    """
    if session.query(model.PurchaseOrder).first():
        return 0
    row = (session.query(model.Book.publisher_id, func.count())
           .filter(model.Book.publisher_id.isnot(None))
           .group_by(model.Book.publisher_id)
           .order_by(func.count().desc(), model.Book.publisher_id).first())
    if row is None or row[1] < 3:
        return 0
    publisher_id = row[0]
    books = (session.query(model.Book).filter_by(publisher_id=publisher_id)
             .order_by(model.Book.id).limit(5).all())
    today = apptime.today()

    first = model.PurchaseOrder(publisher_id=publisher_id, notes='Spring titles',
                                order_date=today - timedelta(days=20),
                                expected_delivery=today - timedelta(days=6))
    second = model.PurchaseOrder(publisher_id=publisher_id, order_date=today - timedelta(days=4),
                                 expected_delivery=today + timedelta(days=10))
    session.add_all([first, second])
    session.flush()
    lines = []
    for n, book in enumerate(books, start=1):
        lines.append(model.PurchaseOrderLine(
            purchase_order_id=first.id, sequence=n, book_id=book.id,
            quantity=n + 1, price=book.price, discount=10 if n % 2 else None))
    for n, book in enumerate(books[:2], start=1):
        lines.append(model.PurchaseOrderLine(
            purchase_order_id=second.id, sequence=n, book_id=book.id,
            quantity=3, price=book.price))
    session.add_all(lines)
    session.flush()

    receipt = model.Receipt(publisher_id=publisher_id, reference='DN 0412',
                            receipt_date=today - timedelta(days=7))
    session.add(receipt)
    session.flush()
    session.add_all(model.ReceiptLine(
        receipt_id=receipt.id, sequence=n, book_id=line.book_id,
        quantity=line.quantity, purchase_order_line_id=line.id)
        for n, line in enumerate(lines[:2], start=1))
    return 3
