"""Durable, explicitly acknowledged Bybit Spot TESTNET qualification tool.

This module is not a production execution adapter. Bybit documents attached
Spot TP/SL on entry, but its REST parentOrderLinkId is documented only for
futures/options. Matching a Spot sell's price/quantity is NOT proof of lineage.
Consequently filled entries remain blocked until authoritative child linkage
can be qualified; this tool never labels a position protected or closed.

Official references (reviewed 2026-09-08):
https://bybit-exchange.github.io/docs/v5/order/create-order
https://bybit-exchange.github.io/docs/v5/order/open-order
https://bybit-exchange.github.io/docs/v5/order/execution
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .brackets import ACKNOWLEDGEMENT, strict_decimal, validate_plan


TARGET = "bybit-testnet"
TERMINAL = {"no_fill"}
ORDER_TERMINAL = {"Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated"}
ORDER_OPEN = {"New", "PartiallyFilled", "Untriggered", "Triggered"}
PROTECTIVE_TYPES = {"TakeProfit", "StopLoss", "tpslOrder", "OcoOrder", "BidirectionalTpslOrder"}
LINKAGE_LIMITATION = (
    "Spot protective-child linkage is not documented by the REST API. "
    "Candidate orders are not attributed by matching price, size or symbol. "
    "Protection, remaining inventory and exit fees remain unverified; no new entry is allowed."
)


def normalise_plan(plan):
    """Accept only a fresh <=25 USDT, non-leveraged, bounded Spot buy."""
    checked = dict(plan)
    checked.setdefault("protected_quantity", checked.get("quantity"))
    validate_plan(checked)
    if strict_decimal(checked["quantity"]) != strict_decimal(checked["protected_quantity"]):
        raise ValueError("Bybit attached TP/SL cannot specify a separate protected quantity")
    return {key: str(checked[key]) for key in
            ("symbol", "quantity", "entry", "stop", "target", "created_at")}


def entry_client_id(identity):
    if not re.fullmatch(r"[a-f0-9]{24}", identity):
        raise ValueError("Invalid journal identity")
    return "acB" + identity


class BybitJournal:
    """One unresolved intent per owner; no credential material is persisted."""

    def __init__(self, path):
        self.path = str(path)
        with self.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS bybit_signal_intents (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL,
                    status TEXT NOT NULL, plan TEXT NOT NULL, observation TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS bybit_one_active_intent
                    ON bybit_signal_intents(owner,target) WHERE status != 'no_fill';
                CREATE TABLE IF NOT EXISTS bybit_signal_events (
                    id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL,
                    status TEXT NOT NULL, recorded_at REAL NOT NULL);
            """)

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.execute("PRAGMA synchronous=FULL")
        try:
            with con:
                yield con
        finally:
            con.close()

    def create(self, owner, plan):
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ValueError("Opaque account owner identity is required")
        identity = uuid.uuid4().hex[:24]
        with self.db() as con:
            con.execute("INSERT INTO bybit_signal_intents VALUES(?,?,?,?,?,?,?,?)",
                        (identity, owner, TARGET, "intent", json.dumps(plan, allow_nan=False), "{}",
                         time.time(), time.time()))
            con.execute("INSERT INTO bybit_signal_events(intent_id,status,recorded_at) VALUES(?,?,?)",
                        (identity, "intent", time.time()))
        return identity

    def get(self, owner, identity):
        with self.db() as con:
            row = con.execute("SELECT target,status,plan,observation FROM bybit_signal_intents WHERE owner=? AND id=?",
                              (owner, identity)).fetchone()
        if not row:
            raise ValueError("Intent not found for this account")
        return {"id": identity, "target": row[0], "status": row[1], "plan": json.loads(row[2]),
                "observation": json.loads(row[3])}

    def active(self, owner):
        with self.db() as con:
            return [row[0] for row in con.execute(
                "SELECT id FROM bybit_signal_intents WHERE owner=? AND target=? AND status != 'no_fill' ORDER BY created_at",
                (owner, TARGET))]

    def update(self, owner, identity, status, observation, expected=None):
        with self.db() as con:
            sql = "UPDATE bybit_signal_intents SET status=?,observation=?,updated_at=? WHERE owner=? AND id=?"
            params = [status, json.dumps(observation, allow_nan=False), time.time(), owner, identity]
            if expected is not None:
                sql += " AND status=? AND observation=?"
                params.extend([expected["status"], json.dumps(expected["observation"], allow_nan=False)])
            changed = con.execute(sql, params)
            if changed.rowcount != 1:
                if expected is not None and con.execute("SELECT 1 FROM bybit_signal_intents WHERE owner=? AND id=?", (owner, identity)).fetchone():
                    return False
                raise ValueError("Intent not found for this account")
            con.execute("INSERT INTO bybit_signal_events(intent_id,status,recorded_at) VALUES(?,?,?)",
                        (identity, status, time.time()))
        return True


