from datetime import datetime, timezone
import pytest
from arbicore.performance import summarize

NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def test_profit_decreases_after_loss_and_breakeven_is_not_a_win():
    rows = [("2026-09-13T10:00:00Z", 100, "filled")]
    assert summarize(rows, NOW)["total_profit"] == 100
    rows += [("2026-09-13T11:00:00Z", -16, "closed"), ("2026-09-13T11:30:00Z", 0, "filled")]
    result = summarize(rows, NOW)
    assert result["total_profit"] == result["today_profit"] == 84
    assert result["wins"] == result["losses"] == result["breakeven"] == 1
    assert result["win_rate"] == pytest.approx(100 / 3)


def test_daily_cutoff_uses_utc_and_decimals():
    result = summarize([("2026-09-13T01:00:00+05:00", 50, "filled"),
                        ("2026-09-13T00:00:00Z", .1, "filled"),
                        ("2026-09-13T00:01:00Z", .2, "filled")], NOW)
    assert result["today_profit"] == .3
    assert result["total_profit"] == 50.3
    assert result["today_trades"] == 2


@pytest.mark.parametrize("profit,status,stamp", [
    (999, "open", "2026-09-13"), (999, "partial", "2026-09-13"),
    (999, None, "2026-09-13"), ("NaN", "filled", "2026-09-13"),
    (None, "filled", "2026-09-13"), (True, "filled", "2026-09-13"),
    (999, "filled", "bad date"), (999, "filled", "2026-09-14"),
])
def test_unfinished_or_invalid_evidence_does_not_inflate_win_rate(profit, status, stamp):
    result = summarize([(stamp, profit, status)], NOW)
    assert result["total_profit"] == result["win_rate"] == result["total_trades"] == 0
    assert result["excluded_trades"] == 1
