#!/usr/bin/env python3
"""
Import sample data into Kitebase database.

Usage:
    python sample_data/import_sample_data.py

This script will:
1. Load Publishers
2. Load Authors
3. Load Books
4. Create Book-Author relationships

Run this after initializing the database with the updated model.
"""

import csv
import sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from sqlalchemy import text

# Add directories to path
devtest_dir = Path(__file__).parent.parent  # devtest/
root_dir = devtest_dir.parent  # kitebase/ (root)

sys.path.insert(0, str(devtest_dir))  # For model.py
sys.path.insert(0, str(root_dir))     # For kitebase package

import kitebase
from kitebase.utils import get_app

# model.py imports plugin submodules (e.g. `books.model`) that only become
# importable once PluginsManager.load_plugins() has added the plugin roots to
# sys.path — so `import model` must happen after load_plugins(), inside main().
model = None


def parse_date(date_str):
    """Parse date string to date object."""
    if not date_str or date_str.strip() == '':
        return None
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None


def load_publishers(session, csv_path):
    """Load publishers from CSV."""
    print(f"\n📚 Loading publishers from {csv_path}...")
    count = 0

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            publisher = model.Publisher(
                id=int(row['id']),
                name=row['name'],
                country=row['country'] if row['country'] else None,
                website=row['website'] if row['website'] else None
            )
            session.add(publisher)
            count += 1

    session.commit()
    print(f"   ✅ Loaded {count} publishers")
    return count


def load_authors(session, csv_path):
    """Load authors from CSV."""
    print(f"\n👤 Loading authors from {csv_path}...")
    count = 0

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            author = model.Author(
                id=int(row['id']),
                first_name=row['first_name'],
                last_name=row['last_name'],
                birth_date=parse_date(row['birth_date']),
                nationality=row['nationality'] if row['nationality'] else None
            )
            session.add(author)
            count += 1

    session.commit()
    print(f"   ✅ Loaded {count} authors")
    return count


def load_books(session, csv_path):
    """Load books from CSV."""
    print(f"\n📖 Loading books from {csv_path}...")
    count = 0

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            book = model.Book(
                id=int(row['id']),
                title=row['title'],
                isbn=row['isbn'],
                publication_date=parse_date(row['publication_date']),
                price=Decimal(row['price']) if row['price'] else None,
                language=row['language'] if row['language'] else None,
                pages=int(row['pages']) if row['pages'] else None,
                publisher_id=int(row['publisher_id']) if row['publisher_id'] else None,
                description=row['description'] if row['description'] else None,
                tags=row['tags'] if row['tags'] else None,
                status='A'  # Active by default
            )
            session.add(book)
            count += 1

    session.commit()
    print(f"   ✅ Loaded {count} books")
    return count


