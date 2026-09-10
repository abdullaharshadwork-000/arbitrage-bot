"""Deterministic exchange fixtures, not authenticated qualification evidence."""
import copy
import sqlite3
import time
from decimal import Decimal

import pytest

from arbicore import okx_demo as okx, bybit_demo as bybit
from arbicore.brackets import BinanceTestnetTransport


def plan(symbol="BTC/USDT"):
    return {"symbol": symbol, "quantity": "0.1", "entry": "100", "stop": "95", "target": "110", "created_at": time.time()}


class OKX:
    credential_id = "dummy-key-fingerprint"

    def __init__(self):
        self.params = None
        self.submissions = 0
        self.timeout = False
        self.entry_patch, self.algo_patch, self.child_patch = {}, {}, {}
        self.trade_patch = {}
        self.exit = False
        self.missing_fills = False

    def assert_demo(self):
        pass

    def submit(self, params):
        self.params = params
        self.submissions += 1
        if self.timeout:
            raise TimeoutError("do not log credentials")

    def query_entry(self, instrument, client_id):
        entry = {"instType": "SPOT", "instId": instrument, "tdMode": "cash", "side": "buy",
                 "ordId": "101", "clOrdId": client_id, "ordType": "fok", "state": "filled",
                 "sz": "0.1", "accFillSz": "0.1", "px": "100", "avgPx": "100",
                 "fee": "-0.0001", "feeCcy": "BTC", "rebate": "0", "rebateCcy": "",
                 "attachAlgoOrds": copy.deepcopy(self.params["attachAlgoOrds"])}
        entry.update(self.entry_patch)
        return {"code": "0", "data": [entry]}

    def query_algo(self, client_id):
        algo = {**self.params["attachAlgoOrds"][0], "algoId": "102", "algoClOrdId": client_id,
                "instType": "SPOT", "instId": "BTC-USDT", "tdMode": "cash", "side": "sell", "ordType": "oco",
                "sz": "0.0999", "state": "effective" if self.exit else "live", "ordIdList": ["103"] if self.exit else []}
        algo.update(self.algo_patch)
        return {"code": "0", "data": [algo]}

    def query_order(self, instrument, order_id):
        child = {"instType": "SPOT", "instId": instrument, "tdMode": "cash", "side": "sell", "ordId": order_id,
                 "ordType": "market", "state": "filled", "sz": "0.0999", "accFillSz": "0.0999", "avgPx": "110",
                 "fee": "-0.010989", "feeCcy": "USDT", "rebate": "0", "rebateCcy": "",
                 "algoId": "102", "algoClOrdId": self.params["attachAlgoOrds"][0]["attachAlgoClOrdId"]}
        child.update(self.child_patch)
        return {"code": "0", "data": [child]}

    def query_fills(self, instrument, order_id):
        report = (self.query_entry(instrument, self.params["clOrdId"]) if order_id == "101"
                  else self.query_order(instrument, order_id))["data"][0]
        if self.missing_fills or report["accFillSz"] == "0":
            return []
        trade = {"billId": order_id, "ordId": order_id, "instType": "SPOT", "instId": instrument,
                 "side": report["side"], "fillSz": report["accFillSz"], "fillPx": report["avgPx"],
                 "fee": report["fee"], "feeCcy": report["feeCcy"]}
        trade.update(self.trade_patch)
        return [trade]


def okx_controller(tmp_path):
    t = OKX()
    j = okx.OKXDemoJournal(tmp_path / "okx.db")
    return t, j, okx.OKXDemoLifecycle(t, j, "owner")


def test_okx_timeout_native_protection_restart_and_fee_adjusted_exit(tmp_path):
    t, j, c = okx_controller(tmp_path)
    t.timeout = True
    record = c.submit(plan(), okx.ACKNOWLEDGEMENT)
    assert record["status"] == "protected"
    with pytest.raises(sqlite3.IntegrityError):
        c.submit(plan(), okx.ACKNOWLEDGEMENT)
    restarted = okx.OKXDemoLifecycle(t, okx.OKXDemoJournal(tmp_path / "okx.db"), "owner")
    t.exit = True
    result = restarted.resume()[0]
    assert result["status"] == "closed"
    assert Decimal(result["observation"]["realized_pnl_usdt"]) == Decimal("0.978011")
    assert result["observation"]["production_ready"] is False
    assert t.submissions == 1


