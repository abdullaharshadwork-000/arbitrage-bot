"""Immutable settings and credentials that never print themselves.

Two problems this replaces.

First, configuration lived in module-global variables that the web API mutated
at runtime with `globals()[name] = value`. A scan running in the background
thread could therefore read `TRADE_SIZE_USDT` halfway through being changed,
and nothing recorded which values a given trade was executed under. `Settings`
is frozen: an update produces a new object, and the engine picks it up between
scans instead of mid-scan.

Second, API keys were literal strings in `live_config.py`. That file is
gitignored, which protects the repository and nothing else — the keys are still
on disk in plaintext, still in editor backups, and still printable by any
`print(CONFIG)` someone adds while debugging. `Credentials` loads from the
environment by default and its repr is redacted, so a stray log line cannot
leak a secret.
"""

import os
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .money import D, HUNDRED, ZERO

# Data sources.
MODE_DEMO = "demo"          # synthetic prices, no network
MODE_LIVE = "live"          # real public market data
MODES = (MODE_DEMO, MODE_LIVE)

# What happens to the orders.
EXECUTION_PAPER = "paper"   # simulated fills against real books
EXECUTION_REAL = "real"     # actual money
EXECUTION_MODES = (EXECUTION_PAPER, EXECUTION_REAL)

STRATEGY_CROSS = "cross_exchange"
STRATEGY_TRIANGULAR = "triangular"
STRATEGIES = (STRATEGY_CROSS, STRATEGY_TRIANGULAR, "signal_trend")

# Typing this exact string is the last gate before real orders. It exists so
# that enabling live trading cannot happen by flipping one boolean that
# defaulted to True in a config file someone copied.
REAL_TRADING_ACK = "I ACCEPT REAL LOSSES"

# A hard ceiling no config file can raise. If a bug multiplies the trade size,
# this is what stops it from being unbounded.
ABSOLUTE_MAX_TRADE_SIZE_USDT = Decimal("5000")

ENV_PREFIX = "ARBI"
PASSPHRASE_EXCHANGES = frozenset({"kucoin", "okx"})


def _env_name(exchange, suffix):
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(exchange)).upper()
    return f"{ENV_PREFIX}_{clean}_{suffix}"


@dataclass(frozen=True)
class Credentials:
    """One exchange's keys. Never renders the secret, in any context."""

    exchange: str
    api_key: str = ""
    api_secret: str = ""
    password: str = ""      # ccxt calls the KuCoin/OKX passphrase `password`
    source: str = "unset"   # where it came from, for the readiness report

    @property
    def complete(self):
        return not self.missing()

    @property
    def requires_password(self):
        """Whether CCXT needs an API passphrase in addition to key + secret."""
        return self.exchange.lower() in PASSPHRASE_EXCHANGES

    def missing(self):
        gaps = []
        if not self.api_key:
            gaps.append("api_key")
        if not self.api_secret:
            gaps.append("api_secret")
        if self.requires_password and not self.password:
            gaps.append("password")
        return gaps

    @staticmethod
    def _mask(value):
        if not value:
            return ""
        tail = value[-4:] if len(value) > 8 else ""
        return f"***{tail}" if tail else "***"

    def redacted(self):
        return {
            "exchange": self.exchange,
            "api_key": self._mask(self.api_key),
            "api_secret": "***" if self.api_secret else "",
            "password": "***" if self.password else "",
            "complete": self.complete,
            "source": self.source,
        }

    def __repr__(self):
        return (f"Credentials(exchange={self.exchange!r}, "
                f"api_key={self._mask(self.api_key)!r}, api_secret=***, "
                f"complete={self.complete})")

    __str__ = __repr__

    def ccxt_params(self):
        """The dict ccxt wants. The only place the real values are returned."""
        params = {"apiKey": self.api_key, "secret": self.api_secret}
        if self.password:
            params["password"] = self.password
        return params

    @classmethod
    def from_env(cls, exchange, environ=None):
        env = environ if environ is not None else os.environ
        key = env.get(_env_name(exchange, "API_KEY"), "").strip()
        secret = env.get(_env_name(exchange, "API_SECRET"), "").strip()
        password = env.get(_env_name(exchange, "PASSWORD"), "").strip()
        source = "env" if (key or secret or password) else "unset"
        return cls(str(exchange), key, secret, password, source)

    @classmethod
    def from_mapping(cls, exchange, mapping, source="config"):
        data = mapping or {}
        key = str(data.get("apiKey") or data.get("api_key") or "").strip()
        secret = str(data.get("secret") or data.get("api_secret") or "").strip()
        password = str(data.get("password") or data.get("passphrase") or "").strip()
        return cls(str(exchange), key, secret, password,
                   source if (key or secret or password) else "unset")


