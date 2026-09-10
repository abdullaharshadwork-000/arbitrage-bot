import sqlite3
import time

import pytest

from arbicore.brackets import (ACKNOWLEDGEMENT, BracketJournal, ProtectedTestnetLifecycle,
                               validate_plan, strict_decimal)
from arbicore.exchange_signals import capabilities, native_plan, DemoDiagnostics


def plan():
    return {"symbol": "BTCUSDT", "quantity": "0.1", "protected_quantity": "0.1",
            "entry": "100", "stop": "95", "target": "110", "created_at": time.time()}


class Transport:
    def __init__(self):
        self.submissions = 0
        self.params = None
        self.timeout = False
        self.partial = False
        self.bad_fill = False
        self.bad_id = False
        self.no_fill = False
        self.exit_filled = False
        self.fee = "0"
        self.fee_asset = "BTC"
        self.trades_missing = False
        self.trade_duplicate = False

    def assert_testnet(self):
        pass

    def submit(self, params):
        self.submissions += 1
        self.params = params
        if self.timeout:
            raise TimeoutError()

    def query_list(self, identity):
        return {"listClientOrderId": identity, "symbol": "BTCUSDT", "orders": [
            {"symbol": "BTCUSDT", "clientOrderId": "ac" + role + identity[3:], "orderId": i}
            for i, role in enumerate(("E", "T", "S"), 1)]}

    def query_order(self, symbol, identity):
        entry = identity.startswith("acE")
        target = identity.startswith("acT")
        quantity = (self.params or {}).get("workingQuantity" if entry else "pendingQuantity", "0.1")
        filled = "0" if self.no_fill else "0.05" if self.partial and entry else quantity if entry or (self.exit_filled and target) else "0"
        status = "EXPIRED" if self.no_fill else "PARTIALLY_FILLED" if self.partial and entry else "FILLED" if entry or (target and self.exit_filled) else "CANCELED" if self.exit_filled else "NEW"
        from decimal import Decimal
        return {"symbol": symbol, "clientOrderId": "wrong" if self.bad_id else identity,
                "orderId": 1 if entry else 2 if target else 3,
                "side": "BUY" if entry else "SELL", "type": "LIMIT" if entry else "TAKE_PROFIT" if target else "STOP_LOSS",
                "timeInForce": "FOK", "price": "100", "stopPrice": "110" if target else "95",
                "origQty": quantity, "executedQty": None if self.bad_fill else filled,
                "cummulativeQuoteQty": str(Decimal(filled) * (100 if entry else 110)), "status": status}

    def query_trades(self, symbol, order_id):
        role = {"1": "E", "2": "T", "3": "S"}[order_id]
        order = self.query_order(symbol, "ac" + role + "unused")
        if order["executedQty"] == "0" or self.trades_missing:
            return []
        trade = {"symbol": symbol, "orderId": int(order_id), "id": int(order_id) * 10,
                 "isBuyer": role == "E", "qty": order["executedQty"], "quoteQty": order["cummulativeQuoteQty"],
                 "price": "100" if role == "E" else "110", "commission": self.fee if role == "E" else "0",
                 "commissionAsset": self.fee_asset}
        return [trade, trade.copy()] if self.trade_duplicate else [trade]


def controller(tmp_path):
    transport = Transport()
    journal = BracketJournal(tmp_path / "journal.db")
    return transport, journal, ProtectedTestnetLifecycle(transport, journal, "owner")


def test_ack_and_expired_plan_do_not_submit(tmp_path):
    t, j, c = controller(tmp_path)
    with pytest.raises(ValueError):
        c.submit(plan(), "")
    expired = plan()
    expired["created_at"] -= 11
    with pytest.raises(ValueError):
        c.submit(expired, ACKNOWLEDGEMENT)
    assert t.submissions == 0


def test_timeout_recovers_by_identity_and_restart_never_resubmits(tmp_path):
    t, j, c = controller(tmp_path)
    t.timeout = True
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    assert result["status"] == "protected"
    restarted = ProtectedTestnetLifecycle(t, BracketJournal(tmp_path / "journal.db"), "owner")
    assert restarted.reconcile(result["id"])["status"] == "protected"
    with pytest.raises(sqlite3.IntegrityError):
        restarted.submit(plan(), ACKNOWLEDGEMENT)
    assert t.submissions == 1
    with pytest.raises(ValueError):
        j.get("other-owner", result["id"])


@pytest.mark.parametrize("flag,status", [("partial", "recovery_required"), ("bad_fill", "reconciliation_required"),
                                        ("bad_id", "reconciliation_required"), ("no_fill", "no_fill")])
def test_no_guessed_fill_or_identity(tmp_path, flag, status):
    t, j, c = controller(tmp_path)
    setattr(t, flag, True)
    assert c.submit(plan(), ACKNOWLEDGEMENT)["status"] == status