@pytest.mark.parametrize("field,value", [("algoClOrdId", "unrelated"), ("slTriggerPx", "96"),
    ("sz", None), ("ordType", "conditional"), ("side", "buy"), ("ordIdList", None)])
def test_okx_does_not_guess_protection(tmp_path, field, value):
    t, j, c = okx_controller(tmp_path)
    t.algo_patch[field] = value
    assert c.submit(plan(), okx.ACKNOWLEDGEMENT)["status"] == "reconciliation_required"


@pytest.mark.parametrize("field,value", [("fee", None), ("fee", "NaN"), ("fillSz", ".09"),
                                       ("billId", ""), ("ordId", "999"), ("fillPx", "101")])
def test_okx_unverified_fills_cannot_claim_protection(tmp_path, field, value):
    t, j, c = okx_controller(tmp_path)
    t.trade_patch[field] = value
    assert c.submit(plan(), okx.ACKNOWLEDGEMENT)["status"] == "reconciliation_required"


def test_okx_residual_and_external_fee_block_completion(tmp_path):
    t, j, c = okx_controller(tmp_path)
    t.exit = True
    t.child_patch.update(sz="0.0998", accFillSz="0.0998")
    record = c.submit(plan(), okx.ACKNOWLEDGEMENT)
    assert record["status"] == "recovery_required"
    assert Decimal(record["observation"]["residual_base"]) == Decimal(".0001")


def test_okx_partial_fok_blocks_and_no_fill_allows_retry(tmp_path):
    t, j, c = okx_controller(tmp_path)
    t.entry_patch.update(accFillSz="0", state="canceled")
    assert c.submit(plan(), okx.ACKNOWLEDGEMENT)["status"] == "no_fill"
    t.entry_patch.update(accFillSz="0.05", state="partially_filled")
    assert c.submit(plan(), okx.ACKNOWLEDGEMENT)["status"] == "recovery_required"


def test_okx_confirmed_entry_cannot_regress_after_outage(tmp_path):
    t, j, c = okx_controller(tmp_path)
    record = c.submit(plan(), okx.ACKNOWLEDGEMENT)
    t.missing_fills = True
    assert c.reconcile(record["id"])["status"] == "reconciliation_required"
    t.missing_fills = False
    t.entry_patch.update(accFillSz="0", state="canceled")
    assert c.reconcile(record["id"])["status"] == "reconciliation_required"
    assert len(j.active("owner", t.credential_id)) == 1


class Bybit:
    def __init__(self):
        self.submissions, self.params = 0, None
        self.timeout, self.missing_fills = False, False
        self.entry_patch, self.trade_patch = {}, {}

    def assert_testnet(self):
        pass

    def submit(self, params):
        self.params = params
        self.submissions += 1
        if self.timeout:
            raise TimeoutError("secret data")
        return {"orderId": "entry-1", "orderLinkId": params["orderLinkId"]}

    def query_order(self, symbol, client_id):
        result = {"symbol": symbol, "orderId": "entry-1", "orderLinkId": client_id,
                  "side": "Buy", "orderType": "Limit", "timeInForce": "FOK", "isLeverage": "0",
                  "qty": "0.1", "cumExecQty": "0.1", "leavesQty": "0", "cumExecValue": "10",
                  "orderStatus": "Filled", "price": "100", "takeProfit": "110", "stopLoss": "95"}
        result.update(self.entry_patch)
        return result

    def query_executions(self, symbol, order_id):
        if self.missing_fills:
            return []
        trade = {"execId": "fill-1", "orderId": order_id, "orderLinkId": self.params["orderLinkId"],
                 "symbol": symbol, "side": "Buy", "orderType": "Limit", "execType": "Trade",
                 "execQty": "0.1", "execPrice": "100", "execValue": "10", "execFee": "0.0001", "feeCurrency": "BTC"}
        trade.update(self.trade_patch)
        return [trade]

    def query_protection_orders(self, symbol):
        return [{"orderId": "candidate-stop", "symbol": symbol, "side": "Sell", "stopOrderType": "StopLoss",
                 "qty": "0.0999", "cumExecQty": "0", "leavesQty": "0.0999", "orderStatus": "Untriggered",
                 "isLeverage": "0", "parentOrderLinkId": self.params["orderLinkId"]}]