def _positive(value):
    result = strict_decimal(value)
    if result <= 0:
        raise ValueError("Expected positive exchange value")
    return result


def _order_numbers(order):
    quantity = _positive(order.get("qty"))
    filled = strict_decimal(order.get("cumExecQty"))
    leaves = strict_decimal(order.get("leavesQty"))
    status = order.get("orderStatus")
    if status not in ORDER_OPEN | ORDER_TERMINAL or not 0 <= filled <= quantity or not 0 <= leaves <= quantity - filled:
        raise ValueError("Inconsistent order quantity or status")
    if status == "Filled" and (filled != quantity or leaves != 0):
        raise ValueError("Incomplete filled order")
    if status in ORDER_OPEN and leaves + filled != quantity:
        raise ValueError("Missing open order quantity")
    return quantity, filled, leaves


def _entry_details(entry, plan, client_id, accepted_order_id=None):
    if (entry.get("orderLinkId") != client_id or entry.get("symbol") != plan["symbol"]
            or entry.get("side") != "Buy" or entry.get("orderType") != "Limit"
            or entry.get("timeInForce") != "FOK" or str(entry.get("isLeverage")) != "0"):
        raise ValueError("Unbounded or mismatched entry")
    order_id = entry.get("orderId")
    if not isinstance(order_id, str) or not order_id or (accepted_order_id and accepted_order_id != order_id):
        raise ValueError("Entry order identity mismatch")
    for key, source in (("price", "entry"), ("takeProfit", "target"), ("stopLoss", "stop")):
        if strict_decimal(entry.get(key)) != strict_decimal(plan[source]):
            raise ValueError("Entry or attached trigger differs from durable intent")
    quantity, filled, _ = _order_numbers(entry)
    if quantity != strict_decimal(plan["quantity"]):
        raise ValueError("Entry quantity differs from durable intent")
    cumulative_value = strict_decimal(entry.get("cumExecValue"))
    if cumulative_value < 0:
        raise ValueError("Invalid cumulative quote value")
    return order_id, quantity, filled, cumulative_value