@pytest.mark.parametrize("bad", [None, "", "garbage", float("nan"), float("inf"), True])
def test_exchange_numbers_are_strict(bad):
    with pytest.raises(ValueError):
        strict_decimal(bad)


def test_native_plans_are_venue_specific_and_no_kucoin_fallback():
    args = ("BTC/USDT", ".1", ".0999", "100", "95", "110", "unique1")
    assert native_plan("binance", *args)["workingTimeInForce"] == "FOK"
    assert native_plan("okx", *args)["attachAlgoOrds"][0]["slOrdPx"] == "-1"
    assert native_plan("bybit", *args)["category"] == "spot"
    with pytest.raises(ValueError, match="sandbox"):
        native_plan("kucoin", *args)
    rows = capabilities()
    assert {r["exchange"] for r in rows} == {"binance", "kucoin", "okx", "bybit"}
    assert all(r["paper_signals"] and not r["production_ready"] for r in rows)


@pytest.mark.parametrize("exchange", ["binance", "okx", "bybit"])
def test_demo_clients_never_accept_production_endpoint_mutation(exchange):
    diagnostic = DemoDiagnostics(exchange, "dummy", "dummy", "dummy")  # Constructor does no network I/O.
    if exchange == "okx":
        diagnostic.client.headers.pop("x-simulated-trading")
    else:
        diagnostic.client.urls["api"]["private"] = "https://api.binance.com/api/v3"
    with pytest.raises(ValueError, match="endpoint"):
        diagnostic.assert_demo()


def test_fee_adjusted_inventory_is_protected_then_closed(tmp_path):
    t, j, c = controller(tmp_path)
    p = plan()
    p["protected_quantity"] = "0.0999"
    t.fee = "0.0001"
    result = c.submit(p, ACKNOWLEDGEMENT)
    assert result["status"] == "protected"
    assert result["observation"]["accounting"]["remaining_base"] == "0.0999"
    assert result["observation"]["accounting"]["realized_pnl_usdt"] is None
    t.exit_filled = True
    result = c.reconcile(result["id"])
    assert result["status"] == "closed"
    assert result["observation"]["accounting"]["realized_pnl_usdt"] == "0.9890"
    assert j.active("owner") == []


def test_reserve_dust_is_not_discarded_to_unblock_another_entry(tmp_path):
    t, j, c = controller(tmp_path)
    p = plan()
    p["protected_quantity"] = "0.0998"
    t.fee, t.exit_filled = "0.0001", True
    result = c.submit(p, ACKNOWLEDGEMENT)
    assert result["status"] == "recovery_required"
    assert result["observation"]["accounting"]["remaining_base"] == "0.0001"
    assert result["observation"]["accounting"]["realized_pnl_usdt"] is None
    assert j.active("owner") == [result["id"]]
    with pytest.raises(sqlite3.IntegrityError):
        c.submit(p, ACKNOWLEDGEMENT)
    assert t.submissions == 1


@pytest.mark.parametrize("asset,expected,pnl", [("USDT", "closed", "0.99"),
                                               ("BNB", "valuation_required", None)])
def test_quote_and_external_fee_accounting(tmp_path, asset, expected, pnl):
    t, j, c = controller(tmp_path)
    t.fee, t.fee_asset, t.exit_filled = "0.01", asset, True
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    assert result["status"] == expected
    accounting = result["observation"]["accounting"]
    assert accounting["realized_pnl_usdt"] == pnl
    assert accounting["commissions"][asset] == "0.01"


@pytest.mark.parametrize("flag", ["trades_missing", "trade_duplicate"])
def test_incomplete_or_duplicate_trades_never_mean_zero_fees(tmp_path, flag):
    t, j, c = controller(tmp_path)
    setattr(t, flag, True)
    t.exit_filled = True
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    assert result["status"] == "reconciliation_required"
    assert j.active("owner") == [result["id"]]


@pytest.mark.parametrize("field,value", [("commission", None), ("commission", "NaN"), ("commission", "-1"),
    ("commissionAsset", ""), ("orderId", 99), ("id", True), ("isBuyer", "true"),
    ("qty", "0.09"), ("quoteQty", "9"), ("price", "0")])
def test_malformed_account_trades_fail_closed(tmp_path, field, value):
    t, j, c = controller(tmp_path)
    original = t.query_trades
    def changed(symbol, order_id):
        rows = original(symbol, order_id)
        if rows:
            rows[0][field] = value
        return rows
    t.query_trades = changed
    assert c.submit(plan(), ACKNOWLEDGEMENT)["status"] == "reconciliation_required"


def test_order_change_during_trade_fetch_requires_fresh_snapshot(tmp_path):
    t, j, c = controller(tmp_path)
    original = t.query_trades
    def changed(symbol, order_id):
        rows = original(symbol, order_id)
        if order_id == "3":
            t.exit_filled = True
        return rows
    t.query_trades = changed
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    assert result["status"] == "reconciliation_required"
    t.query_trades = original
    assert c.reconcile_active()[0]["status"] == "closed"
    assert t.submissions == 1