def bybit_controller(tmp_path):
    t = Bybit()
    j = bybit.BybitJournal(tmp_path / "bybit.db")
    return t, j, bybit.BybitTestnetLifecycle(t, j, "owner")


def test_bybit_timeout_restart_keeps_unverified_children_blocked(tmp_path):
    t, j, c = bybit_controller(tmp_path)
    t.timeout = True
    result = c.submit(plan("BTCUSDT"), bybit.ACKNOWLEDGEMENT)
    assert result["status"] == "child_linkage_unverified"
    assert result["observation"]["net_entry_quantity"] == "0.0999"
    assert result["observation"]["unattributed_protective_candidates"] == 1
    assert result["observation"]["protection_verified"] is False
    restarted = bybit.BybitTestnetLifecycle(t, bybit.BybitJournal(tmp_path / "bybit.db"), "owner")
    assert restarted.resume()[0]["status"] == "child_linkage_unverified"
    with pytest.raises(sqlite3.IntegrityError):
        restarted.submit(plan("BTCUSDT"), bybit.ACKNOWLEDGEMENT)
    assert t.submissions == 1


@pytest.mark.parametrize("field,value", [("feeCurrency", ""), ("execFee", None), ("execFee", "NaN"),
    ("execFee", "-1"), ("orderId", "different"), ("execQty", ".09"), ("execPrice", "101"),
    ("execId", ""), ("extraFees", "unhandled")])
def test_bybit_bad_or_incomplete_execution_fails_closed(tmp_path, field, value):
    t, j, c = bybit_controller(tmp_path)
    t.trade_patch[field] = value
    assert c.submit(plan("BTCUSDT"), bybit.ACKNOWLEDGEMENT)["status"] == "reconciliation_required"


def test_bybit_confirmed_fills_cannot_regress_to_no_fill_after_outage(tmp_path):
    t, j, c = bybit_controller(tmp_path)
    result = c.submit(plan("BTCUSDT"), bybit.ACKNOWLEDGEMENT)
    t.missing_fills = True
    assert c.reconcile(result["id"])["status"] == "reconciliation_required"
    t.entry_patch.update(cumExecQty="0", leavesQty="0", cumExecValue="0", orderStatus="Cancelled")
    assert c.reconcile(result["id"])["status"] == "reconciliation_required"


@pytest.mark.parametrize("venue", ["okx", "bybit"])
def test_demo_intent_requires_explicit_ack_freshness_and_size_cap(tmp_path, venue):
    t, j, c = okx_controller(tmp_path) if venue == "okx" else bybit_controller(tmp_path)
    ack = okx.ACKNOWLEDGEMENT if venue == "okx" else bybit.ACKNOWLEDGEMENT
    p = plan() if venue == "okx" else plan("BTCUSDT")
    with pytest.raises(ValueError):
        c.submit(p, "")
    p["quantity"] = "100"
    with pytest.raises(ValueError):
        c.submit(p, ack)
    p.update(quantity="0.1", created_at=time.time() - 20)
    with pytest.raises(ValueError):
        c.submit(p, ack)
    assert t.submissions == 0


@pytest.mark.parametrize("kind", ["okx", "bybit", "binance"])
def test_native_transports_refuse_retry_configuration_and_production_hosts(kind):
    t = (okx.OKXDemoTransport("dummy", "dummy", "dummy") if kind == "okx" else
         bybit.BybitTestnetTransport("dummy", "dummy") if kind == "bybit" else BinanceTestnetTransport("dummy", "dummy"))
    check = t.assert_demo if kind == "okx" else t.assert_testnet
    t.client.options["maxRetriesOnFailure"] = 2
    with pytest.raises(ValueError):
        check()
    t.client.options["maxRetriesOnFailure"] = 0
    if kind == "okx":
        t.client.headers["x-simulated-trading"] = "0"
    else:
        t.client.urls["api"]["private"] = "https://api.binance.com/api/v3"
    with pytest.raises(ValueError):
        check()


