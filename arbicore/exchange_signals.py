"""Venue-specific signal execution capabilities, never a generic order fallback.

Native plans describe requests for adapter testing; a plan is not permission to
submit it. Dedicated test/demo tools are separate from the web trading engine.
"""
import copy
import re

from .brackets import BinanceTestnetTransport, strict_decimal


CAPABILITIES = {
    "binance": {"label": "Binance", "environment": "Spot Testnet", "demo_available": True,
                "native_protection": "OTOCO: FOK entry with linked stop and target",
                "submission_adapter": True, "requires_passphrase": False,
                "blocker": "Fee-aware bracket/restart tooling is mock-tested; authenticated qualification and automatic residual recovery are pending"},
    "kucoin": {"label": "KuCoin", "environment": "No verified sandbox", "demo_available": False,
               "native_protection": "Standalone OCO is not treated as an atomic protected entry",
               "submission_adapter": False, "requires_passphrase": True,
               "blocker": "Former sandbox suspended; replacement and entry/protection recovery are not verified"},
    "okx": {"label": "OKX", "environment": "Demo Trading", "demo_available": True,
            "native_protection": "Attached TP/SL algorithms on Spot entry",
            "submission_adapter": True, "requires_passphrase": True,
            "blocker": "Demo-only attached OCO and fill-reconciliation adapter is mock-tested, not authenticated or production-qualified"},
    "bybit": {"label": "Bybit", "environment": "Testnet", "demo_available": True,
              "native_protection": "Spot limit entry with attached TP/SL",
              "submission_adapter": True, "requires_passphrase": False,
              "blocker": "Testnet entry/fill adapter is mock-tested; Spot protective-child linkage and exits remain unverified"},
}


def capabilities():
    return [{"exchange": venue, "paper_signals": True, "production_ready": False,
             "authenticated_qualification": False, **copy.deepcopy(data)}
            for venue, data in CAPABILITIES.items()]


def native_plan(exchange, symbol, quantity, protected_quantity, entry, stop, target, identity):
    if exchange not in CAPABILITIES:
        raise ValueError("Unsupported exchange")
    if exchange == "kucoin":
        raise ValueError(CAPABILITIES[exchange]["blocker"])
    if not re.fullmatch(r"[A-Z0-9]+/USDT", symbol) or not re.fullmatch(r"[a-zA-Z0-9]{1,24}", identity):
        raise ValueError("Invalid spot symbol or client identity")
    q, pq, e, s, t = map(strict_decimal, (quantity, protected_quantity, entry, stop, target))
    if not 0 < pq <= q or not 0 < s < e < t:
        raise ValueError("Invalid bracket values")
    if q * e > 25:
        raise ValueError("Demo qualification cap is 25 USDT")
    if exchange == "binance":
        return {"symbol": symbol.replace("/", ""), "workingType": "LIMIT", "workingSide": "BUY",
                "workingTimeInForce": "FOK", "workingPrice": str(e), "workingQuantity": str(q),
                "pendingSide": "SELL", "pendingQuantity": str(pq),
                "pendingAboveType": "TAKE_PROFIT", "pendingAboveStopPrice": str(t),
                "pendingBelowType": "STOP_LOSS", "pendingBelowStopPrice": str(s),
                "listClientOrderId": "acL" + identity, "workingClientOrderId": "acE" + identity,
                "pendingAboveClientOrderId": "acT" + identity, "pendingBelowClientOrderId": "acS" + identity}
    if exchange == "okx":
        return {"instId": symbol.replace("/", "-"), "tdMode": "cash", "side": "buy",
                "ordType": "fok", "sz": str(q), "px": str(e), "clOrdId": "acE" + identity,
                "attachAlgoOrds": [{"attachAlgoClOrdId": "acP" + identity,
                                    "tpTriggerPx": str(t), "tpOrdPx": "-1", "tpTriggerPxType": "last",
                                    "slTriggerPx": str(s), "slOrdPx": "-1", "slTriggerPxType": "last"}]}
    return {"category": "spot", "symbol": symbol.replace("/", ""), "side": "Buy",
            "orderType": "Limit", "timeInForce": "FOK", "qty": str(q), "price": str(e),
            "orderLinkId": "acE" + identity, "takeProfit": str(t), "stopLoss": str(s),
            "tpOrderType": "Market", "slOrderType": "Market", "isLeverage": 0}


class DemoDiagnostics:
    """Read-only clients for dedicated demo/testnet keys; never place orders."""
    def __init__(self, exchange, api_key, api_secret, passphrase=""):
        import ccxt
        if exchange not in CAPABILITIES:
            raise ValueError("Unsupported exchange")
        profile = CAPABILITIES[exchange]
        if not profile["demo_available"]:
            raise ValueError(profile["blocker"])
        if not api_key or not api_secret or (profile["requires_passphrase"] and not passphrase):
            raise ValueError("Dedicated demo/testnet credentials are incomplete")
        self.exchange = exchange
        if exchange == "binance":
            self.client = BinanceTestnetTransport(api_key, api_secret).client
        else:
            self.client = getattr(ccxt, exchange)({"apiKey": api_key, "secret": api_secret,
                "password": passphrase, "enableRateLimit": True, "timeout": 5000,
                "options": {"defaultType": "spot"}})
            self.client.set_sandbox_mode(True)
        self.assert_demo()

    def assert_demo(self):
        client = self.client
        if self.exchange == "binance":
            valid = all(client.urls["api"].get(k) == "https://testnet.binance.vision/api/v3" for k in ("public", "private"))
        elif self.exchange == "okx":
            valid = (client.urls["api"].get("rest") == "https://{hostname}"
                     and client.hostname == "www.okx.com"
                     and str(client.headers.get("x-simulated-trading")) == "1")
        elif self.exchange == "bybit":
            valid = (all(client.urls["api"].get(k) == "https://api-testnet.{hostname}" for k in ("public", "private"))
                     and client.hostname == "bybit.com")
        else:
            valid = False
        if not valid:
            raise ValueError("Unverified or production endpoint refused")

    def check(self):
        self.assert_demo()
        self.client.fetch_balance({"type": "spot"})
        return {"exchange": self.exchange, "authenticated_read": True,
                "orders_submitted": False, "production_ready": False,
                "blocker": CAPABILITIES[self.exchange]["blocker"]}
