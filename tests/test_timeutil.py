from mapex.core import timeutil as tu
from tests.helpers import ny_ts


def test_killzones_and_lunch():
    assert tu.killzone(ny_ts(2026, 9, 22, 8, 47)) == "New York"
    assert tu.killzone(ny_ts(2026, 9, 22, 11, 30)) is None
    assert tu.killzone(ny_ts(2026, 9, 22, 12, 30)) is None  # NY Lunch
    assert tu.killzone(ny_ts(2026, 9, 22, 14, 20)) == "PM Silver Bullet"
    assert tu.killzone(ny_ts(2026, 9, 22, 3, 0)) == "London"
    assert tu.killzone(ny_ts(2026, 9, 22, 15, 55)) is None


def test_dst_march_and_november():
    # 2026 DST starts Mar 8, ends Nov 1. 08:30 NY is 13:30 UTC before, 12:30 UTC after the March switch.
    before, after = ny_ts(2026, 3, 6, 8, 30), ny_ts(2026, 3, 9, 8, 30)
    assert tu.utc(before).hour == 13 and tu.utc(after).hour == 12
    assert tu.killzone(before) == tu.killzone(after) == "New York"
    nov_before, nov_after = ny_ts(2026, 10, 30, 8, 30), ny_ts(2026, 11, 2, 8, 30)
    assert tu.utc(nov_before).hour == 12 and tu.utc(nov_after).hour == 13
    assert tu.ny_at(ny_ts(2026, 11, 1, 12), 0) == ny_ts(2026, 11, 1, 0, 0)


def test_market_hours_gold_and_btc():
    assert not tu.market_open("XAUUSD", ny_ts(2026, 9, 19, 12))  # Saturday
    assert not tu.market_open("XAUUSD", ny_ts(2026, 9, 18, 17, 30))  # Friday after close
    assert not tu.market_open("XAUUSD", ny_ts(2026, 9, 20, 17, 0))  # Sunday before 18:00
    assert tu.market_open("XAUUSD", ny_ts(2026, 9, 20, 18, 5))
    assert not tu.market_open("XAUUSD", ny_ts(2026, 9, 22, 17, 30))  # daily break
    assert tu.market_open("BTCUSD", ny_ts(2026, 9, 19, 12))
    assert tu.minutes_to_close("XAUUSD", ny_ts(2026, 9, 22, 16, 40)) == 20
    assert tu.minutes_to_close("BTCUSD", ny_ts(2026, 9, 22, 16, 40)) == float("inf")


def test_sessions_and_trading_day():
    assert tu.current_session(ny_ts(2026, 9, 22, 20)) == "Asia"
    assert tu.current_session(ny_ts(2026, 9, 22, 1)) == "Asia"
    assert tu.current_session(ny_ts(2026, 9, 22, 3)) == "London"
    assert tu.current_session(ny_ts(2026, 9, 22, 9)) == "New York"
    assert tu.trading_day(ny_ts(2026, 9, 22, 16, 59)) == "2026-09-22"
    assert tu.trading_day(ny_ts(2026, 9, 22, 17, 1)) == "2026-09-23"
    assert tu.week_start(ny_ts(2026, 9, 23, 10)) == ny_ts(2026, 9, 20, 18)


def test_macro_and_silver_bullet():
    assert tu.in_macro_window(ny_ts(2026, 9, 22, 9, 55))
    assert tu.in_macro_window(ny_ts(2026, 9, 22, 10, 5))
    assert not tu.in_macro_window(ny_ts(2026, 9, 22, 10, 30))
    assert tu.in_silver_bullet(ny_ts(2026, 9, 22, 10, 30))
