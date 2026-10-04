"""
Write transforms — values the server rewrites on their way into the database.

A column marked `secret: true` never travels back to a client: the read paths
(`serialize_model`, the query `select`) drop it, and the auto-generated
descriptors already skip it. That says how a value is protected in transit, not
how it must be stored — `on_write` names a transform the CRUD endpoints apply
before writing, so the stored form is declared in YAML while the mechanism
lives here:

    Password:
      base: String
      secret: true            # never read back
      on_write: password_hash # stored hashed

Because a secret is never sent back, the client cannot show its current value,
so an empty value arriving for it means "unchanged" and is dropped rather than
written — otherwise opening a user and saving would wipe the password.

One rule covers stored values that predate hashing: a password is converted
**when it is written**, never as a side effect of a login. A database another
system also authenticates against therefore keeps working, and each account
converts the day its owner deliberately changes their password — a change that
is wanted, visible, and one person at a time.

Scope — what belongs here and what does not
-------------------------------------------
This is a *one-way, per-column, value-level* transform applied at the HTTP
boundary: what the client sent is not yet in the form it must be stored in.
Two families fit — irreversible ones (a password) and server-authoritative
normalisations that are read back normalised (canonical case, a trimmed code).

Reshaping a value between the wire and the model — a structure the client
renders one way and the database stores another — is *symmetric* and does not
belong here: it needs the way out as well, and half a mechanism scatters the
other half between the client and the descriptors. Symmetric conversions
between a Python value and its stored column belong one layer below, in a
SQLAlchemy TypeDecorator (see CaseString in db.py), which covers every consumer
of the models — endpoints, batch jobs, a host application's own pages — not
just HTTP requests. A password cannot live there: bcrypt salts each call, so
binding would produce a different hash every write and nothing could reverse it
on read.

When a real symmetric case turns up, `on_read` joins this registry and an entry
becomes a pair instead of a single callable — a backwards-compatible addition,
which is why it is not built ahead of a use for it.

`password_hash` is built in rather than living in a commons plugin because the
core is its consumer: the `auth` endpoint verifies credentials, so it has to
know how they were stored. Apps register their own transforms with
register_write_transform() — same spirit as add_query_behavior.
"""
import re
from typing import Callable, Dict, Optional, Set

import bcrypt

# Prefixes of the bcrypt variants, used to tell a hash from a legacy plaintext
# value. Anything else stored in a password column predates hashing.
_BCRYPT_PREFIXES = ('$2a$', '$2b$', '$2x$', '$2y$')

# Digests this code cannot verify but must not compare literally either — a
# crypt-style scheme ($argon2id$…, $6$…) or a bare hex digest, as a legacy
# import or another tool may well have written. Comparing one of these as if it
# were plaintext would let whoever read the column log in by typing the digest.
_FOREIGN_DIGEST = re.compile(r'^\$[A-Za-z0-9._-]+\$|^[0-9a-fA-F]{32,128}$')


def hash_password(plain: str) -> str:
    """
    Hash a password for storage.

    Raises:
        ValueError: if the password exceeds bcrypt's 72-byte limit — truncating
            silently would make two different passwords interchangeable.
    """
    encoded = plain.encode('utf-8')
    if len(encoded) > 72:
        raise ValueError('Password is too long (max 72 bytes)')
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode('utf-8')


def is_hashed(stored: Optional[str]) -> bool:
    """True if the stored value is a hash rather than a legacy plaintext one."""
    return bool(stored) and stored.startswith(_BCRYPT_PREFIXES)


def verify_password(plain: str, stored: Optional[str]) -> bool:
    """
    Check a password against its stored form.

    Accepts legacy plaintext values, so a database written before hashing keeps
    working and is left as it is: a password becomes hashed when it is written,
    never as a side effect of a login. An installation where another system
    reads the same column therefore keeps working until someone deliberately
    changes their password there — a conversion that is visible, wanted, and one
    account at a time.

    A stored value that looks like a digest of some other scheme fails rather
    than taking the plaintext path: comparing it literally would let anyone who
    read the column log in by typing the digest itself. Such an account needs
    its password set again.
    """
    if not stored or not plain:
        return False

    if is_hashed(stored):
        try:
            return bcrypt.checkpw(plain.encode('utf-8'), stored.encode('utf-8'))
        except ValueError:
            return False

    if _FOREIGN_DIGEST.match(stored):
        return False

    # Legacy plaintext
    return stored == plain


# Registry: name used in YAML as `on_write:` -> callable applied to the value.
_TRANSFORMS: dict = {
    'password_hash': hash_password,
}


def register_write_transform(name: str, func: Callable) -> None:
    """Register a write transform, referenced in YAML as `on_write: <name>`."""
    _TRANSFORMS[name] = func


def get_write_transform(name: str) -> Optional[Callable]:
    """Return the transform for a name, or None if unknown."""
    return _TRANSFORMS.get(name)


def transform_names() -> Set[str]:
    """All registered transform names."""
    return set(_TRANSFORMS)


# ── Validators ───────────────────────────────────────────────────────────────
#
# `validate: <name>` on a column (or on its type) names a rule the value must
# pass on its way in, before any transform: a password long enough, an email
# with an @. The CRUD endpoints apply it and answer with the errors by field, so
# a form shows each one under the field it is about.
#
# A validator is `func(value, values) -> Optional[str]`: the message when the
# value breaks the rule, None when it passes. `values` is everything written in
# the same call, for a rule that looks at a neighbour (a password that must not
# contain the username). An empty value is not validated: whether it may be
# empty is `nullable`'s business.

class ValidationError(ValueError):
    """Values that break a rule their column declares, as {field: message}."""

    def __init__(self, errors: Dict[str, str]):
        self.errors = errors
        super().__init__('; '.join(f'{field}: {message}' for field, message in errors.items()))


def email_validator(value, values) -> Optional[str]:
    """An address with something on both sides of a single @, and no spaces.

    Deliberately loose: the only proof that an address works is a message that
    arrives, and a stricter pattern refuses real addresses (`poller@localhost`).
    """
    from kitebase.i18n import _
    if not _EMAIL.match(str(value)):
        return _('Not a valid email address')
    return None


_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+$')

_VALIDATORS: dict = {
    'email_validator': email_validator,
}


def register_validator(name: str, func: Callable) -> None:
    """Register a validator, referenced in YAML as `validate: <name>`."""
    _VALIDATORS[name] = func


def get_validator(name: str) -> Optional[Callable]:
    """Return the validator for a name, or None if unknown."""
    return _VALIDATORS.get(name)


def validator_names() -> Set[str]:
    """All registered validator names."""
    return set(_VALIDATORS)