def load_credentials(exchanges, fallback=None, environ=None):
    """Environment first, then any in-process mapping, per exchange.

    The environment wins so that a developer's `live_config.py` cannot silently
    override the keys an operator set for a deployment.
    """
    resolved = {}
    for exchange in exchanges:
        from_env = Credentials.from_env(exchange, environ)
        if from_env.complete:
            resolved[exchange] = from_env
            continue
        supplied = (fallback or {}).get(exchange)
        resolved[exchange] = (Credentials.from_mapping(exchange, supplied)
                              if supplied else from_env)
    return resolved


# Every field the web API is allowed to change, and how to coerce it. Anything
# not listed here is rejected rather than ignored, so a typo in a PUT body is
# an error instead of a setting that silently did nothing.
_DECIMAL_FIELDS = frozenset({
    "start_cash_per_exchange", "trade_size_usdt", "taker_fee", "min_profit_pct",
    "max_slippage_pct", "max_daily_loss_usdt", "max_position_notional_usdt",
    "min_notional_usdt", "rebalance_skew_trigger",
})
_INT_FIELDS = frozenset({
    "check_interval", "latency_ms", "max_consecutive_failures",
    "max_orders_per_minute", "max_triangular_routes", "order_book_depth",
    "max_clock_skew_ms",
})
_BOOL_FIELDS = frozenset({"allow_partial_fills", "halt_on_stranded_position"})
_STR_FIELDS = frozenset({
    "mode", "execution_mode", "strategy", "quote_currency", "log_file",
    "db_file", "real_trading_ack", "alert_webhook",
})
_TUPLE_FIELDS = frozenset({"exchanges", "symbols"})

MUTABLE_FIELDS = (_DECIMAL_FIELDS | _INT_FIELDS | _BOOL_FIELDS
                  | _STR_FIELDS | _TUPLE_FIELDS)


