"""Read-only, venue-specific demo access diagnostics. Never submits orders."""
import argparse
import json
import os

from .exchange_signals import CAPABILITIES, DemoDiagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", required=True, choices=CAPABILITIES)
    args = parser.parse_args(argv)
    prefix = args.exchange.upper() + ("_DEMO" if args.exchange == "okx" else "_TESTNET")
    try:
        diagnostic = DemoDiagnostics(args.exchange, os.environ.get(prefix + "_API_KEY", ""),
            os.environ.get(prefix + "_API_SECRET", ""), os.environ.get(prefix + "_API_PASSPHRASE", ""))
        print(json.dumps(diagnostic.check(), indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "exchange": args.exchange,
                          "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                          "orders_submitted": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
