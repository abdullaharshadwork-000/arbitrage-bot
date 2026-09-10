"""Run UI regression tests with a fresh disposable DB and NO exchange access.

Usage: python tests/run_browser_smoke.py
Does not load live_config.py, operator credentials, or the operator's database.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def serve_test_app():
    sys.path.insert(0, str(ROOT))
    import ccxt
    from werkzeug.serving import make_server
    import server

    def no_exchange_network(*args, **kwargs):
        raise RuntimeError("Exchange network disabled in offline browser tests")

    ccxt.Exchange.fetch = no_exchange_network
    server.bot.LiveFeed = no_exchange_network
    server.initialize_database()
    server.state["running"] = False
    httpd = make_server("127.0.0.1", 0, server.app, threaded=True)
    print(f"TEST_URL=http://127.0.0.1:{httpd.server_port}", flush=True)
    httpd.serve_forever()


def main():
    with tempfile.TemporaryDirectory(prefix="arbicore-browser-") as temporary:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ARBI_", "ARBICORE_", "BINANCE_", "BYBIT_", "OKX_", "KUCOIN_"))}
        env.update(ARBICORE_DB=str(Path(temporary) / "test.db"),
                   ARBICORE_ADMIN_USERNAME="admin", ARBICORE_ADMIN_PASSWORD="Admin@12345")
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve"],
                                   cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        try:
            for line in process.stdout:
                if line.startswith("TEST_URL="):
                    env["ARBICORE_URL"] = line.strip().split("=", 1)[1]
                    break
            else:
                raise RuntimeError("Isolated test server failed to start")
            # Generated screenshots are QA artifacts, never operator screenshots.
            env["ARBICORE_SCREENSHOT_DIR"] = str(ROOT / "test-results")
            result = subprocess.run(["node", "tests/browser_smoke.js"], cwd=ROOT, env=env, timeout=180)
            return result.returncode
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve_test_app()
    else:
        raise SystemExit(main())
