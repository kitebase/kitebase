"""The timezone an application declares, and the clock read through it.

What is being protected here is not the arithmetic — `ZoneInfo` does that — but
the two properties that make declaring a timezone worth anything: that a
process whose clock disagrees does not start, and that an application which
declares nothing behaves exactly as it did before. Between them sits the whole
reason the module exists: a naive datetime carries no offset, so a wrong
timezone does not produce an error, it produces plausible numbers that are off
by hours, mixed into the same column as the right ones and indistinguishable
from them afterwards.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from kitebase import apptime


@pytest.fixture(autouse=True)
def forget_the_declaration():
    """The declaration is process-wide: leave it as it was found."""
    yield
    apptime.set_app_timezone(None)


# ── Declaring ────────────────────────────────────────────────────────────────

def test_nothing_declared_is_the_process_clock():
    apptime.set_app_timezone(None)

    assert apptime.app_timezone_name() is None
    assert abs((apptime.now() - datetime.now()).total_seconds()) < 1


def test_a_declared_zone_answers_in_that_zone():
    apptime.set_app_timezone('Asia/Tokyo')

    expected = datetime.now(ZoneInfo('Asia/Tokyo')).replace(tzinfo=None)
    assert abs((apptime.now() - expected).total_seconds()) < 1


def test_the_clock_is_naive_because_the_column_is():
    """What comes out has to be what goes into a DateTime column."""
    apptime.set_app_timezone('Europe/Rome')

    assert apptime.now().tzinfo is None


def test_an_unknown_zone_is_refused_at_the_declaration():
    with pytest.raises(ValueError, match='Mars/Olympus'):
        apptime.set_app_timezone('Mars/Olympus')


# ── The check ────────────────────────────────────────────────────────────────

def test_no_declaration_no_check():
    """Declaring a timezone *is* asking for the guarantee; saying nothing keeps
    the previous behaviour, whatever the machine is set to."""
    apptime.set_app_timezone(None)

    apptime.check_process_timezone()  # does not raise


def test_a_process_in_the_wrong_zone_does_not_start():
    """The scenario this exists for: a container rebuilt without TZ, running in
    UTC while the app is declared in Rome. Nothing else would notice."""
    apptime.set_app_timezone('Pacific/Kiritimati')  # +14, never the machine's

    with pytest.raises(RuntimeError) as e:
        apptime.check_process_timezone()

    message = str(e.value)
    assert 'Pacific/Kiritimati' in message
    assert 'TZ=Pacific/Kiritimati' in message   # says how to fix it


def test_the_process_zone_passes_against_itself():
    """Whatever this machine runs in, declaring that same zone must pass —
    otherwise the check would be unusable anywhere."""
    apptime.set_app_timezone(_local_zone_name())

    apptime.check_process_timezone()  # does not raise


def _local_zone_name():
    """An IANA zone whose current offset is this machine's, so the tests that
    need a *matching* declaration hold on any developer's box and in CI."""
    offset = datetime.now(timezone.utc).astimezone().utcoffset()
    for name in ('UTC', 'Europe/Rome', 'Europe/London', 'America/New_York',
                 'America/Chicago', 'America/Los_Angeles', 'America/Sao_Paulo',
                 'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai',
                 'Australia/Sydney', 'Pacific/Auckland', 'Pacific/Kiritimati',
                 'Pacific/Honolulu'):
        if datetime.now(ZoneInfo(name)).utcoffset() == offset:
            return name
    pytest.skip(f'no known zone at offset {offset}')


def test_the_check_compares_the_clock_and_not_the_name():
    """Rome and Berlin hold the same offsets and the same rules, and it is the
    offset that ends up in the column."""
    apptime.set_app_timezone('Europe/Rome')
    rome = apptime.now()
    apptime.set_app_timezone('Europe/Berlin')

    assert abs((apptime.now() - rome).total_seconds()) < 1


# ── Wired to config.yaml ─────────────────────────────────────────────────────

def _app(tmp_path, config):
    (tmp_path / 'plugins').mkdir()
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(config))
    return str(tmp_path / 'config.yaml')


def test_loading_a_config_declares_its_timezone(tmp_path):
    from kitebase.plugins import PluginsManager

    here = _local_zone_name()
    PluginsManager().load_config(_app(tmp_path, {'name': 'x', 'timezone': here}))

    assert apptime.app_timezone_name() == here


def test_loading_a_config_in_the_wrong_zone_fails_there(tmp_path):
    """The check runs where every entry point already passes — servers, tests,
    CLI, a host application — so none of them has to remember it."""
    from kitebase.plugins import PluginsManager

    with pytest.raises(RuntimeError, match='Timezone mismatch'):
        PluginsManager().load_config(
            _app(tmp_path, {'name': 'x', 'timezone': 'Pacific/Kiritimati'}))


def test_a_config_without_a_timezone_leaves_it_undeclared(tmp_path):
    from kitebase.plugins import PluginsManager

    apptime.set_app_timezone('Asia/Tokyo')
    PluginsManager().load_config(_app(tmp_path, {'name': 'x'}))

    assert apptime.app_timezone_name() is None


# ── $now, the system default ─────────────────────────────────────────────────

def test_now_is_registered_as_a_system_default():
    from kitebase import defaults

    assert 'now' in defaults.default_names()
    assert defaults.get_default('now') is defaults.now


def test_the_stamp_follows_the_declared_zone():
    """`default: $now` is the reason the module has a consumer: a column
    stamped through the app's timezone rather than the machine's."""
    from kitebase import defaults

    apptime.set_app_timezone('Asia/Tokyo')
    expected = datetime.now(ZoneInfo('Asia/Tokyo')).replace(tzinfo=None)

    assert abs((defaults.now() - expected).total_seconds()) < 1


def test_op_date_falls_back_to_the_organisations_today(monkeypatch):
    """Not `date.today()`, which is the machine's. A server running in UTC and
    an office in Rome disagree about the date for the first hours after
    midnight — which is when someone working late gets yesterday's date on
    today's document."""
    from datetime import date as date_type

    from kitebase import defaults
    from kitebase.db import BaseApp

    BaseApp.set_context(None)
    monkeypatch.setattr(apptime, 'today', lambda: date_type(1999, 12, 31))

    assert defaults.op_date() == date_type(1999, 12, 31)


def test_an_explicit_op_date_still_wins():
    from kitebase import defaults
    from kitebase.db import BaseApp

    apptime.set_app_timezone('Asia/Tokyo')
    with BaseApp.context({'op_date': '2026-03-15'}):
        assert defaults.op_date().isoformat() == '2026-03-15'
