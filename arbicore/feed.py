"""Quote collection: one bulk call per venue, and stale prices thrown away.

The original feed asked for one ticker at a time:

    for name, client in self.clients.items():
        for sym in self.symbols:
            client.fetch_ticker(sym)

On a triangular scan that discovered 2,173 routes over 2,794 symbols, at the
rate limit every ccxt client applies, one pass took minutes against a two
second scan interval. The bot was therefore always trading on prices from the
previous scan, or from the one before that - which is the same defect as
having no slippage check at all, just further upstream.

Two fixes live here, both pure functions so they can be tested without a
network:

  * `merge_ticker_batch` reads a whole `fetch_tickers()` payload, so a venue
    costs one request instead of one per symbol.
  * it also drops quotes the exchange timestamped too long ago. A stale
    ticker shows a spread that has already closed, and acting on it is a
    guaranteed loss rather than a missed gain.

Ranking matters as much as speed: `rank_routes` keeps the routes whose legs
actually trade, because a cycle through an illiquid pair cannot be executed
at the price its ticker advertises no matter how good the arithmetic looks.
"""

import time

DEFAULT_MAX_QUOTE_AGE_MS = 10_000   # a price older than this is not tradeable
DEFAULT_MAX_ROUTES = 60             # routes kept per scan after ranking
MAX_CLOCK_SKEW_MS = 60_000          # beyond this, trust the local clock less


def _positive(value):
    """A float that is a usable price, or None.

    ccxt fills missing sides with None, and some venues report 0.0 for a book
    with no bids. Both must be rejected before they reach spread arithmetic,
    where a zero denominator or a zero ask looks like infinite profit.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0.0 else None


def quote_age_ms(timestamp, now_ms):
    """Age of an exchange timestamp in milliseconds, or None if unusable.

    A timestamp in the future means our clock and theirs disagree. Small
    disagreements are normal and treated as age zero; a large one means the
    number cannot be used to judge freshness at all, so it returns None and
    the caller keeps the quote rather than discarding every price on the venue.
    """
    if timestamp is None:
        return None
    try:
        stamp = float(timestamp)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    age = now_ms - stamp
    if age < 0:
        return 0.0 if -age <= MAX_CLOCK_SKEW_MS else None
    return age


def merge_ticker_batch(payload, symbols=None, now_ms=None,
                       max_age_ms=DEFAULT_MAX_QUOTE_AGE_MS):
    """Turn a `fetch_tickers` payload into {symbol: {"bid","ask"}}.

    Returns (quotes, rejected) where `rejected` maps a symbol to the reason it
    was dropped, so the dashboard can say "kucoin returned 40 stale prices"
    instead of silently scanning a shrinking universe.
    """
    wanted = set(symbols) if symbols else None
    now_ms = time.time() * 1000.0 if now_ms is None else float(now_ms)
    quotes = {}
    rejected = {}
    for symbol, ticker in (payload or {}).items():
        if wanted is not None and symbol not in wanted:
            continue
        if not isinstance(ticker, dict):
            rejected[symbol] = "malformed"
            continue
        bid = _positive(ticker.get("bid"))
        ask = _positive(ticker.get("ask"))
        if bid is None or ask is None:
            rejected[symbol] = "one-sided"
            continue
        if ask < bid:
            # Crossed quote: the venue is mid-update, or the two sides came
            # from different snapshots. Either way the spread is fiction.
            rejected[symbol] = "crossed"
            continue
        age = quote_age_ms(ticker.get("timestamp"), now_ms)
        if age is not None and max_age_ms and age > max_age_ms:
            rejected[symbol] = f"stale by {age / 1000.0:.1f}s"
            continue
        quotes[symbol] = {"bid": bid, "ask": ask,
                          "age_ms": age if age is not None else 0.0}
    return quotes, rejected


def route_volume(route, tickers):
    """The tradeable size of a route: its thinnest leg.

    A cycle is only as executable as its worst leg, so the minimum quote
    volume across the three symbols is the honest ranking key. Any leg with
    no volume data makes the whole route unrankable (0.0), which sorts it
    below every route we can actually measure.
    """
    volumes = []
    for symbol in route.get("symbols", ()):
        ticker = tickers.get(symbol) or {}
        value = ticker.get("quoteVolume")
        if value is None:
            base_volume = _positive(ticker.get("baseVolume"))
            last = _positive(ticker.get("last")) or _positive(ticker.get("close"))
            value = base_volume * last if base_volume and last else None
        volume = _positive(value)
        if volume is None:
            return 0.0
        volumes.append(volume)
    return min(volumes) if volumes else 0.0


def rank_routes(routes, tickers=None, limit=DEFAULT_MAX_ROUTES):
    """Keep the `limit` most liquid routes, most liquid first.

    Ties and missing volume data fall back to the discovery order, so the
    result is deterministic - a scan that reorders itself every pass makes
    the dashboard unreadable and bug reports unreproducible.
    """
    tickers = tickers or {}
    scored = [(-route_volume(route, tickers), index, route)
              for index, route in enumerate(routes)]
    scored.sort(key=lambda item: (item[0], item[1]))
    kept = [route for _score, _index, route in scored]
    return kept[:limit] if limit and limit > 0 else kept


def symbols_for_routes(routes):
    """Every symbol the given routes need, in first-seen order, deduplicated."""
    return list(dict.fromkeys(
        symbol for route in routes for symbol in route.get("symbols", ())))


def supports_bulk_tickers(client):
    """Whether this venue can price many symbols in one request."""
    has = getattr(client, "has", None)
    if isinstance(has, dict) and "fetchTickers" in has:
        return bool(has["fetchTickers"]) and callable(
            getattr(client, "fetch_tickers", None))
    return callable(getattr(client, "fetch_tickers", None))


def fetch_bulk_quotes(client, symbols, now_ms=None,
                      max_age_ms=DEFAULT_MAX_QUOTE_AGE_MS):
    """One request for the whole symbol list, with the two known refusals handled.

    Some venues reject an explicit symbol list (or silently ignore it and
    return everything); asking again with no argument and filtering locally
    costs one extra request in the worst case and is the only way to get a
    usable answer from them.
    """
    try:
        payload = client.fetch_tickers(list(symbols))
    except Exception:
        payload = client.fetch_tickers()
    return merge_ticker_batch(payload, symbols, now_ms=now_ms,
                              max_age_ms=max_age_ms)


def fetch_quotes_one_by_one(client, symbols, now_ms=None,
                            max_age_ms=DEFAULT_MAX_QUOTE_AGE_MS):
    """The slow path, for venues without fetchTickers.

    A symbol that is not listed on this venue raises; that is expected during
    a cross-exchange scan and is not an error worth surfacing, so it is
    recorded as a rejection and the scan continues.
    """
    payload = {}
    rejected = {}
    for symbol in symbols:
        try:
            payload[symbol] = client.fetch_ticker(symbol)
        except Exception as exc:
            rejected[symbol] = f"unavailable ({type(exc).__name__})"
    quotes, dropped = merge_ticker_batch(payload, symbols, now_ms=now_ms,
                                         max_age_ms=max_age_ms)
    rejected.update(dropped)
    return quotes, rejected
