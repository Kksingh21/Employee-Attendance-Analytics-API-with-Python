"""Pure-logic tests (no MongoDB needed): business rules R1-R5, rounding R8, serialisers."""
from datetime import date, datetime, timezone
from fractions import Fraction

import pytest

from app import main as m

UTC, IST = timezone.utc, m.IST
DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


def late(h, mi, s):
    d = date(2026, 7, 6)
    start, _ = m.shift_bounds(d, DAY)
    return m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), start)


def test_late_boundaries_from_spec():
    assert late(9, 40, 0) == 0      # exactly 10:00 -> not late
    assert late(9, 40, 1) == 10     # 10:01 -> late, floor(10.01)=10, measured from shift start
    assert late(10, 15, 59) == 45
    assert late(9, 0, 0) == 0       # early
    assert late(9, 29, 59) == 0


def test_second_truncation():
    assert m.from_ms(1783312500900) == m.from_ms(1783312500000)
    assert m.from_ms(1783312500900).microsecond == 0


def test_attendance_date_ist_not_utc():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th -> attendance date is the 7th for a day shift
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), DAY) == date(2026, 7, 7)


def test_overnight_date_and_bounds():
    # punch-in 02:00 IST on the 7th, shift 22:00-06:00 -> belongs to the shift of the 6th
    assert m.attendance_date(ist(2026, 7, 7, 2, 0), NIGHT) == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), NIGHT) == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 6, 0), NIGHT) == date(2026, 7, 7)  # not earlier than shift_end
    start, end = m.shift_bounds(date(2026, 7, 6), NIGHT)
    assert start == ist(2026, 7, 6, 22, 0) and end == ist(2026, 7, 7, 6, 0)


def test_overnight_sample_record_7():
    pin, pout = ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)
    f = m.derive(NIGHT | {}, date(2026, 7, 6), pin, pout)
    assert f == {"late_minutes": 0, "work_hours": 8.75, "overtime_minutes": 40, "half_day": False}


def test_overnight_late_after_midnight():
    # 22:00 shift, punches in at 00:15 next day -> 135 min late (still the 6th's record)
    pin = ist(2026, 7, 7, 0, 15)
    d = m.attendance_date(pin, NIGHT)
    start, _ = m.shift_bounds(d, NIGHT)
    assert d == date(2026, 7, 6) and m.compute_late_minutes(pin, start) == 135 - 0


@pytest.mark.parametrize("mins,expected", [(29, 0), (30, 30), (40, 40), (-5, 0)])
def test_overtime_threshold(mins, expected):
    _, end = m.shift_bounds(date(2026, 7, 6), DAY)
    from datetime import timedelta
    assert m.compute_overtime(end + timedelta(minutes=mins), end) == expected
    assert m.compute_overtime(end + timedelta(minutes=mins, seconds=59), end) == expected  # floor


def test_work_hours_half_up_and_half_day():
    pin = ist(2026, 7, 6, 9, 0)
    from datetime import timedelta
    # 4.495h is 16182 s -> 4.495 -> half-up 4.50 -> NOT a half day (R5 uses the rounded value)
    wh = m.compute_work_hours(pin, pin + timedelta(seconds=16182))
    assert str(wh) == "4.50"
    assert m.derive(DAY, date(2026, 7, 6), pin, pin + timedelta(seconds=16182))["half_day"] is False
    assert m.derive(DAY, date(2026, 7, 6), pin, pin + timedelta(seconds=16170))["half_day"] is True  # 4.4917 -> 4.49
    assert str(m.compute_work_hours(pin, pin + timedelta(seconds=18))) == "0.01"  # 0.005h tie rounds UP
    # banker's rounding would give 8.12 for 8.125h; half-up must give 8.13
    assert str(m.compute_work_hours(pin, pin + timedelta(seconds=29250))) == "8.13"


def test_round_half_up():
    assert m.round_half_up(8.015, 2) == 8.02
    assert m.round_half_up(Fraction(1, 32), 4) == 0.0313
    assert m.round_half_up(2.675, 2) == 2.68        # float round() gives 2.67
    assert m.round_half_up(Fraction(1, 3), 4) == 0.3333
    assert m.round_half_up(0.12345, 4) == 0.1235


def test_ms_conversion_roundtrip():
    ms = 1783312500000
    assert m.to_ms(m.from_ms(ms)) == ms
    naive = datetime(2026, 7, 6, 4, 35)  # naive treated as UTC
    assert m.to_ms(naive) == m.to_ms(naive.replace(tzinfo=UTC))


def test_weekdays_between():
    assert m.weekdays_between(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.weekdays_between(date(2026, 7, 13), date(2026, 7, 31)) == 15   # mid-month joiner
    assert m.weekdays_between(date(2026, 8, 1), date(2026, 8, 31)) == 21
    assert m.weekdays_between(date(2026, 9, 1), date(2026, 8, 31)) == 0


def test_record_out_legacy_and_history():
    rec = {"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
           "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC), "punch_out": None,
           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0}  # no half_day / history
    out = m.record_out(rec)
    assert out["half_day"] is False and out["history"] == [] and "_id" not in out
    rec["history"] = [{"at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC), "by": "hr", "reason": "x",
                       "changes": {"punch_in": {"from": datetime(2026, 7, 7, 4, 35, tzinfo=UTC), "to": None},
                                   "late_minutes": {"from": 35, "to": 0}}}]
    h = m.record_out(rec)["history"][0]
    assert isinstance(h["at"], int) and isinstance(h["changes"]["punch_in"]["from"], int)
    assert h["changes"]["punch_in"]["to"] is None and h["changes"]["late_minutes"] == {"from": 35, "to": 0}


def test_month_bounds():
    assert m.month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert m.month_bounds("2028-02")[1] == date(2028, 2, 29)
    with pytest.raises(Exception):
        m.month_bounds("2026-13")


def test_pipelines_are_serialisable_and_use_expected_stages():
    f, l = date(2026, 7, 1), date(2026, 7, 31)
    stages = lambda p: [next(iter(s)) for s in p]
    assert stages(m.monthly_pipeline("EMP0001", f, l)) == ["$match", "$group"]
    assert "$lookup" in stages(m.summary_pipeline(f, l, None))
    lb = stages(m.leaderboard_pipeline(f, l, None, 10))
    assert "$setWindowFields" in lb and lb.index("$match") == 0
    tr = stages(m.trend_pipeline("Engineering", f, l))
    assert "$densify" in tr and tr.count("$setWindowFields") == 2
    from bson import json_util
    for p in (m.trend_pipeline("E", f, l), m.summary_pipeline(f, l, "E"), m.leaderboard_pipeline(f, l, "E", 3)):
        json_util.dumps(p)  # must be BSON-encodable
