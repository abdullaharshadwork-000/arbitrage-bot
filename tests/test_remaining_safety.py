"""Safety tests for remaining agent modules."""

from arbicore.kill_switch import KillSwitch
from arbicore.monte_carlo import run_monte_carlo
from arbicore.portfolio_alloc import PortfolioAllocator
from arbicore.promotion_gate import PromotionGate


def test_kill_switch_blocks():
    ks = KillSwitch()
    assert not ks.is_active()
    ks.trip("test", source="unit")
    assert ks.is_active()
    assert "kill_switch" in (ks.block_reason() or "")
    ks.clear(source="unit")
    assert not ks.is_active()


def test_promotion_gate_requires_human():
    gate = PromotionGate()
    result = gate.evaluate(
        "trend",
        "1.0",
        metrics={
            "trade_count": 100,
            "min_trades": 30,
            "max_drawdown_pct": 5,
            "max_drawdown_limit_pct": 15,
            "expectancy": 0.5,
        },
        flags={
            "backtest_pass": True,
            "walk_forward_pass": True,
            "stress_pass": True,
            "shadow_pass": True,
            "paper_pass": True,
            "human_approval": False,
        },
    )
    assert result.eligible is False
    result2 = gate.evaluate(
        "trend",
        "1.0",
        metrics={
            "trade_count": 100,
            "min_trades": 30,
            "max_drawdown_pct": 5,
            "max_drawdown_limit_pct": 15,
            "expectancy": 0.5,
        },
        flags={
            "backtest_pass": True,
            "walk_forward_pass": True,
            "stress_pass": True,
            "shadow_pass": True,
            "paper_pass": True,
            "human_approval": True,
        },
    )
    assert result2.eligible is True


def test_monte_carlo_runs():
    report = run_monte_carlo([1.0, -0.5, 0.8, -0.3, 1.2], paths=50, seed=1)
    assert report.paths == 50
    assert report.median_final_equity > 0


def test_portfolio_alloc_cash_floor():
    plan = PortfolioAllocator(min_cash=0.2).allocate(
        {"a": 2.0, "b": 1.0, "c": 0.5}
    )
    assert plan.cash_weight >= 0.2
    assert abs(sum(a.weight for a in plan.allocations) + plan.cash_weight - 1.0) < 0.02
