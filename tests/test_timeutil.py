from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from timeutil import (
    InvalidLocalTime,
    format_local,
    parse_api_datetime,
    parse_local,
    to_api,
    zone_for,
    zone_label,
)


def test_known_code_dst_correct():
    tz = zone_for("lax", "2027-06-12T10:00:00-07:00")
    assert to_api(parse_local("2027-01-15T10:00", tz), tz).endswith("-08:00")
    assert to_api(parse_local("2027-07-15T10:00", tz), tz).endswith("-07:00")


def test_unknown_code_uses_pickup_offset():
    tz = zone_for("ZZZ", "2027-06-12T10:00:00-05:00")
    assert parse_local("2027-12-01T09:00", tz).utcoffset() == timedelta(hours=-5)
    assert zone_label(tz) == "UTC-05:00"


def test_utc_input_converted():
    tz = zone_for("LAX", "2027-06-12T10:00:00-07:00")
    returned = parse_api_datetime("2027-06-15T21:00:00Z")
    assert format_local(returned, tz) == "2027-06-15T14:00"


def test_rejects_other_formats():
    for bad in ["2027-06-17 14:00", "June 17 2pm", "2027-06-17T14:00:00-07:00", ""]:
        with pytest.raises(InvalidLocalTime):
            parse_local(bad, ZoneInfo("America/Los_Angeles"))


def test_dst_gap_rejected_and_overlap_takes_earlier():
    tz = ZoneInfo("America/Los_Angeles")
    with pytest.raises(InvalidLocalTime):
        parse_local("2027-03-14T02:30", tz)
    assert parse_local("2027-11-07T01:30", tz).utcoffset() == timedelta(hours=-7)
