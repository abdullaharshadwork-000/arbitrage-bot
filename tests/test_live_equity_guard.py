from decimal import Decimal
import pytest
from arbicore.config import Settings
from arbicore.risk import RiskManager, HALT_DRAWDOWN


def test_equity_drawdown_latches_without_trade_and_survives_recovery_restart():
    manager = RiskManager(Settings(max_daily_loss_usdt=10))
    manager.update_equity(100)
    manager.update_equity(90)
    assert manager.halted and manager.halt_limit == HALT_DRAWDOWN
    assert manager.realized_today == 0
    manager.update_equity(110)
    assert manager.halted
    restored = RiskManager(manager.settings)
    restored.restore_state(manager.export_state())
    assert restored.halted
    assert restored.peak_equity == 110


@pytest.mark.parametrize("value", [None, "", "bad", True, -1, Decimal("NaN"), Decimal("sNaN"), float("inf")])
def test_invalid_equity_does_not_erase_last_valid_mark(value):
    manager = RiskManager(Settings())
    manager.update_equity(100)
    with pytest.raises(ValueError):
        manager.update_equity(value)
    assert manager.equity == 100
    assert manager.peak_equity == 100