def load_book_authors_from_column(session, books_csv_path):
    """Load book-author relationships from the `authors` column in books.csv."""
    print(f"\n🔗 Loading book-author relationships from {books_csv_path} (authors column)...")
    count = 0

    with open(books_csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if 'authors' not in (reader.fieldnames or []):
            print("   ⚠️  No 'authors' column found, skipping")
            return 0
        for row in reader:
            authors_str = row.get('authors', '').strip()
            if not authors_str:
                continue
            for author_id_str in authors_str.split():
                book_author = model.BookAuthor(
                    book_id=int(row['id']),
                    author_id=int(author_id_str),
                    notes='Primary author'
                )
                session.add(book_author)
                count += 1

    session.commit()
    print(f"   ✅ Loaded {count} book-author relationships")
    return count


def load_book_authors(session, csv_path):
    """Load book-author relationships from CSV (fallback when no authors column)."""
    print(f"\n🔗 Loading book-author relationships from {csv_path}...")
    count = 0

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            book_author = model.BookAuthor(
                book_id=int(row['book_id']),
                author_id=int(row['author_id']),
                notes=row['notes'] if row['notes'] else None
            )
            session.add(book_author)
            count += 1

    session.commit()
    print(f"   ✅ Loaded {count} book-author relationships")
    return count


def clean_existing_data(session):
    """Delete all existing data (respecting foreign keys)."""
    print("\n🧹 Cleaning existing data...")

    # Delete in order (respecting foreign key constraints)
    try:
        # 1. Delete many-to-many relationships first
        count = session.query(model.BookAuthor).delete()
        print(f"   ✓ Deleted {count} book-author relationships")

        # 2. Delete books (depends on publishers)
        count = session.query(model.Book).delete()
        print(f"   ✓ Deleted {count} books")

        # 3. Delete authors (independent)
        count = session.query(model.Author).delete()
        print(f"   ✓ Deleted {count} authors")

        # 4. Delete publishers (independent)
        count = session.query(model.Publisher).delete()
        print(f"   ✓ Deleted {count} publishers")

        # 5. Reset SQLite autoincrement sequences (if table exists)
        try:
            session.execute(text("DELETE FROM sqlite_sequence WHERE name IN "
                                 "('books', 'authors', 'publishers', 'books_authors')"))
            print("   ✓ Reset autoincrement sequences")
        except Exception:
            # Table doesn't exist yet (fresh database)
            pass

        session.commit()
        print("   ✅ Database cleaned successfully")
    except Exception as e:
        print(f"   ⚠️  Error during cleanup: {e}")
        session.rollback()
        raise


def main():
    """Main import function."""
    global model
    print("=" * 60)
    print("🚀 Kitebase Sample Data Import (Replace Mode)")
    print("=" * 60)

    # Initialize Kitebase
    print("\n⚙️  Initializing Kitebase...")
    plugins = kitebase.plugins.PluginsManager()
    plugins.load_config("config.yaml")
    kitebase.utils.register_standard_handlers(plugins)
    plugins.load_plugins()

    import model  # noqa: E402 — after load_plugins() so plugin submodules resolve

    app = get_app()
    app.calc_db(plugins)

    db_url = 'sqlite:///devtest.sqlite'
    app.initialize_db(db_url, model)

    # Get CSV file paths
    data_dir = Path(__file__).parent
    publishers_csv = data_dir / 'publishers.csv'
    authors_csv = data_dir / 'authors.csv'
    books_csv = data_dir / 'books.csv'
    book_authors_csv = data_dir / 'books_authors.csv'

    # Check required files exist
    for csv_file in [publishers_csv, authors_csv, books_csv]:
        if not csv_file.exists():
            print(f"❌ Error: {csv_file} not found!")
            return 1

    # Detect whether to use inline authors column or fallback CSV
    use_inline_authors = False
    with open(books_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        use_inline_authors = 'authors' in (reader.fieldnames or [])

    if not use_inline_authors and not book_authors_csv.exists():
        print(f"❌ Error: no 'authors' column in books.csv and {book_authors_csv} not found!")
        return 1

    try:
        # Use context manager for session
        with app.get_session() as session:
            # Clean existing data first
            clean_existing_data(session)
            # Load data in order (respecting foreign keys)
            total_publishers = load_publishers(session, publishers_csv)
            total_authors = load_authors(session, authors_csv)
            total_books = load_books(session, books_csv)
            if use_inline_authors:
                total_relationships = load_book_authors_from_column(session, books_csv)
            else:
                total_relationships = load_book_authors(session, book_authors_csv)

            # Summary
            print("\n" + "=" * 60)
            print("✨ Import Complete!")
            print("=" * 60)
            print(f"   Publishers:  {total_publishers}")
            print(f"   Authors:     {total_authors}")
            print(f"   Books:       {total_books}")
            print(f"   Relationships: {total_relationships}")
            print("=" * 60)

            # Sample queries to verify
            print("\n🔍 Sample verification queries:")

            # Count books by language
            from sqlalchemy import func
            lang_counts = session.query(
                model.Book.language,
                func.count(model.Book.id)
            ).group_by(model.Book.language).all()

            print("\n   Books by language:")
            for lang, count in lang_counts:
                lang_name = {'en': 'English', 'it': 'Italian', 'fr': 'French', 'de': 'German', 'es': 'Spanish'}
                print(f"      {lang_name.get(lang, lang)}: {count}")

            # Books with multiple authors
            multi_author_books = session.query(
                model.Book.title,
                func.count(model.BookAuthor.author_id).label('author_count')
            ).join(model.BookAuthor).group_by(model.Book.id).having(
                func.count(model.BookAuthor.author_id) > 1
            ).all()

            if multi_author_books:
                print("\n   Books with multiple authors:")
                for title, count in multi_author_books:
                    print(f"      {title} ({count} authors)")

            print("\n✅ Database successfully populated with sample data!")

    except Exception as e:
        print(f"\n❌ Error during import: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
