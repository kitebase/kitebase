#!/usr/bin/env python3
"""
Build the sample catalogue of devtest from Open Library and Wikidata.

Run once, from devtest/, when the catalogue is to be made again; the CSV files
it writes are committed, so rebuilding the database needs no network:

    uv run python sample_data/fetch_openlibrary.py            # CSV + sheets
    uv run python sample_data/fetch_openlibrary.py --sheets   # sheets only, from the CSV
    uv run python sample_data/fetch_openlibrary.py --covers   # covers into media/books/covers

Books come from the Open Library search, a fixed list of subjects times a
quota per language, so the same run gives the same catalogue while the
sources do not change. Each book is one real edition, with its ISBN-13, its
publisher and its title in that language. Authors get dates and nationality
from Wikidata, found through the Open Library id (property P648).

Texts are left out on purpose: author biographies and book descriptions are
looked up on demand, from the ISBN and the Wikidata id the catalogue keeps.

Answers are cached in sample_data/_cache/: a second run asks the network only
for what changed in the lists below. Delete the folder to start over.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / '_cache'
COVERS = HERE.parent / 'media' / 'books' / 'covers'
USER_AGENT = 'kitebase-devtest-sample-data/1.0 (https://github.com/kitebase)'

# (tag, Open Library subject). The tag is the primary genre of the book.
GENRES = [
    ('science fiction', 'science_fiction'),
    ('fantasy', 'fantasy'),
    ('mystery', 'mystery_and_detective_stories'),
    ('horror', 'horror_tales'),
    ('historical fiction', 'historical_fiction'),
    ('romance', 'romance'),
    ('adventure', 'adventure_stories'),
    ('humor', 'humor'),
    ('short stories', 'short_stories'),
    ('classics', 'classic_literature'),
    ('young adult', 'young_adult_fiction'),
    ('children', 'juvenile_fiction'),
    ('poetry', 'poetry'),
    ('drama', 'drama'),
    ('biography', 'biography'),
    ('history', 'history'),
    ('philosophy', 'philosophy'),
    ('science', 'science'),
    ('psychology', 'psychology'),
    ('art', 'art'),
    ('travel', 'travel'),
    ('cooking', 'cooking'),
]
FICTION = {'science fiction', 'fantasy', 'mystery', 'horror', 'historical fiction',
           'romance', 'adventure', 'humor', 'short stories', 'classics',
           'young adult', 'children', 'poetry', 'drama', 'italian literature'}

# Italian writers, a few books each in Italian: the subjects of Open Library
# find Italian translations of everything, and almost no Italian literature.
ITALIAN_AUTHORS = [
    'Italo Calvino', 'Umberto Eco', 'Luigi Pirandello', 'Primo Levi', 'Alberto Moravia',
    'Elsa Morante', 'Dino Buzzati', 'Leonardo Sciascia', 'Cesare Pavese',
    'Alessandro Manzoni', 'Giovanni Verga', 'Italo Svevo', 'Giuseppe Tomasi di Lampedusa',
    'Elena Ferrante', 'Alessandro Baricco', 'Andrea Camilleri', 'Carlo Collodi',
    'Grazia Deledda', 'Beppe Fenoglio', 'Natalia Ginzburg', 'Antonio Tabucchi',
    'Gianni Rodari', 'Emilio Salgari', 'Pier Paolo Pasolini', 'Dacia Maraini',
]
PER_AUTHOR = 2

# Books per genre and language. The smaller languages go first: a work found
# in two of them is kept in the first, and English has the most to choose from.
QUOTAS = {'it': 8, 'fr': 4, 'de': 3, 'es': 3, 'en': 22}
MARC = {'en': 'eng', 'it': 'ita', 'fr': 'fre', 'de': 'ger', 'es': 'spa'}
MAX_AUTHORS = 3

# Names that say a role, or are no person: such a co-author is dropped.
NOT_AUTHORS = re.compile(
    r'narrator|translator|traduct|illustrator|\bstaff\b|\bclassics?\b', re.IGNORECASE)

# Print-on-demand and digitisation imprints: an edition from them is skipped.
NOT_PUBLISHERS = re.compile(
    r'independently|createspace|gutenberg|lulu|kindle|amazon|books on demand|'
    r'forgotten books|^unknown|^\[', re.IGNORECASE)

# A reprint recorded with the ISBN of its first printing: the catalogue keeps
# one such pair on purpose, for the order import to meet an ISBN two books share.
REPRINT_SUFFIX = ' (reprint)'


# ── Network, through the cache ─────────────────────────────────────────────────

_last_call = 0.0


def _get(url: str, *, data: bytes | None = None, accept: str = 'application/json') -> bytes:
    """One polite request: identified, at most two a second, retried on 429/5xx."""
    global _last_call
    for attempt in range(5):
        wait = 0.5 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        req = urllib.request.Request(url, data=data, headers={
            'User-Agent': USER_AGENT, 'Accept': accept})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(5 * (attempt + 1))
        except urllib.error.URLError:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'giving up on {url}')


def cached_json(name: str, url: str, *, data: bytes | None = None, accept='application/json'):
    path = CACHE / f'{name}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    print(f'  fetch {name}')
    result = json.loads(_get(url, data=data, accept=accept))
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


# ── Open Library ───────────────────────────────────────────────────────────────

SEARCH_FIELDS = ','.join([
    'key', 'title', 'author_key', 'author_name', 'first_publish_year',
    'number_of_pages_median', 'editions', 'editions.key', 'editions.title',
    'editions.isbn', 'editions.publisher', 'editions.publish_date',
    'editions.language', 'editions.cover_i',
])


def search(q: str, lang: str, limit: int, name: str) -> list[dict]:
    """Works matching `q` with an edition in the language, most read first."""
    query = urllib.parse.urlencode({
        'q': f'{q} language:{MARC[lang]}',
        'lang': lang, 'sort': 'readinglog', 'limit': limit, 'fields': SEARCH_FIELDS})
    result = cached_json(f'search-{name}-{lang}-{limit}',
                         f'https://openlibrary.org/search.json?{query}')
    return result.get('docs', [])


def isbn13(values: list[str]) -> str | None:
    for v in values or []:
        if len(v) == 13 and v.isdigit() and v[:3] in ('978', '979'):
            return v
    return None


def parse_date(text: str | None) -> str | None:
    """An edition's free-text date as ISO, the first of the year when only the year is known."""
    if not text:
        return None
    text = text.strip().rstrip('.')
    for fmt in ('%Y-%m-%d', '%B %d, %Y', '%b %d, %Y', '%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    for fmt in ('%B %Y', '%b %Y', '%Y-%m'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'\b(1[5-9]\d\d|20\d\d)\b', text)
    return f'{m.group(1)}-01-01' if m else None


def pick_edition(doc: dict, lang: str) -> dict | None:
    """The edition the search chose, when it is usable as a catalogue record."""
    eds = (doc.get('editions') or {}).get('docs') or []
    if not eds:
        return None
    ed = eds[0]
    if MARC[lang] not in (ed.get('language') or []):
        return None
    if not isbn13(ed.get('isbn')) or not ed.get('publisher') or not doc.get('author_key'):
        return None
    if NOT_PUBLISHERS.search(ed['publisher'][0]):
        return None
    return ed


def collect_books() -> list[dict]:
    books, seen_works, seen_isbn = [], set(), set()

    def take(docs, lang, tag, quota) -> int:
        taken = 0
        for doc in docs:
            if taken == quota:
                break
            if doc['key'] in seen_works:
                continue
            ed = pick_edition(doc, lang)
            if ed is None:
                continue
            isbn = isbn13(ed['isbn'])
            if isbn in seen_isbn:
                continue
            seen_works.add(doc['key'])
            seen_isbn.add(isbn)
            taken += 1
            books.append(_book(doc, ed, lang, tag))
        return taken

    for author in ITALIAN_AUTHORS:
        docs = search(f'author:"{author}"', 'it', 20, 'author-' + re.sub(r'\W+', '_', author.lower()))
        print(f'it {author:30} {take(docs, "it", "italian literature", PER_AUTHOR)}/{PER_AUTHOR}')
    for lang, quota in QUOTAS.items():
        limit = quota * (4 if lang in ('en', 'it') else 10)
        for tag, subject in GENRES:
            taken = take(search(f'subject_key:{subject}', lang, limit, subject), lang, tag, quota)
            print(f'{lang} {tag:20} {taken}/{quota}')
    return books


def _book(doc: dict, ed: dict, lang: str, tag: str) -> dict:
    title = ed.get('title') or doc['title']
    return {
        'title': title.strip(),
        'original_title': doc['title'].strip()
        if doc['title'].strip().casefold() != title.strip().casefold() else '',
        'isbn': isbn13(ed['isbn']),
        'publication_date': parse_date((ed.get('publish_date') or [None])[0]),
        'first_published': doc.get('first_publish_year') or '',
        'language': lang,
        'pages': doc.get('number_of_pages_median') or '',
        'publisher': ed['publisher'][0].strip(),
        'tags': f"{tag}, {'fiction' if tag in FICTION else 'non-fiction'}",
        'author_keys': doc['author_key'][:MAX_AUTHORS],
        'author_names': doc['author_name'][:MAX_AUTHORS],
        'cover_id': ed.get('cover_i') or '',
    }


# ── Wikidata ───────────────────────────────────────────────────────────────────

SPARQL = """
SELECT ?ol ?item ?label ?family ?birth ?birthPrec ?death ?deathPrec ?demonym ?country WHERE {
  VALUES ?ol { %s }
  ?item wdt:P648 ?ol .
  OPTIONAL { ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }
  OPTIONAL { ?item wdt:P734 ?f . ?f rdfs:label ?family . FILTER(LANG(?family) = "en") }
  OPTIONAL { ?item p:P569 ?bs . ?bs a wikibase:BestRank .
             ?bs psv:P569 [ wikibase:timeValue ?birth; wikibase:timePrecision ?birthPrec ] }
  OPTIONAL { ?item p:P570 ?ds . ?ds a wikibase:BestRank .
             ?ds psv:P570 [ wikibase:timeValue ?death; wikibase:timePrecision ?deathPrec ] }
  OPTIONAL { ?item wdt:P27 ?c .
             ?c rdfs:label ?country . FILTER(LANG(?country) = "en")
             OPTIONAL { ?c wdt:P1549 ?demonym . FILTER(LANG(?demonym) = "en") } }
}
"""


def wikidata_authors(keys: list[str]) -> dict[str, dict]:
    """Per Open Library author id: the Wikidata id, family name, dates, nationality."""
    found: dict[str, dict] = {}
    for start in range(0, len(keys), 150):
        chunk = keys[start:start + 150]
        digest = hashlib.sha1(' '.join(chunk).encode()).hexdigest()[:12]
        body = urllib.parse.urlencode({
            'query': SPARQL % ' '.join(f'"{k}"' for k in chunk)}).encode()
        result = cached_json(f'wikidata-{digest}', 'https://query.wikidata.org/sparql',
                             data=body, accept='application/sparql-results+json')
        rows: dict[str, list] = {}
        for b in result['results']['bindings']:
            rows.setdefault(b['ol']['value'], []).append(b)
        for ol, bindings in rows.items():
            found[ol] = _author_facts(bindings)
    return found


def _wd_date(bindings: list[dict], name: str) -> str:
    """The most precise of the stated dates, the earliest among equals."""
    stated = set()
    for b in bindings:
        if name not in b:
            continue
        value, precision = b[name]['value'], int(b[f'{name}Prec']['value'])
        if value.startswith('-') or precision < 9:   # before Christ, or vaguer than a year
            continue
        day = value[1:11] if value.startswith('+') else value[:10]
        stated.add((-min(precision, 11), day if precision >= 11 else f'{day[:4]}-01-01'))
    return min(stated)[1] if stated else ''


def _author_facts(bindings: list[dict]) -> dict:
    """One author from the rows of the join; several values are settled in a stable order."""
    def first(values):
        values = sorted(v for v in values if v)
        return values[0] if values else ''

    qids = {b['item']['value'].rsplit('/', 1)[1] for b in bindings}
    nationalities = Counter(
        (b.get('demonym') or b.get('country') or {}).get('value') for b in bindings)
    nationalities.pop(None, None)
    return {
        'wikidata': first(qids),
        'label': first(b.get('label', {}).get('value') for b in bindings),
        'family': sorted({b['family']['value'] for b in bindings if 'family' in b}),
        'birth_date': _wd_date(bindings, 'birth'),
        'death_date': _wd_date(bindings, 'death'),
        'nationality': min(nationalities, key=lambda n: (-nationalities[n], n))
        if nationalities else '',
    }


def uninvert(name: str) -> str:
    """'Pavese, Cesare.' as 'Cesare Pavese', the catalogue form some records use."""
    m = re.fullmatch(r'([^,]+),\s*([^,]+?)\.?', name.strip())
    return f'{m[2]} {m[1]}' if m else name


def split_name(name: str, families: list[str]) -> tuple[str, str]:
    """First and last name; Wikidata's family names decide where to cut when they can,
    two of them together for a Spanish double surname."""
    name = ' '.join(name.split())
    candidates = families + [f'{a} {b}' for a in families for b in families if a != b]
    for family in sorted(candidates, key=len, reverse=True):
        if name.endswith(' ' + family):
            return name[:-len(family) - 1], family
    if ' ' not in name:
        return '', name
    first, last = name.rsplit(' ', 1)
    return first, last


# ── Output ─────────────────────────────────────────────────────────────────────

def price_for(isbn: str, pages) -> str:
    """A believable price, the same for the same book on every run."""
    h = int(hashlib.sha1(isbn.encode()).hexdigest(), 16)
    base = 7 + int(pages or 250) * 0.025 + h % 6
    return f'{min(int(base), 48)}.{"90" if h % 3 else "50"}'


# Words that name the company, not the imprint: 'Penguin Books' is 'Penguin'.
_COMPANY = re.compile(
    r'\b(inc|ltd|llc|co|corp|company|s\.?p\.?a|s\.?r\.?l|gmbh|ag|sa|editore|editori|'
    r'edizioni|ediciones|editorial|éditions|editions|verlag|books|publishing|publishers)\b\.?')


# Spellings of one publisher the rule above does not join, by key.
_ALIASES = {'amondadori': 'mondadori', 'arnoldomondadori': 'mondadori'}


def _publisher_key(name: str) -> str:
    key = _COMPANY.sub(' ', name.casefold())
    key = re.sub(r'[^a-z0-9àèéìòù]', '', key) or re.sub(r'[^a-z0-9]', '', name.casefold())
    return _ALIASES.get(key, key)


def write_csv(path: Path, fields: list[str], rows: list[dict]):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f'wrote {path.name}: {len(rows)} rows')


