"""Outbound notifications, because a halt nobody sees is a halt that waits.

Deliberately built on `urllib.request` rather than `requests`: this has to work
from a fresh checkout with only ccxt and Flask installed, and an alerting path
that fails because of a missing dependency is worse than no alerting at all.

Three properties matter more than features here:

  * Sending never blocks the trading loop. A webhook that hangs for 30 seconds
    must not delay a scan or, worse, a halt.
  * Sending never raises into the caller. The bot halting is the important
    event; failing to announce it must not become a second failure.
  * The same alert does not fire hundreds of times. A halted loop can evaluate
    its halt condition every interval, so identical messages are suppressed for
    a cool-off window.

Nothing here formats a credential. Alert bodies are built from risk state and
trade results, and the webhook URL itself is never echoed back.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"

SEVERITY_ORDER = {INFO: 10, WARNING: 20, CRITICAL: 30}

# ASCII only. The Windows console defaults to cp1252, which cannot encode the
# emoji every alerting example uses; a badge that raises UnicodeEncodeError on
# the way to telling you the bot halted is worse than no badge.
BADGES = {INFO: "[info]", WARNING: "[WARN]", CRITICAL: "[HALT]"}

DEFAULT_TIMEOUT = 8.0
DEFAULT_COOLOFF = 300.0   # seconds an identical message stays suppressed


def _payload_for(url, title, body, severity):
    """Shape the JSON body for whichever service the URL points at."""
    text = f"{BADGES.get(severity, '')} {title}\n{body}".strip()
    if "hooks.slack.com" in url:
        return {"text": text}
    if "discord" in url:
        # Discord rejects messages over 2000 characters outright.
        return {"content": text[:1900]}
    if "api.telegram.org" in url:
        return {"text": text[:4000], "disable_web_page_preview": True}
    return {"title": title, "body": body, "severity": severity, "text": text}


def redact_url(url):
    """A loggable form of a webhook URL: host and path shape only.

    Telegram puts the bot token in the path and Slack/Discord put a secret in
    it too, so the URL is itself a credential and must never be printed whole.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        segments = [seg for seg in parts.path.split("/") if seg]
        shape = "/".join("*" * min(len(seg), 6) for seg in segments)
        return f"{parts.scheme}://{parts.netloc}/{shape}"
    except Exception:
        return "<webhook>"


