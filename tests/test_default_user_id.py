"""`$user_id`: the person writing the row, as the default of whoever answers for it."""
import kitebase.context
from kitebase import defaults


def test_user_id_is_registered_as_a_token():
    assert defaults.get_default('user_id') is defaults.user_id


def test_it_is_the_user_of_the_request_and_nobody_without_one():
    kitebase.context.replace({'id': 7, 'username': 'u7'})
    try:
        assert defaults.user_id() == 7
    finally:
        kitebase.context.replace({})
    assert defaults.user_id() is None