def _entry_accounting(entry, executions, plan, client_id, accepted_order_id=None):
    """Reconcile exact execution IDs and fee currencies, without guessed fills."""
    order_id, quantity, filled, cumulative_value = _entry_details(entry, plan, client_id, accepted_order_id)
    base = plan["symbol"][:-4]
    seen, total_quantity, total_value, fees = set(), Decimal(0), Decimal(0), {}
    if not isinstance(executions, list):
        raise ValueError("Missing execution history")
    for execution in executions:
        execution_id = execution.get("execId")
        if (not isinstance(execution_id, str) or not execution_id or execution_id in seen
                or execution.get("orderId") != order_id or execution.get("orderLinkId") != client_id
                or execution.get("symbol") != plan["symbol"] or execution.get("side") != "Buy"
                or execution.get("orderType") != "Limit" or execution.get("execType") != "Trade"):
            raise ValueError("Duplicate, missing or mismatched execution identity")
        seen.add(execution_id)
        q, price, value = (_positive(execution.get(key)) for key in ("execQty", "execPrice", "execValue"))
        if price > strict_decimal(plan["entry"]) or value != q * price:
            raise ValueError("Execution price violates entry cap or quote accounting")
        fee = strict_decimal(execution.get("execFee"))
        currency = execution.get("feeCurrency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", currency):
            raise ValueError("Missing execution fee currency")
        if fee < 0 or (currency == base and fee >= q) or (currency == "USDT" and fee >= value):
            raise ValueError("Unqualified execution fee")
        if execution.get("extraFees") not in (None, "", {}):
            raise ValueError("Additional regional fees require explicit reconciliation")
        fees[currency] = fees.get(currency, Decimal(0)) + fee
        total_quantity += q
        total_value += value
    if total_quantity != filled or total_value != cumulative_value:
        raise ValueError("Execution history is incomplete or disagrees with order totals")
    aggregate_fees = entry.get("cumFeeDetail")
    if aggregate_fees is not None:
        if not isinstance(aggregate_fees, dict):
            raise ValueError("Invalid aggregate fees")
        aggregate = {currency: strict_decimal(fee) for currency, fee in aggregate_fees.items()}
        if any(value < 0 for value in aggregate.values()) or {k: v for k, v in aggregate.items() if v} != {k: v for k, v in fees.items() if v}:
            raise ValueError("Execution fees disagree with order totals")
    return {"order_id": order_id, "entry_status": entry["orderStatus"], "gross_entry_quantity": str(filled),
            "net_entry_quantity": str(filled - fees.get(base, Decimal(0))),
            "quote_entry_cost": str(total_value + fees.get("USDT", Decimal(0))),
            "entry_fees": {currency: str(fee) for currency, fee in fees.items()},
            "execution_count": len(seen)}


def _candidate_summary(orders, symbol):
    """Inspect orders for diagnostics ONLY, never use similarity as ownership."""
    if not isinstance(orders, list):
        raise ValueError("Missing protection scan")
    identities, candidates = set(), 0
    for order in orders:
        order_id = order.get("orderId")
        if not isinstance(order_id, str) or not order_id or order_id in identities or order.get("symbol") != symbol:
            raise ValueError("Ambiguous protection scan")
        identities.add(order_id)
        if order.get("stopOrderType") not in PROTECTIVE_TYPES or order.get("side") != "Sell":
            continue
        _order_numbers(order)
        if str(order.get("isLeverage")) != "0":
            raise ValueError("Leveraged protection candidate")
        candidates += 1
    return {"unattributed_protective_candidates": candidates,
            "protective_child_ids_verified": False, "remaining_quantity": None,
            "protection_verified": False, "production_ready": False, "limitation": LINKAGE_LIMITATION}


