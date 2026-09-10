"""Durable, explicitly acknowledged OKX *demo-only* attached TP/SL lifecycle.

This is qualification tooling, not the production signal executor. An accepted
entry is never evidence of protection: the attached OCO must be queried after
the FOK entry fills. Ambiguous submissions are only queried, never retried.

Protocol reference: https://www.okx.com/docs-v5/en/ (place order, order details,
algo order details, and demo-trading headers). Real exchange qualification,
dynamic cancellation/exit and cross-key account ownership remain unverified.
"""

import argparse
import copy
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

from .brackets import strict_decimal


ACKNOWLEDGEMENT = "PLACE OKX DEMO ORDERS"
TARGET = "okx-demo"
TERMINAL = {"closed", "no_fill"}
ORDER_STATES = {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}
ORDER_TERMINAL = {"filled", "canceled", "mmp_canceled"}
ALGO_STATES = {"live", "pause", "partially_effective", "effective", "canceled",
               "order_failed", "partially_failed"}


class OKXDemoJournal:
    """An unresolved intent occupies its account slot, including after restart.

    Keep this journal durable and use one owner for the exchange account. The
    credential binding prevents accidental recovery with another API key; it
    cannot detect two different keys belonging to the same exchange account.
    """

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("A persistent demo journal is required")
        self.path = str(path)
        with self.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS okx_demo_brackets (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    credential_id TEXT NOT NULL, target TEXT NOT NULL,
                    status TEXT NOT NULL, plan TEXT NOT NULL,
                    observation TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS okx_demo_one_active_owner
                    ON okx_demo_brackets(owner)
                    WHERE status NOT IN ('closed', 'no_fill');
                CREATE UNIQUE INDEX IF NOT EXISTS okx_demo_one_active_key
                    ON okx_demo_brackets(credential_id)
                    WHERE status NOT IN ('closed', 'no_fill');
                CREATE TABLE IF NOT EXISTS okx_demo_events (
                    id INTEGER PRIMARY KEY, bracket_id TEXT NOT NULL,
                    status TEXT NOT NULL, observation TEXT NOT NULL,
                    recorded_at REAL NOT NULL);
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

    def create(self, owner, credential_id, plan):
        if not owner or not credential_id:
            raise ValueError("Demo account identity is required")
        identity = uuid.uuid4().hex[:24]
        with self.db() as con:
            con.execute("INSERT INTO okx_demo_brackets VALUES(?,?,?,?,?,?,?,?)",
                        (identity, owner, credential_id, TARGET, "intent",
                         json.dumps(plan, allow_nan=False), "{}", time.time()))
            con.execute("INSERT INTO okx_demo_events(bracket_id,status,observation,recorded_at) VALUES(?,?,?,?)",
                        (identity, "intent", "{}", time.time()))
        return identity

    def get(self, owner, credential_id, identity):
        with self.db() as con:
            row = con.execute(
                "SELECT target,status,plan,observation FROM okx_demo_brackets "
                "WHERE id=? AND owner=? AND credential_id=?",
                (identity, owner, credential_id)).fetchone()
        if row is None:
            raise ValueError("Demo bracket not found for this account and key")
        return {"id": identity, "target": row[0], "status": row[1],
                "plan": json.loads(row[2]), "observation": json.loads(row[3])}

    def active(self, owner, credential_id):
        with self.db() as con:
            return [row[0] for row in con.execute(
                "SELECT id FROM okx_demo_brackets WHERE owner=? AND credential_id=? "
                "AND status NOT IN ('closed','no_fill') ORDER BY updated_at", (owner, credential_id))]

    def update(self, owner, credential_id, identity, status, observation, expected=None):
        payload = json.dumps(observation, allow_nan=False)
        with self.db() as con:
            sql = "UPDATE okx_demo_brackets SET status=?,observation=?,updated_at=? WHERE id=? AND owner=? AND credential_id=?"
            params = [status, payload, time.time(), identity, owner, credential_id]
            if expected is not None:
                sql += " AND status=? AND observation=?"
                params.extend([expected["status"], json.dumps(expected["observation"], allow_nan=False)])
            row = con.execute(sql, params)
            if row.rowcount != 1:
                if expected is not None and con.execute("SELECT 1 FROM okx_demo_brackets WHERE id=? AND owner=? AND credential_id=?",
                                                       (identity, owner, credential_id)).fetchone():
                    return False
                raise ValueError("Demo bracket not found for this account and key")
            con.execute("INSERT INTO okx_demo_events(bracket_id,status,observation,recorded_at) VALUES(?,?,?,?)",
                        (identity, status, payload, time.time()))
        return True


def validate_plan(plan, now=None):
    if not isinstance(plan, dict) or not re.fullmatch(r"[A-Z0-9]{1,20}/USDT", str(plan.get("symbol", ""))):
        raise ValueError("Only explicit spot /USDT demo symbols are supported")
    result = {"symbol": plan["symbol"]}
    values = {key: strict_decimal(plan.get(key)) for key in ("quantity", "entry", "stop", "target")}
    if values["quantity"] <= 0 or not 0 < values["stop"] < values["entry"] < values["target"]:
        raise ValueError("Invalid positive bracket quantity or prices")
    if values["quantity"] * values["entry"] > 25:
        raise ValueError("Demo qualification orders are capped at 25 USDT")
    created = strict_decimal(plan.get("created_at"))
    age = strict_decimal(time.time() if now is None else now) - created
    if not 0 <= age <= 10:
        raise ValueError("Plan expired; rebuild from current demo market data")
    result.update({key: str(value) for key, value in values.items()})
    result["created_at"] = str(created)
    return result


def client_ids(identity):
    if not re.fullmatch(r"[a-f0-9]{24}", identity):
        raise ValueError("Invalid durable demo bracket identity")
    return "acE" + identity, "acP" + identity


def _one(response):
    if not isinstance(response, dict) or response.get("code") != "0":
        raise ValueError("OKX response was not successful")
    rows = response.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Expected exactly one OKX order record")
    if rows[0].get("sCode", "0") != "0":
        raise ValueError("OKX order request was not successful")
    return rows[0]


def _exchange_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value):
        raise ValueError("Missing or invalid exchange order identity")
    return value


