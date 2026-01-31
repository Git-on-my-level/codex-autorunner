import re

from codex_autorunner.core.time_utils import now_iso, now_iso_utc_z


def test_now_iso_format():
    timestamp = now_iso()
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp)


def test_now_iso_utc_z_format():
    timestamp = now_iso_utc_z()
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp)


def test_now_iso_aliases():
    assert now_iso is now_iso_utc_z