@dataclass(frozen=True)
class Settings:
    """One immutable snapshot of every knob the engine reads."""

    mode: str = MODE_DEMO
    execution_mode: str = EXECUTION_PAPER
    strategy: str = STRATEGY_CROSS
    exchanges: tuple = ("binance", "kucoin", "okx", "bybit")
    symbols: tuple = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT")
    quote_currency: str = "USDT"

    start_cash_per_exchange: Decimal = Decimal("1000")
    trade_size_usdt: Decimal = Decimal("200")
    min_notional_usdt: Decimal = Decimal("10")
    taker_fee: Decimal = Decimal("0.001")
    min_profit_pct: Decimal = Decimal("0.15")
    max_slippage_pct: Decimal = Decimal("0.25")

    check_interval: int = 5
    latency_ms: int = 250
    order_book_depth: int = 20
    max_triangular_routes: int = 120
    max_clock_skew_ms: int = 2000

    max_daily_loss_usdt: Decimal = Decimal("50")
    max_position_notional_usdt: Decimal = Decimal("400")
    max_consecutive_failures: int = 3
    max_orders_per_minute: int = 20
    rebalance_skew_trigger: Decimal = Decimal("0.85")
    halt_on_stranded_position: bool = True
    allow_partial_fills: bool = True

    log_file: str = "trades.csv"
    db_file: str = "arbicore.db"
    alert_webhook: str = ""
    real_trading_ack: str = ""
    credentials: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        """Normalize types however the object was built.

        Without this, `Settings(max_daily_loss_usdt="20")` holds a string, and
        the first comparison against a Decimal raises a TypeError from inside a
        risk check — the worst possible place to discover a config type error.
        Frozen dataclasses forbid plain assignment, hence object.__setattr__.
        """
        for name in _DECIMAL_FIELDS | _INT_FIELDS | _BOOL_FIELDS | _TUPLE_FIELDS:
            current = getattr(self, name)
            coerced = self.coerce(name, current)
            if coerced != current or type(coerced) is not type(current):
                object.__setattr__(self, name, coerced)
        for name in _STR_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str):
                object.__setattr__(self, name, str(value if value is not None else ""))

    # ---------------------------------------------------------------- basics

    @property
    def real(self):
        """True only when real orders are actually permitted."""
        return (self.execution_mode == EXECUTION_REAL
                and self.mode == MODE_LIVE
                and self.real_trading_ack == REAL_TRADING_ACK)

    @property
    def paper(self):
        return not self.real

    def credential_for(self, exchange):
        return self.credentials.get(exchange, Credentials(str(exchange)))

    def with_credentials(self, credentials):
        return replace(self, credentials=dict(credentials or {}))

    def load_env_credentials(self, environ=None, fallback=None):
        return self.with_credentials(
            load_credentials(self.exchanges, fallback=fallback, environ=environ))

    # ---------------------------------------------------------------- updates

    @staticmethod
    def coerce(name, value):
        """Turn one API-supplied value into the type the field holds."""
        if name not in MUTABLE_FIELDS:
            raise KeyError(f"{name} is not a configurable setting")
        if name in _DECIMAL_FIELDS:
            coerced = D(value, default=None)
            if coerced is None:
                raise ValueError(f"{name} must be a number, got {value!r}")
            return coerced
        if name in _INT_FIELDS:
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a whole number, got {value!r}")
        if name in _BOOL_FIELDS:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if name in _TUPLE_FIELDS:
            if isinstance(value, str):
                items = [part.strip() for part in value.split(",")]
            else:
                items = [str(item).strip() for item in (value or [])]
            return tuple(item for item in items if item)
        return str(value if value is not None else "")

    def updated(self, **overrides):
        """A new Settings with these changes applied, or raise.

        Validation runs on the *result*, so a pair of changes that are only
        valid together (raising the trade size and the position cap) succeeds,
        while a change that leaves the config incoherent fails before the
        engine ever sees it.
        """
        coerced = {name: self.coerce(name, value)
                   for name, value in overrides.items()}
        candidate = replace(self, **coerced)
        problems = candidate.validate()
        if problems:
            raise ValueError("; ".join(problems))
        return candidate

    # ------------------------------------------------------------- validation

    def validate(self):
        """Every reason this configuration should not run, as plain sentences."""
        problems = []
        if self.mode not in MODES:
            problems.append(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.execution_mode not in EXECUTION_MODES:
            problems.append(f"execution_mode must be one of {EXECUTION_MODES}, "
                            f"got {self.execution_mode!r}")
        if self.strategy not in STRATEGIES:
            problems.append(f"strategy must be one of {STRATEGIES}, "
                            f"got {self.strategy!r}")
        if not self.exchanges:
            problems.append("no exchanges configured")
        if self.strategy == STRATEGY_CROSS and len(self.exchanges) < 2:
            problems.append("cross-exchange arbitrage needs at least two exchanges")
        if self.strategy == "signal_trend":
            if self.execution_mode != EXECUTION_PAPER or self.mode != MODE_LIVE:
                problems.append("Signal trend requires live-data paper execution; real orders are not supported")
            if len(self.exchanges) != 1:
                problems.append("Signal trend requires exactly one exchange")
            if any(not symbol.endswith("/USDT") for symbol in self.symbols):
                problems.append("Signal trend supports USDT spot pairs only")
        if not self.symbols:
            problems.append("no symbols configured")

        if self.trade_size_usdt <= ZERO:
            problems.append("trade_size_usdt must be positive")
        if self.trade_size_usdt > ABSOLUTE_MAX_TRADE_SIZE_USDT:
            problems.append(
                f"trade_size_usdt {float(self.trade_size_usdt)} exceeds the "
                f"hard cap of {float(ABSOLUTE_MAX_TRADE_SIZE_USDT)}")
        if self.trade_size_usdt < self.min_notional_usdt:
            problems.append(
                f"trade_size_usdt {float(self.trade_size_usdt)} is below the "
                f"{float(self.min_notional_usdt)} minimum most exchanges enforce")
        if self.trade_size_usdt > self.max_position_notional_usdt:
            # Both numbers, because the fix is to change one of them and the
            # operator is reading this in a notice bar with neither in view.
            problems.append(
                f"trade_size_usdt {float(self.trade_size_usdt)} exceeds "
                f"max_position_notional_usdt "
                f"{float(self.max_position_notional_usdt)}")
        if not (ZERO <= self.taker_fee < Decimal("0.05")):
            problems.append("taker_fee must be between 0 and 0.05")
        if self.max_slippage_pct < ZERO:
            problems.append("max_slippage_pct cannot be negative")

        # min_profit_pct is a *net* floor: fees are already subtracted before
        # the comparison, so it is compared against zero, not against the fee.
        # Requiring it to be strictly positive is what stops a "profitable"
        # trade that only breaks even from being worth the execution risk.
        if self.min_profit_pct <= ZERO:
            problems.append("min_profit_pct must be positive - a zero net floor "
                            "accepts trades that cannot pay for their own risk")

        if self.check_interval < 1:
            problems.append("check_interval must be at least 1 second")
        if self.latency_ms < 0:
            problems.append("latency_ms cannot be negative")
        if self.order_book_depth < 5:
            problems.append("order_book_depth must be at least 5 levels")
        if self.max_triangular_routes < 1:
            problems.append("max_triangular_routes must be at least 1")
        if self.max_orders_per_minute < 1:
            problems.append("max_orders_per_minute must be at least 1")
        if self.max_consecutive_failures < 1:
            problems.append("max_consecutive_failures must be at least 1")
        if self.max_daily_loss_usdt <= ZERO:
            problems.append("max_daily_loss_usdt must be positive - an unlimited "
                            "loss budget is not a risk limit")
        if not (ZERO < self.rebalance_skew_trigger <= Decimal("1")):
            problems.append("rebalance_skew_trigger must be between 0 and 1")
        problems.extend(self._real_trading_problems())
        return problems

    def _real_trading_problems(self):
        """Extra requirements that only apply when money is at stake."""
        if self.execution_mode != EXECUTION_REAL:
            return []
        problems = []
        if self.mode != MODE_LIVE:
            problems.append("real execution requires live market data, "
                            f"not mode={self.mode!r}")
        if self.real_trading_ack != REAL_TRADING_ACK:
            problems.append(
                "real execution requires real_trading_ack to be set to the exact "
                f"string {REAL_TRADING_ACK!r}")
        for exchange in self.exchanges:
            gaps = self.credential_for(exchange).missing()
            if gaps:
                problems.append(f"{exchange} is missing {', '.join(gaps)} "
                                f"(set {_env_name(exchange, 'API_KEY')} and "
                                f"{_env_name(exchange, 'API_SECRET')})")
        return problems

    def blocking_reason(self):
        """One sentence for the dashboard, or None when it is safe to run."""
        problems = self.validate()
        return problems[0] if problems else None

    def warnings(self):
        """Legal but unwise combinations. These do not block a start.

        Kept separate from `validate()` because the shipped defaults sit in
        here: a 0.15% net floor with 0.25% of slippage tolerance per leg is
        runnable, and it is also a configuration where a fill that passes both
        leg checks can still lose money. The post-fill recheck in the simulator
        is what actually catches that, but the operator should know.
        """
        notes = []
        two_legs = self.max_slippage_pct * 2
        if two_legs > self.min_profit_pct:
            notes.append(
                f"slippage tolerance across two legs ({float(two_legs)}%) exceeds "
                f"the {float(self.min_profit_pct)}% net profit floor, so fills that "
                f"pass each leg check can still end up negative")
        round_trip = self.taker_fee * 2 * HUNDRED
        if self.min_profit_pct < round_trip:
            notes.append(
                f"the {float(self.min_profit_pct)}% net floor is thinner than the "
                f"{float(round_trip)}% round-trip fee, so most of the gross spread "
                f"goes to the exchange")
        if self.strategy == STRATEGY_TRIANGULAR and self.max_triangular_routes > 300:
            notes.append(
                f"{self.max_triangular_routes} triangular routes per scan will not "
                f"finish inside a {self.check_interval}s interval")
        if self.latency_ms == 0 and self.execution_mode == EXECUTION_PAPER:
            notes.append("latency_ms=0 makes paper fills optimistic in exactly the "
                         "way the old simulator was")
        if self.real and not self.alert_webhook:
            notes.append("real trading with no alert webhook: a halt at 3am will "
                         "go unnoticed until someone opens the dashboard")
        return notes

    # -------------------------------------------------------------- rendering

    def as_dict(self):
        """JSON-safe view. Credentials appear only in redacted form."""
        return {
            "mode": self.mode,
            "execution_mode": self.execution_mode,
            "strategy": self.strategy,
            "exchanges": list(self.exchanges),
            "symbols": list(self.symbols),
            "quote_currency": self.quote_currency,
            "start_cash_per_exchange": float(self.start_cash_per_exchange),
            "trade_size_usdt": float(self.trade_size_usdt),
            "min_notional_usdt": float(self.min_notional_usdt),
            "taker_fee": float(self.taker_fee),
            "min_profit_pct": float(self.min_profit_pct),
            "max_slippage_pct": float(self.max_slippage_pct),
            "check_interval": self.check_interval,
            "latency_ms": self.latency_ms,
            "order_book_depth": self.order_book_depth,
            "max_triangular_routes": self.max_triangular_routes,
            "max_clock_skew_ms": self.max_clock_skew_ms,
            "max_daily_loss_usdt": float(self.max_daily_loss_usdt),
            "max_position_notional_usdt": float(self.max_position_notional_usdt),
            "max_consecutive_failures": self.max_consecutive_failures,
            "max_orders_per_minute": self.max_orders_per_minute,
            "rebalance_skew_trigger": float(self.rebalance_skew_trigger),
            "halt_on_stranded_position": self.halt_on_stranded_position,
            "allow_partial_fills": self.allow_partial_fills,
            "log_file": self.log_file,
            "db_file": self.db_file,
            "alert_webhook_configured": bool(self.alert_webhook),
            "real_trading_enabled": self.real,
            "credentials": {name: cred.redacted()
                            for name, cred in sorted(self.credentials.items())},
        }

    @classmethod
    def from_mapping(cls, mapping):
        """Build from a plain dict, ignoring keys that are not settings."""
        data = mapping or {}
        known = {name: cls.coerce(name, value)
                 for name, value in data.items() if name in MUTABLE_FIELDS}
        return cls(**known)


DEFAULTS = Settings()