def _triggers(report, plan):
    for side, name in (("tp", "target"), ("sl", "stop")):
        if (strict_decimal(report.get(side + "TriggerPx")) != strict_decimal(plan[name])
                or strict_decimal(report.get(side + "OrdPx")) != -1
                or report.get(side + "TriggerPxType") != "last"):
            raise ValueError("Attached protective trigger mismatch")


def _order(report, plan, side, client_id=None, order_id=None, algo_id=None, algo_client_id=None):
    if (report.get("instType") != "SPOT" or report.get("instId") != plan["symbol"].replace("/", "-")
            or report.get("tdMode") != "cash" or report.get("side") != side):
        raise ValueError("Unexpected order instrument, account mode or side")
    exchange_id = _exchange_id(report.get("ordId"))
    if (client_id is not None and report.get("clOrdId") != client_id
            or order_id is not None and exchange_id != order_id
            or algo_id is not None and report.get("algoId") != algo_id
            or algo_client_id is not None and report.get("algoClOrdId") != algo_client_id):
        raise ValueError("Exchange/client order identity mismatch")
    if report.get("ordType") != ("fok" if side == "buy" else "market"):
        raise ValueError("Unexpected entry or exit order type")
    quantity, filled = strict_decimal(report.get("sz")), strict_decimal(report.get("accFillSz"))
    if quantity <= 0 or not 0 <= filled <= quantity:
        raise ValueError("Invalid order quantities")
    if report.get("state") not in ORDER_STATES:
        raise ValueError("Unknown exchange order state")
    if report["state"] == "filled" and filled != quantity:
        raise ValueError("Filled order quantity is inconsistent")
    if report["state"] == "live" and filled != 0:
        raise ValueError("Live order unexpectedly has fills")
    if side == "buy" and (quantity != strict_decimal(plan["quantity"])
                           or strict_decimal(report.get("px")) != strict_decimal(plan["entry"])):
        raise ValueError("Unbounded or wrong-sized entry")
    return {"id": exchange_id, "state": report["state"], "quantity": str(quantity), "filled": str(filled)}