def build_catalogue():
    books = collect_books()

    # Publishers: one per spelling up to case and punctuation, named as most books name it.
    spellings: dict[str, Counter] = {}
    for b in books:
        spellings.setdefault(_publisher_key(b['publisher']), Counter())[b['publisher']] += 1
    publisher_rows, publisher_id = [], {}
    for n, key in enumerate(sorted(spellings, key=lambda k: spellings[k].most_common(1)[0][0].casefold()), 1):
        publisher_id[key] = n
        publisher_rows.append({'id': n, 'name': spellings[key].most_common(1)[0][0],
                               'country': '', 'website': ''})

    # Authors. Open Library has some people twice: one author per full name, as
    # the table wants it, the record that knows more winning.
    names: dict[str, str] = {}
    for b in books:
        for k, name in zip(b['author_keys'], b['author_names']):
            names.setdefault(k, name)
    facts = wikidata_authors(list(names))
    person, by_name = {}, {}            # OL key -> merged record
    for key, name in names.items():
        info = facts.get(key, {})
        first, last = split_name(info.get('label') or uninvert(name), info.get('family', []))
        row = {
            'first_name': first, 'last_name': last,
            'birth_date': info.get('birth_date', ''), 'death_date': info.get('death_date', ''),
            'nationality': info.get('nationality', ''), 'wikidata': info.get('wikidata', ''),
        }
        same = by_name.setdefault((first.casefold(), last.casefold()), row)
        if same is not row:
            for field, value in row.items():
                same[field] = same[field] or value
        person[key] = same

    # A book's authors: the first always kept, the others unless they are no
    # author (a role in the name, or neither a first name nor a Wikidata id);
    # who is on Wikidata goes first, so a translator listed before the writer
    # moves behind. Ids follow the first appearance among the authors kept.
    def credited(b) -> list[dict]:
        kept = []
        for i, key in enumerate(b['author_keys']):
            row = person[key]
            if i and (NOT_AUTHORS.search(names[key])
                      or not (row['first_name'] or row['wikidata'])):
                continue
            if all(row is not k for k in kept):
                kept.append(row)
        return sorted(kept, key=lambda r: not r['wikidata'])

    author_rows = []
    book_authors = []
    for b in books:
        rows = credited(b)
        for row in rows:
            if 'id' not in row:
                row['id'] = len(author_rows) + 1
                author_rows.append(row)
        book_authors.append(' '.join(str(r['id']) for r in rows))
    author_rows = [{'id': r.pop('id'), **r} for r in author_rows]

    book_rows = []
    for n, (b, authors) in enumerate(zip(books, book_authors), 1):
        book_rows.append({
            'id': n, 'title': b['title'], 'original_title': b['original_title'],
            'isbn': b['isbn'], 'publication_date': b['publication_date'] or '',
            'first_published': b['first_published'],
            'price': price_for(b['isbn'], b['pages']), 'language': b['language'],
            'pages': b['pages'], 'publisher_id': publisher_id[_publisher_key(b['publisher'])],
            'tags': b['tags'], 'authors': authors, 'cover_id': b['cover_id'],
        })

    # The deliberate shared ISBN: a reprint of the first English book.
    source = next(r for r in book_rows if r['language'] == 'en')
    reprint = dict(source, id=len(book_rows) + 1, title=source['title'] + REPRINT_SUFFIX,
                   publication_date='', cover_id='')
    book_rows.append(reprint)

    write_csv(HERE / 'publishers.csv', ['id', 'name', 'country', 'website'], publisher_rows)
    write_csv(HERE / 'authors.csv', ['id', 'first_name', 'last_name', 'birth_date',
                                     'death_date', 'nationality', 'wikidata'], author_rows)
    write_csv(HERE / 'books.csv', list(book_rows[0]), book_rows)