class Notifier:
    """Fire-and-forget alerts with de-duplication and a console fallback.

    `sender` exists for tests; the default posts JSON. `console` receives every
    alert regardless of whether a webhook is configured, so a bot run from a
    terminal with no webhook still tells you why it stopped.
    """

    def __init__(self, webhook_url="", min_severity=WARNING,
                 cooloff=DEFAULT_COOLOFF, timeout=DEFAULT_TIMEOUT,
                 sender=None, console=print, clock=time.time, async_send=True):
        self.webhook_url = str(webhook_url or "")
        self.min_severity = min_severity
        self.cooloff = float(cooloff)
        self.timeout = float(timeout)
        self._sender = sender
        self._console = console
        self._clock = clock
        self._async = bool(async_send)
        self._recent = OrderedDict()   # fingerprint -> last sent time
        self._lock = threading.Lock()
        self.sent = 0
        self.suppressed = 0
        self.failed = 0
        self.last_error = ""
        self.log = []                  # every alert, for /api/state

    @property
    def configured(self):
        return bool(self.webhook_url) or self._sender is not None

    def _should_send(self, fingerprint, severity):
        if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(self.min_severity, 0):
            return False
        now = self._clock()
        with self._lock:
            last = self._recent.get(fingerprint)
            if last is not None and now - last < self.cooloff:
                self.suppressed += 1
                return False
            self._recent[fingerprint] = now
            while len(self._recent) > 256:
                self._recent.popitem(last=False)
        return True

    def _post(self, title, body, severity):
        """Runs on a worker thread. Must not raise."""
        try:
            if self._sender is not None:
                self._sender(title, body, severity)
            else:
                payload = _payload_for(self.webhook_url, title, body, severity)
                request = urllib.request.Request(
                    self.webhook_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "User-Agent": "arbicore/1.0"},
                    method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout):
                    pass
            with self._lock:
                self.sent += 1
        except Exception as exc:       # network, DNS, 4xx, anything
            with self._lock:
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"

    def send(self, title, body="", severity=WARNING, fingerprint=None):
        """Queue one alert. Always returns immediately; never raises."""
        entry = {"at": self._clock(), "severity": severity,
                 "title": str(title), "body": str(body)}
        with self._lock:
            self.log.append(entry)
            del self.log[:-100]

        if self._console is not None:
            badge = BADGES.get(severity, "[----]")
            line = f"{badge} {title}"
            if body:
                line += f" - {body}"
            try:
                self._console(line)
            except Exception:
                pass

        if not self.configured:
            return False
        if not self._should_send(fingerprint or f"{severity}:{title}", severity):
            return False

        if self._async:
            thread = threading.Thread(
                target=self._post, args=(title, body, severity),
                name="arbicore-alert", daemon=True)
            thread.start()
        else:
            self._post(title, body, severity)
        return True

    # ------------------------------------------------- the events worth waking
    # for. Each has a stable fingerprint so a repeating condition alerts once
    # per cool-off window rather than once per scan.

    def halted(self, limit, reason, snapshot=None):
        detail = reason
        if snapshot:
            detail += (f" | today {snapshot.get('realized_today', 0):+.2f} USDT "
                       f"over {snapshot.get('trades_today', 0)} trades")
        return self.send("Trading halted", detail, CRITICAL,
                         fingerprint=f"halt:{limit}")

    def resumed(self, note="operator resumed the loop"):
        return self.send("Trading resumed", note, WARNING, fingerprint="resume")

    def stranded(self, exchange, currency, quantity, detail=""):
        return self.send(
            "Stranded position needs a human",
            f"{float(quantity)} {currency} on {exchange}. {detail}".strip(),
            CRITICAL, fingerprint=f"stranded:{exchange}:{currency}")

    def unreconciled(self, error):
        """An order whose fill could never be confirmed — the worst state."""
        return self.send(
            "Unconfirmed fill - position may be open",
            f"{getattr(error, 'exchange', '?')} {getattr(error, 'symbol', '?')} "
            f"order {getattr(error, 'order_id', None) or 'unknown'} / client "
            f"{getattr(error, 'client_order_id', None) or 'unknown'}: {error}",
            CRITICAL,
            fingerprint=f"unreconciled:{getattr(error, 'client_order_id', '')}")

    def losing_trade(self, trade):
        return self.send(
            f"Trade lost money on {getattr(trade, 'symbol', '?')}",
            f"expected {float(getattr(trade, 'expected_profit', 0)):+.4f}, "
            f"realized {float(getattr(trade, 'realized_profit', 0)):+.4f} USDT",
            WARNING, fingerprint=f"loss:{getattr(trade, 'symbol', '')}")

    def armed(self, settings):
        """Announce that real money is now in play. Always worth an alert."""
        return self.send(
            "REAL trading armed",
            f"{settings.strategy} on {', '.join(settings.exchanges)} at "
            f"{float(settings.trade_size_usdt):.2f} USDT per trade, daily loss "
            f"limit {float(settings.max_daily_loss_usdt):.2f} USDT",
            CRITICAL, fingerprint="armed")

    def snapshot(self):
        with self._lock:
            return {
                "configured": self.configured,
                "webhook": redact_url(self.webhook_url),
                "min_severity": self.min_severity,
                "sent": self.sent,
                "suppressed": self.suppressed,
                "failed": self.failed,
                "last_error": self.last_error,
                "recent": list(self.log)[-10:],
            }
