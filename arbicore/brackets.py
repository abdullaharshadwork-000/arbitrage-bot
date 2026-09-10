"""Durable, testnet-only native Binance OTOCO qualification lifecycle.

An acknowledgement is not proof of protection. Query the working and both
pending orders before classifying the position. Never retry an ambiguous POST.
"""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from .money import D


ACKNOWLEDGEMENT = "PLACE TESTNET ORDERS"
TERMINAL = {"closed", "no_fill"}
EXCHANGE_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
EXCHANGE_STATUSES = EXCHANGE_TERMINAL | {"NEW", "PENDING_NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}


def strict_decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing numeric exchange value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid numeric exchange value") from exc
    if not result.is_finite():
        raise ValueError("Non-finite exchange value")
    return result


def exchange_id(value):
    """Identifiers are integers, not rounded floats or coerced booleans."""
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("Invalid exchange identifier")
    return str(int(value))


def reconcile_fills(symbol, orders, trade_sets):
    """Exact per-order inventory and cash flow from authenticated trade records.

    Missing, duplicated, lagging or truncated trade pages never imply zero fees.
    External fee currencies remain unvalued instead of inventing a USDT price.
    """
    base = symbol[:-4]
    base_net, quote_net = D(0), D(0)
    fees, seen = {}, set()
    for role, order in orders.items():
        trades = trade_sets[role]
        if not isinstance(trades, list):
            raise ValueError("Invalid account trades response")
        quantity, quote = D(0), D(0)
        for trade in trades:
            identity = exchange_id(trade.get("id"))
            if identity in seen:
                raise ValueError("Duplicate account trade")
            seen.add(identity)
            if (trade.get("symbol") != symbol
                    or exchange_id(trade.get("orderId")) != order["order_id"]
                    or trade.get("isBuyer") is not (role == "entry")):
                raise ValueError("Account trade identity mismatch")
            amount, cost, fee, price = [strict_decimal(trade.get(k)) for k in ("qty", "quoteQty", "commission", "price")]
            asset = trade.get("commissionAsset")
            if amount <= 0 or cost <= 0 or price <= 0 or fee < 0:
                raise ValueError("Invalid account trade quantity or commission")
            if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", asset):
                raise ValueError("Invalid commission asset")
            quantity += amount
            quote += cost
            if fee:
                fees[asset] = fees.get(asset, D(0)) + fee
        if quantity != D(order["filled"]) or quote != D(order["quote_quantity"]):
            raise ValueError("Account trades do not reconcile to the order totals")
        base_net += quantity if role == "entry" else -quantity
        quote_net += -quote if role == "entry" else quote
    base_net -= fees.get(base, D(0))
    quote_net -= fees.get("USDT", D(0))
    external = {asset: str(cost) for asset, cost in fees.items() if asset not in {base, "USDT"}}
    return {"version": 2, "remaining_base": str(base_net), "quote_cash_flow": str(quote_net),
            "commissions": {asset: str(cost) for asset, cost in fees.items()},
            "unvalued_commissions": external, "trade_count": len(seen),
            "realized_pnl_usdt": str(quote_net) if base_net == 0 and not external else None}


class BracketJournal:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS signal_brackets (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, target TEXT NOT NULL,
                    status TEXT NOT NULL, plan TEXT NOT NULL, observation TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS signal_one_active_bracket
                    ON signal_brackets(owner,target) WHERE status NOT IN ('closed','no_fill');
                CREATE TABLE IF NOT EXISTS signal_bracket_events (
                    id INTEGER PRIMARY KEY, bracket_id TEXT NOT NULL,
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
        identity = uuid.uuid4().hex[:24]
        with self.db() as con:
            con.execute("INSERT INTO signal_brackets VALUES(?,?,?,?,?,?,?,?)",
                        (identity, owner, "binance-testnet", "intent", json.dumps(plan), "{}", time.time(), time.time()))
            con.execute("INSERT INTO signal_bracket_events(bracket_id,status,recorded_at) VALUES(?,?,?)",
                        (identity, "intent", time.time()))
        return identity

    def get(self, owner, identity):
        with self.db() as con:
            row = con.execute("SELECT status,plan,observation,target FROM signal_brackets WHERE id=? AND owner=?",
                              (identity, owner)).fetchone()
        if not row:
            raise ValueError("Bracket not found for this account")
        return {"id": identity, "status": row[0], "plan": json.loads(row[1]),
                "observation": json.loads(row[2]), "target": row[3]}

    def active(self, owner):
        with self.db() as con:
            rows = con.execute("SELECT id FROM signal_brackets WHERE owner=? AND status NOT IN ('closed','no_fill') ORDER BY created_at",
                               (owner,)).fetchall()
        return [row[0] for row in rows]

    def bind_legacy_owner(self, key_owner, account_owner):
        """Migrate this key's old journal after authenticated account identification.

        The unique account/environment index also refuses conflicting records.
        Other deleted keys cannot be identified retrospectively by this method.
        """
        with self.db() as con:
            con.execute("UPDATE signal_brackets SET owner=? WHERE owner=? AND target='binance-testnet'",
                        (account_owner, key_owner))

    def update(self, owner, identity, status, observation=None, expected=None):
        with self.db() as con:
            sql = "UPDATE signal_brackets SET status=?,observation=?,updated_at=? WHERE id=? AND owner=?"
            params = [status, json.dumps(observation or {}, allow_nan=False), time.time(), identity, owner]
            if expected is not None:
                sql += " AND status=? AND observation=?"
                params.extend([expected["status"], json.dumps(expected["observation"], allow_nan=False)])
            changed = con.execute(sql, params)
            if changed.rowcount != 1:
                if expected is not None and con.execute("SELECT 1 FROM signal_brackets WHERE id=? AND owner=?", (identity, owner)).fetchone():
                    return False  # A newer reconciliation already committed.
                raise ValueError("Bracket not found for this account")
            con.execute("INSERT INTO signal_bracket_events(bracket_id,status,recorded_at) VALUES(?,?,?)",
                        (identity, status, time.time()))
        return True


def client_ids(identity):
    return {"listClientOrderId": "acL" + identity, "workingClientOrderId": "acE" + identity,
            "pendingAboveClientOrderId": "acT" + identity, "pendingBelowClientOrderId": "acS" + identity}


def validate_plan(plan, now=None):
    if not re.fullmatch(r"[A-Z0-9]{1,20}USDT", str(plan.get("symbol", ""))):
        raise ValueError("Invalid Binance symbol")
    values = {k: strict_decimal(plan.get(k)) for k in ("quantity", "protected_quantity", "entry", "stop", "target")}
    if any(not v.is_finite() or v <= 0 for v in values.values()):
        raise ValueError("Plan values must be finite and positive")
    if not values["stop"] < values["entry"] < values["target"]:
        raise ValueError("Expected stop < entry < target")
    if values["protected_quantity"] > values["quantity"]:
        raise ValueError("Cannot protect more than the entry quantity")
    if values["entry"] * values["quantity"] > 25:
        raise ValueError("Qualification order exceeds the 25 USDT testnet cap")
    age = strict_decimal(time.time() if now is None else now) - strict_decimal(plan.get("created_at"))
    if not age.is_finite() or not 0 <= age <= 10:
        raise ValueError("Plan expired; rebuild with current market data")


class ProtectedTestnetLifecycle:
    def __init__(self, transport, journal, owner):
        self.transport, self.journal, self.owner = transport, journal, owner

    def submit(self, plan, acknowledgement):
        self.transport.assert_testnet()
        if acknowledgement != ACKNOWLEDGEMENT:
            raise ValueError("Explicit testnet-order acknowledgement required")
        validate_plan(plan)
        identity = self.journal.create(self.owner, plan)  # Durable before POST.
        params = {"symbol": plan["symbol"], "workingType": "LIMIT", "workingSide": "BUY",
                  "workingTimeInForce": "FOK", "workingPrice": plan["entry"],
                  "workingQuantity": plan["quantity"], "pendingSide": "SELL",
                  "pendingQuantity": plan["protected_quantity"],
                  "pendingAboveType": "TAKE_PROFIT", "pendingAboveStopPrice": plan["target"],
                  "pendingBelowType": "STOP_LOSS", "pendingBelowStopPrice": plan["stop"],
                  "newOrderRespType": "RESULT", **client_ids(identity)}
        try:
            self.transport.submit(params)
        except Exception:
            # Even a timeout or apparently rejected request may have matched.
            self.journal.update(self.owner, identity, "unknown_submission", expected={"status": "intent", "observation": {}})
        return self.reconcile(identity)

    def reconcile(self, identity):
        self.transport.assert_testnet()
        record = self.journal.get(self.owner, identity)
        if record["target"] != "binance-testnet":
            raise ValueError("Wrong execution environment")
        if record["status"] in TERMINAL and record["observation"].get("accounting", {}).get("version") == 2:
            return record
        plan, ids = record["plan"], client_ids(identity)
        reports = {}
        try:
            listing = self.transport.query_list(ids["listClientOrderId"])
            if listing.get("listClientOrderId") != ids["listClientOrderId"] or listing.get("symbol") != plan["symbol"]:
                raise ValueError("Order-list identity mismatch")
            members = listing.get("orders")
            if not isinstance(members, list) or len(members) != 3:
                raise ValueError("Incomplete native order list")
            member_ids = {r.get("clientOrderId"): exchange_id(r.get("orderId")) for r in members
                          if r.get("symbol") == plan["symbol"]}
            if set(member_ids) != {ids[k] for k in ("workingClientOrderId", "pendingAboveClientOrderId", "pendingBelowClientOrderId")}:
                raise ValueError("Native order-list membership mismatch")
            raw_reports = {}
            for role, key in (("entry", "workingClientOrderId"), ("target", "pendingAboveClientOrderId"), ("stop", "pendingBelowClientOrderId")):
                report = self.transport.query_order(plan["symbol"], ids[key])
                if report.get("clientOrderId") != ids[key] or report.get("symbol") != plan["symbol"]:
                    raise ValueError("Order identity mismatch")
                order_id = exchange_id(report.get("orderId"))
                if order_id != member_ids[ids[key]] or report.get("status") not in EXCHANGE_STATUSES:
                    raise ValueError("Order-list child or status mismatch")
                expected_side = "BUY" if role == "entry" else "SELL"
                if report.get("side") != expected_side:
                    raise ValueError("Order side mismatch")
                expected_type = {"entry": "LIMIT", "target": "TAKE_PROFIT", "stop": "STOP_LOSS"}[role]
                if report.get("type") != expected_type:
                    raise ValueError("Protective order type mismatch")
                if role == "entry":
                    if report.get("timeInForce") != "FOK" or strict_decimal(report.get("price")) != strict_decimal(plan["entry"]):
                        raise ValueError("Unbounded entry order")
                elif strict_decimal(report.get("stopPrice")) != strict_decimal(plan[role]):
                    raise ValueError("Protective trigger mismatch")
                filled = strict_decimal(report.get("executedQty"))
                quantity = strict_decimal(report.get("origQty"))
                quote_quantity = strict_decimal(report.get("cummulativeQuoteQty"))
                if not 0 <= filled <= quantity or quantity <= 0 or quote_quantity < 0:
                    raise ValueError("Invalid confirmed fill")
                if report["status"] == "FILLED" and filled != quantity:
                    raise ValueError("Filled status disagrees with quantity")
                expected_quantity = D(plan["quantity"] if role == "entry" else plan["protected_quantity"])
                if quantity != expected_quantity:
                    raise ValueError("Order quantity mismatch")
                previous = record["observation"].get("orders", {}).get(role)
                if previous and (previous["order_id"] != order_id or D(previous["filled"]) > filled
                                 or D(previous["quote_quantity"]) > quote_quantity
                                 or (previous["status"] in EXCHANGE_TERMINAL and previous["status"] != report["status"])):
                    raise ValueError("Previously confirmed order identity or fills regressed")
                reports[role] = {"status": report["status"], "filled": str(filled), "quantity": str(quantity),
                                 "order_id": order_id, "quote_quantity": str(quote_quantity)}
                raw_reports[role] = report
            trade_sets = {role: self.transport.query_trades(plan["symbol"], report["order_id"])
                          for role, report in reports.items()}
            accounting = reconcile_fills(plan["symbol"], reports, trade_sets)
            # Order and trade endpoints are not an atomic snapshot. A changed
            # fill/cancel during this read cycle requires a fresh reconciliation.
            fields = ("symbol", "clientOrderId", "orderId", "status", "origQty", "executedQty",
                      "cummulativeQuoteQty", "side", "type", "price", "stopPrice", "timeInForce")
            for role, previous in raw_reports.items():
                current = self.transport.query_order(plan["symbol"], previous["clientOrderId"])
                if any(current.get(k) != previous.get(k) for k in fields):
                    raise ValueError("Order changed during reconciliation")
            entry, stop, target = reports["entry"], reports["stop"], reports["target"]
            bought = D(entry["filled"])
            sold = D(stop["filled"]) + D(target["filled"])
            requested, protected = D(plan["quantity"]), D(plan["protected_quantity"])
            remaining = D(accounting["remaining_base"])
            if D(entry["quantity"]) != requested or any(D(r["quantity"]) != protected for r in (stop, target)):
                raise ValueError("Order quantity mismatch")
            all_terminal = all(r["status"] in EXCHANGE_TERMINAL for r in reports.values())
            if bought == 0 and sold == 0 and all_terminal:
                status = "no_fill"
            elif sold > bought:
                status = "recovery_required"
            elif bought != requested or entry["status"] != "FILLED":
                status = "recovery_required" if bought > 0 else "entry_pending"
            elif sold > protected:
                status = "recovery_required"
            elif all_terminal and remaining == 0:
                status = "valuation_required" if accounting["unvalued_commissions"] else "closed"
            elif (remaining > 0 and stop["status"] == "NEW" and target["status"] == "NEW"
                  and sold == 0 and remaining == protected):
                status = "protected"
            else:
                status = "recovery_required"
            observation = {"orders": reports, "gross_bought": str(bought), "gross_sold": str(sold),
                           "residual_before_fees": str(bought - sold),
                           "accounting": accounting,
                           "note": "Net inventory includes confirmed commissions. Residual inventory or unvalued fees block completion; production remains disabled."}
        except Exception as exc:
            status, observation = "reconciliation_required", {"error_type": type(exc).__name__}
            evidence = {**record["observation"].get("orders", {}), **reports}
            if evidence:
                observation["orders"] = evidence
                observation["stale"] = True
        self.journal.update(self.owner, identity, status, observation, expected=record)
        return self.journal.get(self.owner, identity)

    def reconcile_active(self):
        """Read-only restart recovery. Never create, cancel, or repeat an order."""
        return [self.reconcile(identity) for identity in self.journal.active(self.owner)]


class BinanceTestnetTransport:
    """Only constructed with explicitly supplied testnet credentials."""
    def __init__(self, api_key, api_secret):
        import ccxt
        if not api_key or not api_secret:
            raise ValueError("Dedicated Binance Testnet credentials are required")
        self.client = ccxt.binance({"apiKey": api_key, "secret": api_secret,
                                   "enableRateLimit": True, "timeout": 5000,
                                   "options": {"defaultType": "spot", "fetchCurrencies": False,
                                               "maxRetriesOnFailure": 0}})
        self.client.set_sandbox_mode(True)
        self.assert_testnet()

    def assert_testnet(self):
        if self.client.options.get("maxRetriesOnFailure") != 0:
            raise ValueError("Automatic order retries are forbidden")
        for key in ("public", "private"):
            if self.client.urls["api"].get(key) != "https://testnet.binance.vision/api/v3":
                raise ValueError("Non-testnet endpoint refused")

    def submit(self, params):
        self.assert_testnet()
        return self.client.private_post_orderlist_otoco({**params, "maxRetriesOnFailure": 0})

    def query_list(self, identity):
        self.assert_testnet()
        return self.client.private_get_orderlist({"origClientOrderId": identity})

    def query_order(self, symbol, identity):
        self.assert_testnet()
        return self.client.private_get_order({"symbol": symbol, "origClientOrderId": identity})

    def query_trades(self, symbol, order_id):
        self.assert_testnet()
        # Bounded pagination; truncation/errors block reconciliation, not fees=0.
        rows, cursor = [], None
        for _ in range(20):
            params = {"symbol": symbol, "orderId": exchange_id(order_id), "limit": 1000}
            if cursor is not None:
                params["fromId"] = cursor
            page = self.client.private_get_mytrades(params)
            if not isinstance(page, list) or len(page) > 1000:
                raise ValueError("Invalid trade history page")
            ids = [int(exchange_id(row.get("id"))) for row in page]
            if ids != sorted(set(ids)) or (ids and cursor is not None and ids[0] < cursor):
                raise ValueError("Non-progressing trade history page")
            rows.extend(page)
            if len(page) < 1000:
                return rows
            cursor = ids[-1] + 1
        raise ValueError("Trade history pagination limit exceeded")

    def account_owner(self):
        self.assert_testnet()
        account = self.client.private_get_account()
        uid = exchange_id(account.get("uid"))
        if int(uid) <= 0 or account.get("accountType") != "SPOT":
            raise ValueError("Verified Spot account identity required")
        return "account:" + hashlib.sha256(("binance-testnet:" + uid).encode()).hexdigest()