# ── Order sheets ───────────────────────────────────────────────────────────────

def read_csv(name: str) -> list[dict]:
    with open(HERE / name, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def dashed(isbn: str) -> str:
    return f'{isbn[:3]}-{isbn[3:]}'


def misspelt(name: str) -> str:
    """The name with two letters swapped, the slip of a hurried hand."""
    letters = [i for i, c in enumerate(name) if c.isalpha()]
    i = letters[len(letters) // 2]
    return name[:i] + name[i + 1] + name[i] + name[i + 2:]


def build_sheets():
    """The three sheets of the order import, made from the catalogue as it is."""
    from openpyxl import Workbook

    books = read_csv('books.csv')
    publishers = {p['id']: p['name'] for p in read_csv('publishers.csv')}
    reprint = next(b for b in books if b['title'].endswith(REPRINT_SUFFIX))
    shared = reprint['isbn']
    by_pub: dict[str, list[dict]] = {}
    for b in books:
        if b['isbn'] != shared:
            by_pub.setdefault(b['publisher_id'], []).append(b)
    # The two publishers with most books: the first is also the one the seed orders from.
    first, second = sorted(by_pub, key=lambda p: (-len(by_pub[p]), int(p)))[:2]
    a, b = by_pub[first], by_pub[second]

    def save(name, header, rows):
        wb = Workbook()
        ws = wb.active
        ws.append(header)
        for r in rows:
            ws.append(r)
        wb.save(HERE / name)
        print(f'wrote {name}: {len(rows)} rows')

    header = ['order', 'publisher', 'isbn', 'title', 'quantity', 'price', 'discount']
    pa, pb = publishers[first], publishers[second]
    save('purchase_orders.xlsx', header, [
        ['A1', pa, dashed(a[0]['isbn']), a[0]['title'], 5, float(a[0]['price']), 10],
        ['A1', pa, a[1]['isbn'], a[1]['title'], 3, float(a[1]['price']), 10],
        ['A1', pa, dashed(a[2]['isbn']), a[2]['title'], 4, float(a[2]['price']), None],
        ['A2', pb, dashed(b[0]['isbn']), b[0]['title'], 2, float(b[0]['price']), 5],
        ['A2', pb, dashed(b[1]['isbn']), b[1]['title'], 6, None, None],
    ])
    save('purchase_orders_errors.xlsx', header, [
        ['B1', pa, dashed(a[0]['isbn']), a[0]['title'], 5, float(a[0]['price']), 10],
        ['B1', pa, '978-0000000000', 'Nobody wrote this', 3, 9.99, None],
        ['B1', pa, dashed(a[2]['isbn']), a[2]['title'], 'four', float(a[2]['price']), None],
        ['B1', pa, dashed(shared), 'Two books, one ISBN', 1, None, None],
        ['B2', misspelt(pb), dashed(b[0]['isbn']), b[0]['title'], 2, float(b[0]['price']), 5],
        ['B2', misspelt(pb), dashed(b[1]['isbn']), b[1]['title'], 6, None, 120],
    ])
    save('publisher_order.xlsx', ['EAN', 'Description', 'Qty', 'Unit price', 'Disc.', 'Notes'], [
        [a[0]['isbn'], f"{a[0]['title']} - paperback", 10, float(a[0]['price']), '15%', None],
        [a[3]['isbn'], a[3]['title'], 4, float(a[3]['price']), '15%', 'reprint'],
        [a[4]['isbn'], a[4]['title'], 4, float(a[4]['price']), '10%', None],
    ])


# ── Covers ─────────────────────────────────────────────────────────────────────

def fetch_covers():
    """Medium covers by ISBN into the media root `books`, the ones not there yet."""
    COVERS.mkdir(parents=True, exist_ok=True)
    books = [b for b in read_csv('books.csv') if b['cover_id']]
    for n, b in enumerate(books, 1):
        target = COVERS / f"{b['isbn']}.jpg"
        if target.exists():
            continue
        print(f"  cover {n}/{len(books)} {b['isbn']}")
        target.write_bytes(_get(f"https://covers.openlibrary.org/b/id/{b['cover_id']}-M.jpg",
                                accept='image/jpeg'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--sheets', action='store_true', help='only the order sheets, from the CSV')
    parser.add_argument('--covers', action='store_true', help='only the covers, from the CSV')
    args = parser.parse_args()
    if args.covers:
        fetch_covers()
        return 0
    if not args.sheets:
        build_catalogue()
    build_sheets()
    return 0


if __name__ == '__main__':
    sys.exit(main())