@pytest.mark.parametrize("venue", ["okx", "bybit"])
def test_intent_persistence_failure_prevents_any_submission(tmp_path, monkeypatch, venue):
    t, j, c = okx_controller(tmp_path) if venue == "okx" else bybit_controller(tmp_path)
    def disk_full(*args):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(j, "create", disk_full)
    with pytest.raises(sqlite3.OperationalError):
        c.submit(plan() if venue == "okx" else plan("BTCUSDT"), okx.ACKNOWLEDGEMENT if venue == "okx" else bybit.ACKNOWLEDGEMENT)
    assert t.submissions == 0


@pytest.mark.parametrize("kind", ["okx", "bybit", "binance"])
def test_sdk_timeout_is_never_retried_even_with_per_method_override(monkeypatch, kind):
    import ccxt
    t = (okx.OKXDemoTransport("dummy", "dummy", "dummy") if kind == "okx" else
         bybit.BybitTestnetTransport("dummy", "dummy") if kind == "bybit" else BinanceTestnetTransport("dummy", "dummy"))
    calls = []
    def timed_out(*args, **kwargs):
        calls.append(True)
        raise ccxt.RequestTimeout("timeout")
    monkeypatch.setattr(t.client, "fetch", timed_out)
    method = {"okx": "trade/order", "bybit": "v5/order/create", "binance": "orderList/otoco"}[kind]
    t.client.options[method] = {"maxRetriesOnFailure": 3}
    with pytest.raises(ccxt.RequestTimeout):
        t.submit({})
    assert len(calls) == 1


def test_okx_repeated_pagination_cursor_does_not_invent_complete_history(monkeypatch):
    t = okx.OKXDemoTransport("dummy", "dummy", "dummy")
    rows = [{"billId": str(i)} for i in range(100, 200)]
    monkeypatch.setattr(t.client, "private_get_trade_fills", lambda params: {"code": "0", "data": rows})
    with pytest.raises(ValueError, match="progressing"):
        t.query_fills("BTC-USDT", "101")


def test_bybit_cursor_loop_blocks_incomplete_history(monkeypatch):
    t = bybit.BybitTestnetTransport("dummy", "dummy")
    monkeypatch.setattr(t.client, "private_get_v5_execution_list", lambda params:
        {"retCode": 0, "result": {"category": "spot", "list": [], "nextPageCursor": "repeat"}})
    with pytest.raises(ValueError, match="cursor"):
        t.query_executions("BTCUSDT", "entry-1")


def test_bybit_first_observed_fill_survives_missing_execution_history(tmp_path):
    t, j, c = bybit_controller(tmp_path)
    t.missing_fills = True
    result = c.submit(plan("BTCUSDT"), bybit.ACKNOWLEDGEMENT)
    assert result["status"] == "reconciliation_required"
    assert result["observation"]["entry_evidence"]["filled"] == "0.1"
    t.entry_patch.update(cumExecQty="0", cumExecValue="0", orderStatus="Cancelled")
    assert c.reconcile(result["id"])["status"] == "reconciliation_required"
    assert j.active("owner") == [result["id"]]


def test_okx_first_observed_fill_survives_missing_execution_history(tmp_path):
    t, j, c = okx_controller(tmp_path)
    t.missing_fills = True
    result = c.submit(plan(), okx.ACKNOWLEDGEMENT)
    assert result["status"] == "reconciliation_required"
    t.entry_patch.update(accFillSz="0", state="canceled")
    assert c.reconcile(result["id"])["status"] == "reconciliation_required"


@pytest.mark.parametrize("venue", ["okx", "bybit"])
def test_demo_delayed_recovery_cannot_erase_newer_committed_evidence(tmp_path, venue):
    if venue == "okx":
        t, j, c = okx_controller(tmp_path)
        identity = j.create("owner", t.credential_id, plan())
        stale = j.get("owner", t.credential_id, identity)
        assert j.update("owner", t.credential_id, identity, "protected", {"newer": True}, expected=stale)
        assert not j.update("owner", t.credential_id, identity, "no_fill", {}, expected=stale)
        assert j.get("owner", t.credential_id, identity)["status"] == "protected"
    else:
        t, j, c = bybit_controller(tmp_path)
        identity = j.create("owner", plan("BTCUSDT"))
        stale = j.get("owner", identity)
        assert j.update("owner", identity, "child_linkage_unverified", {"newer": True}, expected=stale)
        assert not j.update("owner", identity, "no_fill", {}, expected=stale)
        assert j.get("owner", identity)["status"] == "child_linkage_unverified"
