"""
A text the catalogue does not carry, fetched from a public source on request.

The bench for a call to the outside from a command of a list: the author's
biography from Wikipedia, through the author's Wikidata id; the book's
description from Open Library, through its ISBN. The command runs on the row
under the cursor and writes the record; a text already there is replaced only
once the person says so, the same command called again with `replace`.

One request at a time, with a short timeout, while the person waits: a batch
over the whole catalogue is a job, not a command.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import kitebase
from kitebase import access
from kitebase import errors
from kitebase.endpoints import endpoint

TIMEOUT = 10
USER_AGENT = 'kitebase-devtest/1.0 (https://github.com/kitebase)'


class NotFound(Exception):
    """The source answered, and has nothing for this record."""


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound() from e
        raise


def _fetch(table: str, data: dict, column: str, what: str, find) -> dict:
    """The common path: the record, the question when it has a text, the call, the write."""
    access.check_table(table, 'update')
    record_id = data.get('id')
    if record_id is None:
        raise errors.Invalid('Select a row first')
    app = kitebase.utils.get_app()
    with app.get_session() as session:
        access.guard_session(session)
        record = session.get(getattr(app.model, table), record_id)
        if record is None:
            raise errors.NotFound(f'No {table.lower()} {record_id}')
        if getattr(record, column) and not data.get('replace'):
            return {'status': 'success', 'data': {
                'action': 'endpoint', 'op': data['$op'],
                'params': {'id': record_id, 'replace': True},
                'confirm': f'{what} is already there. Replace it?'}}
        try:
            text, source = find(record)
        except NotFound:
            raise errors.NotFound(f'No {what.lower()} found') from None
        except LookupError as e:
            raise errors.Invalid(str(e)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, 'reason', None) or e
            raise errors.Unavailable(f'The source did not answer: {reason}', code=502) from None
        setattr(record, column, text)
        session.commit()
    return {'status': 'success', 'data': {
        'message': f'{what} from {source}', 'touched': [record_id]}}


# ── Author: Wikidata -> Wikipedia ──────────────────────────────────────────────

def _languages() -> list[str]:
    """The application's language first, English after it."""
    lang = kitebase.utils.get_app().plugins.config.get('locale') or 'en'
    return [lang] if lang == 'en' else [lang, 'en']


def _author_bio(author) -> tuple[str, str]:
    if not author.wikidata:
        raise LookupError('This author has no Wikidata id: a search by name is to come')
    qid = author.wikidata
    entity = _get_json(f'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json')
    sitelinks = entity['entities'][qid].get('sitelinks', {})
    for lang in _languages():
        link = sitelinks.get(f'{lang}wiki')
        if not link:
            continue
        # The introduction of the article, as plain text: the lead, whole.
        query = urllib.parse.urlencode({
            'action': 'query', 'prop': 'extracts', 'exintro': 1, 'explaintext': 1,
            'redirects': 1, 'titles': link['title'], 'format': 'json'})
        pages = _get_json(f'https://{lang}.wikipedia.org/w/api.php?{query}')['query']['pages']
        extract = next(iter(pages.values()), {}).get('extract', '').strip()
        if extract:
            return extract, f'Wikipedia ({lang})'
    raise NotFound()


@endpoint('author_fetch_bio', access=access.PER_TABLE)
def author_fetch_bio(data):
    """The list's command: the biography of the author under the cursor."""
    return _fetch('Author', {**data, '$op': 'author_fetch_bio'}, 'bio', 'Biography', _author_bio)


# ── Book: ISBN -> Open Library edition -> work ─────────────────────────────────

def _text(value) -> str:
    """Open Library writes a text as a string or as {type, value}."""
    return value.get('value', '') if isinstance(value, dict) else (value or '')


def _clean(text: str) -> str:
    """Without the notes Open Library appends: a rule, then sources and links."""
    text = re.split(r'\n\s*-{3,}', text)[0]
    text = re.sub(r'\(\[[^\]]*\]\[\d+\]\)', '', text)          # ([source][1])
    text = re.sub(r'^\s*\[\d+\]:.*$', '', text, flags=re.M)     # [1]: http://...
    return text.strip()


def _book_description(book) -> tuple[str, str]:
    isbn = (book.isbn or '').replace('-', '').replace(' ', '')
    if not isbn:
        raise LookupError('This book has no ISBN')
    edition = _get_json(f'https://openlibrary.org/isbn/{isbn}.json')
    text = _text(edition.get('description'))
    if not text and edition.get('works'):
        work = _get_json(f"https://openlibrary.org{edition['works'][0]['key']}.json")
        text = _text(work.get('description'))
    text = _clean(text)
    if not text:
        raise NotFound()
    return text, 'Open Library'


@endpoint('book_fetch_description', access=access.PER_TABLE)
def book_fetch_description(data):
    """The list's command: the description of the book under the cursor."""
    return _fetch('Book', {**data, '$op': 'book_fetch_description'}, 'description',
                  'Description', _book_description)
