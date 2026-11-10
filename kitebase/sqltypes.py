"""
The column types of the core itself, beside those of SQLAlchemy.
"""
from typing import Any, List, Optional

import sqlalchemy.types


class CaseString(sqlalchemy.types.TypeDecorator):
    """String normalised to one case on its way to the database.

    Declared as `case: upper` (or `lower`) on a column or on a type; without it
    a plain String is generated, which is the neutral default.

    Normalising in the type means every write goes through it — ORM, Core, bulk
    — and so do the parameters of a query, so a lower-case search matches an
    upper-case value. The object in memory keeps what was assigned to it until
    it is refreshed; the endpoints commit and re-read, so what an API call
    returns is the normalised value.
    """

    impl = sqlalchemy.types.String
    cache_ok = True

    def __init__(self, *args: Any, case: str = 'upper', **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.case = case

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if not isinstance(value, str):
            return value
        return value.upper() if self.case == 'upper' else value.lower()


# The character that wraps every code of a CodeSet, on both sides.
CODESET_DELIMITER = ','


class CodeSet(sqlalchemy.types.TypeDecorator):
    """A set of codes from a small vocabulary: a list in Python, a delimited
    string in the database.

    `["C", "S"]` is stored as `",C,S,"`. The delimiters on both sides are what
    make "contains" one LIKE that is the same on every engine (`%,C,%`), and
    they keep `,C,` from ever matching inside `,CO,`, which packed characters
    would not. An empty list is stored as NULL and read back as an empty list.

    The vocabulary is not here: what a code means, and which codes exist, is
    the business of the plugin that declares the column (a table of roles, a
    list in YAML). A code cannot contain the delimiter, and cannot be empty.

    A string passes through as it is: it is how a LIKE pattern reaches the
    column, and how a value already in the stored form is written back. The
    list read from a row is a plain list, so a change in place goes unseen by
    the ORM: assign a new list instead.
    """

    impl = sqlalchemy.types.String
    cache_ok = True
    # What tells the querybuilder this column answers `has`
    delimiter = CODESET_DELIMITER

    @property
    def python_type(self) -> type:
        return list

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None or isinstance(value, str):
            return value
        return codeset_text(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return codeset_codes(value)


def codeset_text(codes: Any) -> Optional[str]:
    """The stored form of a list of codes: `",C,S,"`, or None when empty.

    Duplicates are dropped and the order kept, so the same set is always
    written the same way.
    """
    if isinstance(codes, str) or not isinstance(codes, (list, tuple, set, frozenset)):
        raise ValueError(f"A code set is a list of codes, not {codes!r}")
    out: List[str] = []
    for code in codes:
        if not isinstance(code, str) or not code or CODESET_DELIMITER in code:
            raise ValueError(
                f"A code of a code set is a non-empty string without "
                f"'{CODESET_DELIMITER}': {code!r}")
        if code not in out:
            out.append(code)
    if not out:
        return None
    return CODESET_DELIMITER + CODESET_DELIMITER.join(out) + CODESET_DELIMITER


def codeset_codes(text: Optional[str]) -> List[str]:
    """The list of codes of a stored `",C,S,"`; an empty list for NULL."""
    if not text:
        return []
    return [c for c in text.split(CODESET_DELIMITER) if c]