class BybitTestnetLifecycle:
    def __init__(self, transport, journal, owner):
        self.transport, self.journal, self.owner = transport, journal, owner

    def submit(self, plan, acknowledgement):
        self.transport.assert_testnet()
        if acknowledgement != ACKNOWLEDGEMENT:
            raise ValueError("Explicit testnet-order acknowledgement required")
        checked = normalise_plan(plan)
        identity = self.journal.create(self.owner, checked)  # Committed before any POST.
        params = {"category": "spot", "symbol": checked["symbol"], "side": "Buy", "orderType": "Limit",
                  "timeInForce": "FOK", "qty": checked["quantity"], "price": checked["entry"],
                  "orderLinkId": entry_client_id(identity), "takeProfit": checked["target"],
                  "stopLoss": checked["stop"], "tpOrderType": "Market", "slOrderType": "Market",
                  "isLeverage": 0, "orderFilter": "Order"}
        try:
            result = self.transport.submit(params)
            if (result.get("orderLinkId") != params["orderLinkId"]
                    or not isinstance(result.get("orderId"), str) or not result["orderId"]):
                raise ValueError("Unverified order acknowledgement")
            self.journal.update(self.owner, identity, "accepted", {"accepted_order_id": result["orderId"]},
                                expected={"status": "intent", "observation": {}})
        except Exception as exc:
            # A timeout/rejection can be ambiguous. There is deliberately no POST retry.
            self.journal.update(self.owner, identity, "unknown_submission", {"error_type": type(exc).__name__},
                                expected={"status": "intent", "observation": {}})
        return self.reconcile(identity)

    def reconcile(self, identity):
        self.transport.assert_testnet()
        record = self.journal.get(self.owner, identity)
        if record["target"] != TARGET:
            raise ValueError("Wrong execution environment")
        if record["status"] in TERMINAL:
            return record
        plan, observation = record["plan"], dict(record["observation"])
        observation.pop("error_type", None)
        try:
            entry = self.transport.query_order(plan["symbol"], entry_client_id(identity))
            pinned = observation.get("entry_evidence", {})
            order_id, _, filled, value = _entry_details(entry, plan, entry_client_id(identity),
                pinned.get("order_id") or observation.get("accepted_order_id") or observation.get("order_id"))
            if (filled < strict_decimal(pinned.get("filled", "0")) or value < strict_decimal(pinned.get("quote", "0"))
                    or (pinned.get("status") in ORDER_TERMINAL and pinned["status"] != entry["orderStatus"])):
                raise ValueError("Previously observed entry evidence regressed")
            observation["entry_evidence"] = {"order_id": order_id, "filled": str(filled), "quote": str(value), "status": entry["orderStatus"]}
            executions = self.transport.query_executions(plan["symbol"], entry.get("orderId"))
            accounting = _entry_accounting(entry, executions, plan, entry_client_id(identity),
                                          observation.get("accepted_order_id") or observation.get("order_id"))
            if (strict_decimal(accounting["gross_entry_quantity"]) < strict_decimal(observation.get("gross_entry_quantity", "0"))
                    or (observation.get("entry_status") in ORDER_TERMINAL
                        and observation["entry_status"] != accounting["entry_status"])):
                raise ValueError("Previously confirmed entry fills or terminal state regressed")
            if self.transport.query_order(plan["symbol"], entry_client_id(identity)) != entry:
                raise ValueError("Order changed during reconciliation")
            observation.update(accounting)
            bought = strict_decimal(accounting["gross_entry_quantity"])
            if bought == 0 and entry["orderStatus"] in ORDER_TERMINAL:
                status = "no_fill"
            elif bought == 0:
                status = "entry_pending"
            else:
                observation.update(_candidate_summary(self.transport.query_protection_orders(plan["symbol"]), plan["symbol"]))
                if bought != strict_decimal(plan["quantity"]) or entry["orderStatus"] != "Filled":
                    status = "recovery_required"
                    observation["reason"] = "Unexpected partial FOK fill; no replacement or automatic sell submitted"
                else:
                    status = "child_linkage_unverified"
            observation["production_ready"] = False
            observation["protection_verified"] = False
        except Exception as exc:
            status = "reconciliation_required"
            observation.update({"error_type": type(exc).__name__, "protection_verified": False,
                                "production_ready": False})
        self.journal.update(self.owner, identity, status, observation, expected=record)
        return self.journal.get(self.owner, identity)

    def resume(self):
        """Read-only restart recovery of every unresolved journal intent."""
        self.transport.assert_testnet()
        return [self.reconcile(identity) for identity in self.journal.active(self.owner)]


