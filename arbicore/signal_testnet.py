"""Explicit operator qualification tool. No production endpoint or auto-start.

python -m arbicore.signal_testnet --help
"""
import argparse
import hashlib
import json
import os
import time

from .brackets import (ACKNOWLEDGEMENT, BinanceTestnetTransport, BracketJournal,
                       ProtectedTestnetLifecycle, validate_plan, strict_decimal)
from .money import D
from .signals import analyse, market_limit_error, validate_book


def build_plan(transport, symbol, notional):
    transport.assert_testnet()
    client = transport.client
    budget = strict_decimal(notional)
    if not budget.is_finite() or not 10 <= budget <= 25:
        raise ValueError("Testnet notional must be between 10 and 25 USDT")
    if not symbol.endswith("/USDT"):
        raise ValueError("Only USDT spot pairs are supported")
    client.load_markets()
    market = client.market(symbol)
    signal = analyse(client.fetch_ohlcv(symbol, "1m", limit=80), time.time() * 1000)
    if signal["action"] != "buy":
        raise ValueError(f"No qualified entry signal: {signal['reason']}")
    commission = client.private_get_account_commission({"symbol": market["id"]})
    standard = commission.get("standardCommission") or {}
    if standard.get("taker") is None:
        raise ValueError("Verified commission unavailable")
    # Include side-specific commission; do not assume that BNB discounts apply.
    rates = []
    for section in ("standardCommission", "specialCommission", "taxCommission"):
        values = commission.get(section) or {}
        parts = [strict_decimal(values.get(key, 0)) for key in ("taker", "buyer", "seller")]
        if any(part < 0 for part in parts):
            raise ValueError("Negative account commission response")
        rates.append(parts[0] + max(parts[1], parts[2]))
    fee = sum(rates, D(0))
    if any(not r.is_finite() or r < 0 for r in rates) or not 0 <= fee < D(".05"):
        raise ValueError("Invalid account commission response")
    account = client.fetch_balance()
    free = strict_decimal((account.get("USDT") or {}).get("free"))
    if not free.is_finite() or free < budget * D("1.02"):
        raise ValueError("Insufficient testnet USDT including the reserve")
    started = time.monotonic()
    book = client.fetch_order_book(symbol, limit=50)
    if time.monotonic() - started > 3:
        raise ValueError("Order book response too slow")
    validate_book(book, time.time())
    ask = D(book["asks"][0][0])
    entry = D(client.price_to_precision(symbol, ask * D("1.0025")))
    quantity = D(client.amount_to_precision(symbol, budget / (entry * (1 + fee))))
    protected = D(client.amount_to_precision(symbol, quantity * (1 - fee)))
    stop_fraction = min(D(".03"), max(D(".005"), D(2) * D(signal["atr14"]) / ask))
    stop = D(client.price_to_precision(symbol, ask * (1 - stop_fraction)))
    target = D(client.price_to_precision(symbol, entry * (1 + 2 * stop_fraction + 2 * fee)))
    if stop >= D(book["bids"][0][0]):
        raise ValueError("Stop trigger is already at or above the current bid")
    # Reject unsupported protective order types before placing the entry.
    order_types = (market.get("info") or {}).get("orderTypes") or []
    if not {"LIMIT", "STOP_LOSS", "TAKE_PROFIT"}.issubset(set(order_types)):
        raise ValueError("This market does not advertise the required native bracket order types")
    for amount, price in ((quantity, entry), (protected, stop), (protected, target)):
        reason = market_limit_error(market, amount, amount * price)
        if reason:
            raise ValueError(reason)
    plan = {"symbol": market["id"], "quantity": str(quantity), "protected_quantity": str(protected),
            "entry": str(entry), "stop": str(stop), "target": str(target),
            "estimated_fee_rate": str(fee), "created_at": time.time(),
            "signal_candle": signal["candle_time"]}
    validate_plan(plan)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", default="signal-testnet.db")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--notional", default="15")
    parser.add_argument("--reconcile", metavar="BRACKET_ID", help="Read exchange state for an existing bracket; never resubmits")
    parser.add_argument("--reconcile-active", action="store_true", help="Read-only restart recovery for all unresolved brackets")
    parser.add_argument("--submit", action="store_true", help="Place testnet orders; absent means read-only plan")
    parser.add_argument("--ack", default="", help=f"Required with --submit: {ACKNOWLEDGEMENT}")
    args = parser.parse_args(argv)
    if sum(bool(value) for value in (args.reconcile, args.reconcile_active, args.submit)) > 1:
        parser.error("Reconcile, reconcile-active and submit are mutually exclusive")
    if args.submit and args.ack != ACKNOWLEDGEMENT:
        parser.error("Explicit testnet acknowledgement is required")
    key = os.environ.get("BINANCE_TESTNET_API_KEY", "")
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
    try:
        transport = BinanceTestnetTransport(key, secret)
        owner = transport.account_owner()
        journal = BracketJournal(args.journal)
        journal.bind_legacy_owner(hashlib.sha256(key.encode()).hexdigest(), owner)
        lifecycle = ProtectedTestnetLifecycle(transport, journal, owner)
        if args.reconcile:
            result = lifecycle.reconcile(args.reconcile)
        elif args.reconcile_active:
            result = {"submitted": False, "records": lifecycle.reconcile_active(), "production_ready": False}
        else:
            if args.submit and journal.active(owner):
                raise ValueError("Unresolved testnet bracket exists; run --reconcile-active before attempting another entry")
            plan = build_plan(transport, args.symbol, args.notional)
            result = (lifecycle.submit(plan, args.ack) if args.submit else
                      {"target": "binance-testnet", "submitted": False, "plan": plan})
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        # Avoid dumping exchange payloads, URLs or credential-bearing tracebacks.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else
                          "Exchange or persistence operation failed. Reconcile existing journal entries before retrying."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