def _cashflows(report, base, side, trades):
    """Use individual fills, not a rounded average price times the order size."""
    filled = strict_decimal(report["accFillSz"])
    total_quantity, total_quote, fees, seen = Decimal(0), Decimal(0), {}, set()
    if not isinstance(trades, list):
        raise ValueError("Missing authenticated fill history")
    for trade in trades:
        identity = _exchange_id(trade.get("billId"))
        if (identity in seen or trade.get("ordId") != report["ordId"]
                or trade.get("instId") != report["instId"] or trade.get("instType") != "SPOT"
                or trade.get("side") != side):
            raise ValueError("Missing, duplicate or mismatched fill identity")
        seen.add(identity)
        quantity, price, fee = [strict_decimal(trade.get(k)) for k in ("fillSz", "fillPx", "fee")]
        currency = trade.get("feeCcy")
        if quantity <= 0 or price <= 0 or not isinstance(currency, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", currency):
            raise ValueError("Invalid individual fill or fee currency")
        if side == "buy" and price > strict_decimal(report["px"]):
            raise ValueError("Entry fill exceeded limit price")
        total_quantity += quantity
        total_quote += quantity * price
        fees[currency] = fees.get(currency, Decimal(0)) + fee
    if total_quantity != filled:
        raise ValueError("Fill history disagrees with cumulative order quantity")
    if filled == 0:
        return Decimal(0), Decimal(0), {}
    fee, rebate = strict_decimal(report.get("fee")), strict_decimal(report.get("rebate"))
    if fee > 0 or rebate < 0:
        raise ValueError("Unexpected signed taker fee or rebate")
    base_flow = filled if side == "buy" else -filled
    quote_flow = -total_quote if side == "buy" else total_quote
    other_fees, order_fees = {}, {}
    for amount, currency in ((fee, report.get("feeCcy")), (rebate, report.get("rebateCcy"))):
        if amount == 0:
            continue
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", currency):
            raise ValueError("Unknown fee currency")
        order_fees[currency] = order_fees.get(currency, Decimal(0)) + amount
    if {k: v for k, v in fees.items() if v} != {k: v for k, v in order_fees.items() if v}:
        raise ValueError("Individual fill fees disagree with order totals")
    for currency, amount in fees.items():
        if currency == base:
            base_flow += amount
        elif currency == "USDT":
            quote_flow += amount
        else:
            other_fees[currency] = str(strict_decimal(other_fees.get(currency, "0")) + amount)
    if side == "buy" and not 0 < base_flow <= filled:
        raise ValueError("Invalid net entry inventory")
    return base_flow, quote_flow, other_fees


def _pin(evidence, role, report):
    previous = evidence.get(role)
    if previous:
        if previous["id"] != report["id"] or strict_decimal(report["filled"]) < strict_decimal(previous["filled"]):
            raise ValueError("Order identity changed or cumulative fills decreased")
        if previous["state"] in ORDER_TERMINAL and report["state"] != previous["state"]:
            raise ValueError("Terminal exchange order state regressed")
    evidence[role] = report


class OKXDemoLifecycle:
    def __init__(self, transport, journal, owner):
        self.transport, self.journal, self.owner = transport, journal, owner

    def _get(self, identity):
        return self.journal.get(self.owner, self.transport.credential_id, identity)

    def _update(self, identity, status, observation, expected=None):
        return self.journal.update(self.owner, self.transport.credential_id, identity, status, observation, expected=expected)

    def submit(self, plan, acknowledgement):
        self.transport.assert_demo()
        if acknowledgement != ACKNOWLEDGEMENT:
            raise ValueError("Explicit OKX demo-order acknowledgement required")
        plan = validate_plan(plan)
        identity = self.journal.create(self.owner, self.transport.credential_id, plan)
        entry_id, algo_id = client_ids(identity)
        params = {"instId": plan["symbol"].replace("/", "-"), "tdMode": "cash", "side": "buy",
                  "ordType": "fok", "sz": plan["quantity"], "px": plan["entry"], "clOrdId": entry_id,
                  "attachAlgoOrds": [{"attachAlgoClOrdId": algo_id,
                                      "tpTriggerPx": plan["target"], "tpOrdPx": "-1", "tpTriggerPxType": "last",
                                      "slTriggerPx": plan["stop"], "slOrdPx": "-1", "slTriggerPxType": "last"}]}
        # Committed before the first (and only) request. Crashes leave a blocked slot.
        self._update(identity, "submitting", {})
        try:
            self.transport.submit(params)
        except Exception as exc:
            # Store only the exception type; SDK errors can contain signed request secrets.
            self._update(identity, "unknown_submission", {"error_type": type(exc).__name__},
                         expected={"status": "submitting", "observation": {}})
        return self.reconcile(identity)

    def reconcile(self, identity):
        """Read-only recovery. No amend, cancel, market-exit or retry fallback."""
        self.transport.assert_demo()
        record = self._get(identity)
        if record["target"] != TARGET:
            raise ValueError("Wrong execution environment")
        if record["status"] in TERMINAL:
            return record
        evidence = copy.deepcopy(record["observation"].get("evidence", {}))
        try:
            status, observation = self._observe(record["plan"], identity, evidence)
        except Exception as exc:
            status = "reconciliation_required"
            observation = {"error_type": type(exc).__name__,
                           "reason": "Exchange evidence is incomplete or inconsistent; do not submit again."}
        observation["evidence"] = evidence
        observation["production_ready"] = False
        self._update(identity, status, observation, expected=record)
        return self._get(identity)

    def resume(self):
        """Read-only restart recovery; never cancels or resubmits an intent."""
        return [self.reconcile(identity) for identity in self.journal.active(self.owner, self.transport.credential_id)]

    def _observe(self, plan, identity, evidence):
        entry_client_id, algo_client_id = client_ids(identity)
        entry = _one(self.transport.query_entry(plan["symbol"].replace("/", "-"), entry_client_id))
        entry_summary = _order(entry, plan, "buy", client_id=entry_client_id)
        _pin(evidence, "entry", entry_summary)
        bought = strict_decimal(entry_summary["filled"])
        instrument = plan["symbol"].replace("/", "-")
        base = plan["symbol"].split("/")[0]
        entry_flow = _cashflows(entry, base, "buy", self.transport.query_fills(instrument, entry_summary["id"]))
        if bought == 0:
            if entry_summary["state"] in {"canceled", "mmp_canceled"}:
                # Attached orders are activated only after a completely filled entry.
                return "no_fill", {"reason": "Entry canceled with zero confirmed fills."}
            return "entry_pending", {"reason": "FOK entry completion is not yet confirmed."}
        if entry_summary["state"] != "filled" or bought != strict_decimal(plan["quantity"]):
            return "recovery_required", {"reason": "Unexpected partial FOK fill; automatic re-entry is blocked."}
        if strict_decimal(entry.get("avgPx")) > strict_decimal(plan["entry"]):
            raise ValueError("Entry fill exceeded its limit price")
        attached = entry.get("attachAlgoOrds")
        if (not isinstance(attached, list) or len(attached) != 1 or not isinstance(attached[0], dict)
                or attached[0].get("attachAlgoClOrdId") != algo_client_id):
            raise ValueError("Attached algo identity is not confirmed on the entry")
        _triggers(attached[0], plan)
        if attached[0].get("failCode", "") not in ("", "0") or attached[0].get("failReason", ""):
            return "recovery_required", {"reason": "The attached protective order failed."}

        algo = _one(self.transport.query_algo(algo_client_id))
        algo_exchange_id = _exchange_id(algo.get("algoId"))
        if (algo.get("algoClOrdId") != algo_client_id or algo.get("instType") != "SPOT"
                or algo.get("instId") != plan["symbol"].replace("/", "-")
                or algo.get("tdMode") != "cash" or algo.get("side") != "sell" or algo.get("ordType") != "oco"):
            raise ValueError("Native protective OCO identity/type is not verified")
        _triggers(algo, plan)
        if algo.get("state") not in ALGO_STATES:
            raise ValueError("Unknown protective algo state")
        if evidence.get("algo_id", algo_exchange_id) != algo_exchange_id:
            raise ValueError("Protective algo identity changed")
        evidence["algo_id"] = algo_exchange_id
        algo_quantity = strict_decimal(algo.get("sz"))
        if not 0 < algo_quantity <= bought:
            raise ValueError("Invalid native protective quantity")
        residual, quote_flow, other_fees = entry_flow
        net_bought = residual
        children = algo.get("ordIdList")
        if not isinstance(children, list) or len(children) > 2 or len(set(children)) != len(children):
            raise ValueError("Missing or duplicate triggered child identities")
        children = [_exchange_id(value) for value in children]
        if not set(evidence.get("child_ids", [])).issubset(children):
            raise ValueError("Previously observed exit order disappeared")
        evidence["child_ids"] = children
        exit_reports, raw_children = [], {}
        for child_id in children:
            child = _one(self.transport.query_order(plan["symbol"].replace("/", "-"), child_id))
            summary = _order(child, plan, "sell", order_id=child_id,
                             algo_id=algo_exchange_id, algo_client_id=algo_client_id)
            if strict_decimal(summary["quantity"]) > algo_quantity:
                raise ValueError("Exit order exceeds protected quantity")
            _pin(evidence, "exit:" + child_id, summary)
            base_delta, quote_delta, child_fees = _cashflows(child, base, "sell", self.transport.query_fills(instrument, child_id))
            raw_children[child_id] = child
            residual += base_delta
            quote_flow += quote_delta
            for currency, amount in child_fees.items():
                other_fees[currency] = str(strict_decimal(other_fees.get(currency, "0")) + strict_decimal(amount))
            exit_reports.append(summary)
        # A stop may trigger between REST reads. Never combine evidence from
        # different snapshots into a false protected/closed classification.
        if (_one(self.transport.query_entry(instrument, entry_client_id)) != entry
                or _one(self.transport.query_algo(algo_client_id)) != algo
                or any(_one(self.transport.query_order(instrument, child_id)) != child
                       for child_id, child in raw_children.items())):
            raise ValueError("Order changed during reconciliation")
        observation = {"net_bought": str(net_bought), "residual_base": str(residual),
                       "quote_cashflow_usdt": str(quote_flow), "other_asset_fees": other_fees,
                       "algo_state": algo["state"], "protected_quantity": str(algo_quantity)}
        if residual < 0:
            observation["reason"] = "Exits consumed more inventory than the confirmed net entry."
            return "recovery_required", observation
        if algo["state"] == "live" and not children and algo_quantity == net_bought and algo.get("failCode", "") in ("", "0"):
            observation["reason"] = "Both native OCO triggers and the full net entry quantity were observed live."
            return "protected", observation
        if algo["state"] == "effective" and exit_reports:
            if all(child["state"] in ORDER_TERMINAL for child in exit_reports):
                if residual == 0 and not other_fees:
                    observation["realized_pnl_usdt"] = str(quote_flow)
                    observation["reason"] = "Terminal exits and signed fees reconcile to zero base inventory."
                    return "closed", observation
            elif residual > 0:
                observation["reason"] = "Triggered exit is still filling; no new entry is allowed."
                return "exit_pending", observation
        observation["reason"] = "Full native protection or zero residual inventory is not confirmed."
        return "recovery_required", observation


class OKXDemoTransport:
    """Dedicated demo credentials only; OKX shares a host but requires its demo header."""

    def __init__(self, api_key, api_secret, passphrase):
        import ccxt

        if not all(isinstance(value, str) and value.strip() for value in (api_key, api_secret, passphrase)):
            raise ValueError("Dedicated OKX Demo API key, secret and passphrase are required")
        self.credential_id = hashlib.sha256((TARGET + ":" + api_key).encode()).hexdigest()
        self.client = ccxt.okx({"apiKey": api_key, "secret": api_secret, "password": passphrase,
                               "enableRateLimit": True, "timeout": 5000,
                               "options": {"defaultType": "spot", "maxRetriesOnFailure": 0}})
        self.client.set_sandbox_mode(True)
        self.assert_demo()

    def assert_demo(self):
        if (self.client.urls["api"].get("rest") != "https://{hostname}"
                or self.client.hostname != "www.okx.com"
                or self.client.headers.get("x-simulated-trading") != "1"
                or self.client.options.get("sandboxMode") is not True
                or self.client.options.get("maxRetriesOnFailure") != 0):
            raise ValueError("Unverified or production endpoint/retry configuration refused")

    def submit(self, params):
        self.assert_demo()
        # Per-request option defeats any inherited CCXT retry configuration.
        return self.client.private_post_trade_order({**params, "maxRetriesOnFailure": 0})

    def query_entry(self, instrument, identity):
        self.assert_demo()
        return self.client.private_get_trade_order({"instId": instrument, "clOrdId": identity})

    def query_algo(self, identity):
        self.assert_demo()
        return self.client.private_get_trade_order_algo({"algoClOrdId": identity})

    def query_order(self, instrument, identity):
        self.assert_demo()
        return self.client.private_get_trade_order({"instId": instrument, "ordId": identity})

    def query_fills(self, instrument, identity):
        rows, seen, cursor = [], set(), None
        for _ in range(20):
            self.assert_demo()
            params = {"instType": "SPOT", "instId": instrument, "ordId": identity, "limit": "100"}
            if cursor is not None:
                params["after"] = cursor
            response = self.client.private_get_trade_fills(params)
            if not isinstance(response, dict) or response.get("code") != "0" or not isinstance(response.get("data"), list):
                raise ValueError("Invalid authenticated fill history response")
            page = response["data"]
            if len(page) > 100:
                raise ValueError("Unexpected fill history page size")
            for row in page:
                bill_id = _exchange_id(row.get("billId"))
                if bill_id in seen or (cursor is not None and int(bill_id) >= int(cursor)):
                    raise ValueError("Non-progressing fill history page")
                seen.add(bill_id)
            rows.extend(page)
            if len(page) < 100:
                return rows
            cursor = str(min(int(row["billId"]) for row in page))
        raise ValueError("Fill history pagination incomplete")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="okx-signal-demo.db")
    parser.add_argument("--plan", help="Fresh JSON plan: symbol, quantity, entry, stop, target, created_at")
    parser.add_argument("--ack", default="")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--submit", action="store_true", help="Submit one <=25 USDT DEMO order")
    actions.add_argument("--reconcile", metavar="BRACKET_ID")
    actions.add_argument("--resume", action="store_true", help="Read-only recovery of unresolved brackets")
    args = parser.parse_args(argv)
    if args.submit and (not args.plan or args.ack != ACKNOWLEDGEMENT):
        parser.error("--submit requires --plan and --ack 'PLACE OKX DEMO ORDERS'")
    try:
        transport = OKXDemoTransport(os.environ.get("OKX_DEMO_API_KEY", ""), os.environ.get("OKX_DEMO_API_SECRET", ""),
                                     os.environ.get("OKX_DEMO_API_PASSPHRASE", ""))
        lifecycle = OKXDemoLifecycle(transport, OKXDemoJournal(args.journal), transport.credential_id)
        if args.submit:
            result = lifecycle.submit(json.loads(Path(args.plan).read_text(encoding="utf-8")), args.ack)
        else:
            result = lifecycle.resume() if args.resume else lifecycle.reconcile(args.reconcile)
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "production_ready": False,
                          "message": "Demo qualification failed; reconcile the journal before any new attempt."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