class BybitTestnetTransport:
    """Dedicated testnet keys, exact host whitelist; no credential fallback."""

    def __init__(self, api_key, api_secret):
        import ccxt
        if not api_key or not api_secret:
            raise ValueError("Dedicated Bybit Testnet credentials are required")
        self.client = ccxt.bybit({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True,
                                  "timeout": 5000, "options": {"defaultType": "spot", "maxRetriesOnFailure": 0}})
        self.client.set_sandbox_mode(True)
        self.assert_testnet()

    def assert_testnet(self):
        if (self.client.options.get("maxRetriesOnFailure") != 0
                or self.client.hostname != "bybit.com" or any(self.client.urls["api"].get(key) != "https://api-testnet.{hostname}"
                                                     for key in ("public", "private"))):
            raise ValueError("Non-testnet endpoint refused")

    @staticmethod
    def _result(response):
        if (not isinstance(response, dict) or type(response.get("retCode")) is not int
                or response["retCode"] != 0 or not isinstance(response.get("result"), dict)):
            raise ValueError("Unsuccessful or malformed Bybit response")
        return response["result"]

    def _pages(self, method, params):
        rows, cursors, cursor = [], set(), ""
        for _ in range(20):
            self.assert_testnet()
            result = self._result(method({**params, **({"cursor": cursor} if cursor else {}), "limit": 50}))
            if result.get("category") != "spot" or not isinstance(result.get("list"), list):
                raise ValueError("Wrong response category or missing rows")
            if any(not isinstance(row, dict) for row in result["list"]):
                raise ValueError("Malformed exchange row")
            rows.extend(result["list"])
            cursor = result.get("nextPageCursor", "")
            if cursor == "":
                return rows
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("Repeated or malformed exchange pagination cursor")
            cursors.add(cursor)
        raise ValueError("Exchange pagination incomplete")

    def submit(self, params):
        self.assert_testnet()
        return self._result(self.client.private_post_v5_order_create({**params, "maxRetriesOnFailure": 0}))

    def query_order(self, symbol, client_id):
        params = {"category": "spot", "symbol": symbol, "orderLinkId": client_id}
        rows = self._pages(self.client.private_get_v5_order_realtime, params)
        if not rows:
            rows = self._pages(self.client.private_get_v5_order_history, params)
        if len(rows) != 1:
            raise ValueError("Entry missing or ambiguous; never resubmit")
        return rows[0]

    def query_executions(self, symbol, order_id):
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("Missing entry order ID")
        return self._pages(self.client.private_get_v5_execution_list,
                           {"category": "spot", "symbol": symbol, "orderId": order_id})

    def query_protection_orders(self, symbol):
        return self._pages(self.client.private_get_v5_order_realtime,
                           {"category": "spot", "symbol": symbol, "openOnly": 0})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="bybit-signal-testnet.db")
    parser.add_argument("--plan", help="Fresh JSON plan: symbol, quantity, entry, stop, target, created_at")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--submit", action="store_true", help="Submit one <=25 USDT TESTNET order")
    group.add_argument("--reconcile", metavar="INTENT_ID")
    group.add_argument("--resume", action="store_true", help="Read-only reconciliation of unresolved intents")
    parser.add_argument("--ack", default="")
    args = parser.parse_args(argv)
    if not (args.submit or args.reconcile or args.resume):
        parser.error("Choose --submit, --reconcile or --resume; no action is automatic")
    if args.submit and (not args.plan or args.ack != ACKNOWLEDGEMENT):
        parser.error("--submit requires --plan and --ack 'PLACE TESTNET ORDERS'")
    try:
        api_key = os.environ.get("BYBIT_TESTNET_API_KEY", "")
        transport = BybitTestnetTransport(api_key, os.environ.get("BYBIT_TESTNET_API_SECRET", ""))
        # The fingerprint is key-scoped, NOT an exchange account UID. Do not use
        # multiple keys/journals for the same account to bypass the intent lock.
        owner = hashlib.sha256(api_key.encode()).hexdigest()
        lifecycle = BybitTestnetLifecycle(transport, BybitJournal(args.journal), owner)
        if args.submit:
            result = lifecycle.submit(json.loads(Path(args.plan).read_text(encoding="utf-8")), args.ack)
        else:
            result = lifecycle.resume() if args.resume else lifecycle.reconcile(args.reconcile)
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0
    except Exception as exc:
        # CCXT errors can include credential-bearing request material.
        print(json.dumps({"error_type": type(exc).__name__, "production_ready": False,
                          "message": "Testnet qualification failed; reconcile the journal before any new attempt."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