def test_outage_restart_recovery_is_read_only(tmp_path):
    t, j, c = controller(tmp_path)
    original = t.query_trades
    def unavailable(*args):
        raise TimeoutError("sensitive exchange details must not escape")
    t.query_trades = unavailable
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    assert result["observation"]["error_type"] == "TimeoutError"
    assert result["observation"]["orders"]["entry"]["filled"] == "0.1"
    assert result["observation"]["stale"] is True
    t.query_trades = original
    restarted = ProtectedTestnetLifecycle(t, BracketJournal(tmp_path / "journal.db"), "owner")
    assert restarted.reconcile_active()[0]["status"] == "protected"
    assert t.submissions == 1


def test_verified_account_binding_preserves_restart_lock(tmp_path):
    t, j, c = controller(tmp_path)
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    j.bind_legacy_owner("owner", "account:verified")
    assert j.active("owner") == []
    assert j.active("account:verified") == [result["id"]]
    rekeyed = ProtectedTestnetLifecycle(t, j, "account:verified")
    with pytest.raises(sqlite3.IntegrityError):
        rekeyed.submit(plan(), ACKNOWLEDGEMENT)
    assert t.submissions == 1


def test_conflicting_key_owner_migration_is_atomic(tmp_path):
    _, j, c = controller(tmp_path)
    first = j.create("key-one", plan())
    second = j.create("account:verified", plan())
    with pytest.raises(sqlite3.IntegrityError):
        j.bind_legacy_owner("key-one", "account:verified")
    assert j.active("key-one") == [first]
    assert j.active("account:verified") == [second]


def test_regressing_exchange_fills_cannot_release_account_lock(tmp_path):
    t, j, c = controller(tmp_path)
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    t.trades_missing = True
    assert c.reconcile(result["id"])["status"] == "reconciliation_required"
    t.no_fill = True
    assert c.reconcile(result["id"])["status"] == "reconciliation_required"
    assert j.active("owner") == [result["id"]]


def test_binance_pagination_is_complete_and_does_not_loop(monkeypatch):
    from arbicore.brackets import BinanceTestnetTransport
    t = BinanceTestnetTransport("dummy", "dummy")
    calls = []
    def pages(params):
        calls.append(dict(params))
        return [{"id": i} for i in range(1000)] if len(calls) == 1 else [{"id": 1000}]
    monkeypatch.setattr(t.client, "private_get_mytrades", pages)
    assert len(t.query_trades("BTCUSDT", "1")) == 1001
    assert calls[1]["fromId"] == 1000
    monkeypatch.setattr(t.client, "private_get_mytrades", lambda params: [{"id": i} for i in range(1000)])
    with pytest.raises(ValueError, match="progressing"):
        t.query_trades("BTCUSDT", "1")


def test_binance_account_identity_is_key_independent_and_requires_uid(monkeypatch):
    from arbicore.brackets import BinanceTestnetTransport
    first = BinanceTestnetTransport("dummy-one", "dummy")
    second = BinanceTestnetTransport("dummy-two", "dummy")
    for transport in (first, second):
        monkeypatch.setattr(transport.client, "private_get_account", lambda: {"uid": 12345, "accountType": "SPOT"})
    assert first.account_owner() == second.account_owner()
    assert "12345" not in first.account_owner()
    monkeypatch.setattr(second.client, "private_get_account", lambda: {"accountType": "SPOT"})
    with pytest.raises(ValueError):
        second.account_owner()


def test_binance_disk_failure_leaves_exchange_untouched(tmp_path, monkeypatch):
    t, j, c = controller(tmp_path)
    def unavailable(*args):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(j, "create", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        c.submit(plan(), ACKNOWLEDGEMENT)
    assert t.submissions == 0


def test_first_observed_fill_survives_missing_fee_history(tmp_path):
    t, j, c = controller(tmp_path)
    t.trades_missing = True
    result = c.submit(plan(), ACKNOWLEDGEMENT)
    assert result["status"] == "reconciliation_required"
    t.no_fill = True
    result = c.reconcile(result["id"])
    assert result["status"] == "reconciliation_required"
    assert result["observation"]["orders"]["entry"]["filled"] == "0.1"
    assert j.active("owner") == [result["id"]]


def test_binance_delayed_reconciliation_cannot_overwrite_newer_evidence(tmp_path):
    t, j, c = controller(tmp_path)
    identity = j.create("owner", plan())
    stale = j.get("owner", identity)
    assert j.update("owner", identity, "protected", {"newer_evidence": True}, expected=stale)
    assert not j.update("owner", identity, "no_fill", {}, expected=stale)
    assert j.get("owner", identity)["status"] == "protected"
