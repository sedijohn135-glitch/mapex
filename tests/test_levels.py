from mapex.core import levels as lv
from mapex.core.primitives import Bar
from tests.helpers import mk, ny_ts


def test_sweep_status_rules():
    later = mk([(99, 99.9, 98, 99.5)])
    assert lv.sweep_status(100, "high", later, 100)[0] == "UNTOUCHED"
    near = mk([(99, 99.98, 98, 99.5)])  # within 0.03 % of 100 = 0.03
    assert lv.sweep_status(100, "high", near, 100)[0] == "SWEPT_WICK"
    body = mk([(99, 100.5, 98, 100.2)])
    assert lv.sweep_status(100, "high", body, 100)[0] == "SWEPT_BODY"
    assert lv.sweep_status(100, "low", mk([(101, 102, 99.9, 101)]), 100)[0] == "SWEPT_WICK"


def test_equal_levels_at_point_one_percent():
    groups = lv.group_equal([(3390.0, 1), (3392.0, 5), (3400.0, 9)], price=3390)
    assert [len(g) for g in groups] == [2, 1]  # 2.0 <= 3.39 tolerance, 10 is not


def test_touch_count_needs_separation():
    bars = mk([(9, 10, 8, 9)] * 2 + [(9, 9.5, 8, 9)] * 2 + [(9, 10, 8, 9)] + [(9, 9.5, 8, 9)] * 3 + [(9, 10, 8, 9)])
    assert lv.touch_count(10, "high", bars, 0.01) == 3  # idx 0, (1 skipped), 4, 8


def test_session_atr_fallback_and_normal():
    now = ny_ts(2026, 9, 22, 9, 0)  # New York label session (07:00-19:00)
    h1 = mk([(100, 101, 99, 100)] * 20, start=now - 20 * 3600, tf="H1")
    val, fb = lv.session_atr([], h1, now)
    assert fb and val == 4 * 2.0
    m15 = []
    for d in range(1, 13):
        start = ny_ts(2026, 9, 22 - d, 7, 0)
        for k in range(48):  # 07:00 -> 19:00
            m15.append(Bar(start + k * 900, 100, 100 + d, 100 - d, 100))
    m15.sort()
    val, fb = lv.session_atr(m15, h1, now)
    assert not fb and val == sum(2 * d for d in range(1, 11)) / 10


def test_completed_sessions_extremes():
    now = ny_ts(2026, 9, 22, 11, 0)
    start = ny_ts(2026, 9, 22, 2, 0)
    m15 = [Bar(start + k * 900, 100, 100 + k, 100 - k, 100) for k in range(12)]  # London 02:00-05:00
    sess = {s.name: s for s in lv.completed_sessions(m15, now, days=1)}
    assert sess["London"].high == 111 and sess["London"].low == 89


def test_sessions_between():
    assert lv.sessions_between(ny_ts(2026, 9, 22, 8), ny_ts(2026, 9, 23, 8)) == 3
