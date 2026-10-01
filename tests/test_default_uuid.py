"""`$uuid`: a key minted on the server, for tables whose key no form fills."""
import re

from coframe import defaults

CANONICAL = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')


def test_uuid_is_registered_as_a_token():
    assert defaults.get_default('uuid') is defaults.uuid
    assert 'uuid' in defaults.default_names()


def test_uuid_is_canonical_lower_case_v4():
    key = defaults.uuid()
    assert len(key) == 36
    assert CANONICAL.match(key)


def test_every_call_mints_a_new_key():
    assert len({defaults.uuid() for _ in range(1000)}) == 1000
