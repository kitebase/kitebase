#!/usr/bin/env python3
"""
Seed loans and reviews for the first 5 books.

Run from the devtest/ directory:
    python sample_data/seed_relations.py
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
import random

devtest_dir = Path(__file__).parent.parent
root_dir = devtest_dir.parent
sys.path.insert(0, str(devtest_dir))
sys.path.insert(0, str(root_dir))

import kitebase
from kitebase.utils import get_app

# model.py imports plugin submodules (e.g. `books.model`) that only become
# importable once PluginsManager.load_plugins() has added the plugin roots to
# sys.path — so `import model` must happen after load_plugins(), inside main().
model = None


LIBRARY_USERS = [
    dict(name="Alice Morgan",   username="amorgan",  email="alice.morgan@example.com",   password="pass"),
    dict(name="Bob Lawson",     username="blawson",  email="bob.lawson@example.com",     password="pass"),
    dict(name="Carol Finch",    username="cfinch",   email="carol.finch@example.com",    password="pass"),
    dict(name="David Park",     username="dpark",    email="david.park@example.com",     password="pass"),
]

COMMENTS = [
    "A wonderful read, highly recommended.",
    "Couldn't put it down — absolutely gripping.",
    "Interesting, though a bit slow to start.",
    "A true masterpiece of world literature.",
    "Well written with deeply developed characters.",
    "Somewhat disappointing — I expected more.",
    "Everyone should read this at least once.",
    "The narrative style is unique and captivating.",
]


def main():
    global model
    print("Initializing Kitebase...")
    plugins = kitebase.plugins.PluginsManager()
    plugins.load_config("config.yaml")
    kitebase.utils.register_standard_handlers(plugins)
    plugins.load_plugins()

    import model  # noqa: E402 — after load_plugins() so plugin submodules resolve

    app = get_app()
    app.calc_db(plugins)
    app.initialize_db('sqlite:///devtest.sqlite', model)

    with app.get_session() as session:

        # ── Ensure library users exist ────────────────────────────────────────
        users = []
        for u in LIBRARY_USERS:
            existing = session.query(model.LibraryUser).filter_by(username=u['username']).first()
            if existing:
                users.append(existing)
            else:
                lu = model.LibraryUser(**u, is_student=False)
                session.add(lu)
                session.flush()
                users.append(lu)
                print(f"  Created library user: {u['name']}")

        # ── Get first 5 books ─────────────────────────────────────────────────
        books = session.query(model.Book).order_by(model.Book.id).limit(5).all()
        if not books:
            print("No books found — run import_sample_data.py first.")
            return 1
        print(f"\nSeeding relations for {len(books)} book(s):")

        now = datetime.now()
        created_loans = 0
        created_reviews = 0

        for i, book in enumerate(books):
            print(f"\n  [{book.id}] {book.title}")

            # 2 loans per book — different users, different dates
            for j in range(2):
                user = users[j % len(users)]
                borrowed = now - timedelta(days=random.randint(10, 90))
                due = borrowed + timedelta(days=30)
                returned = borrowed + timedelta(days=random.randint(5, 25)) if random.random() > 0.4 else None

                # skip if already exists
                exists = session.query(model.Loan).filter_by(
                    book_id=book.id, library_user_id=user.id
                ).first()
                if not exists:
                    session.add(model.Loan(
                        book_id=book.id,
                        library_user_id=user.id,
                        borrowed_at=borrowed,
                        due_date=due,
                        returned_at=returned,
                    ))
                    created_loans += 1
                    status = 'returned' if returned else 'active'
                    print(f"    loan   → {user.name} ({status})")

            # 2-3 reviews per book — different users, different ratings
            n_reviews = 2 + (i % 2)
            for j in range(n_reviews):
                user = users[(i + j + 1) % len(users)]
                rating = random.randint(3, 5)
                comment = COMMENTS[(i * 3 + j) % len(COMMENTS)]

                exists = session.query(model.Review).filter_by(
                    book_id=book.id, library_user_id=user.id
                ).first()
                if not exists:
                    session.add(model.Review(
                        book_id=book.id,
                        library_user_id=user.id,
                        rating=rating,
                        comment=comment,
                    ))
                    created_reviews += 1
                    print(f"    review → {user.name}  ★{rating}")

        session.commit()
        print(f"\nDone — {created_loans} loans, {created_reviews} reviews created.")

    return 0


if __name__ == '__main__':
    sys.exit(main())
