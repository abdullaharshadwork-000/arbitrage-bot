#!/usr/bin/env python3
"""Apply all ArbiCore server patches idempotently.

Fixes:
  * Strategy Lab routes (/lab, /api/lab, /agent_lab.js) even if blueprint fails
  * theme-glass.css route
  * wire_agent_session in scan loop
  * session_wire import

Run: python scripts/apply_server_patches.py
Then restart the server.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def main() -> int:
    text = SERVER.read_text(encoding="utf-8")
    original = text
    notes: list[str] = []

    # --- import session_wire ---
    if "from arbicore.session_wire import wire_agent_session" not in text:
        for anchor in (
            "from arbicore.server_live_hook import maybe_process_handoff, maybe_register_from_engine\n",
            "from arbicore.scan_hook import notify_agent_mid\n",
        ):
            if anchor in text:
                text = text.replace(
                    anchor,
                    anchor + "from arbicore.session_wire import wire_agent_session\n",
                    1,
                )
                notes.append("added session_wire import")
                break
        else:
            notes.append("WARN: could not add session_wire import")

    # --- wire_agent_session before notify_agent_mid ---
    if "wire_agent_session(" not in text:
        markers = [
            "        # Optional agent observation (no-op unless ARBICORE_AGENT_LOOP=1; never raises)\n",
            "            for _sym, _mid in (mid_prices or {}).items():\n",
        ]
        block = '''        # Wire agent + optional live path (never raises)
        try:
            wire_agent_session(
                engine=locals().get("engine") or locals().get("real_engine"),
                risk_manager=locals().get("risk_manager"),
                equity=float((cfg or {}).get("trade_size") or 10000),
                mode=str((cfg or {}).get("mode") or getattr(bot, "MODE", "tutorial")),
                execution_mode=str((cfg or {}).get("execution_mode") or getattr(bot, "EXECUTION_MODE", "paper")),
                real_trading_ack=str((cfg or {}).get("real_trading_ack") or ""),
            )
        except Exception:
            pass

'''
        inserted = False
        for m in markers:
            if m in text:
                text = text.replace(m, block + m, 1)
                notes.append("inserted wire_agent_session")
                inserted = True
                break
        if not inserted:
            notes.append("WARN: wire_agent_session insert point not found")

    # --- theme-glass.css ---
    if '@app.route("/theme-glass.css")' not in text:
        anchor = '@app.route("/arbitrage-bot-terminal.html")'
        if anchor in text:
            route = '''@app.route("/theme-glass.css")
def theme_glass_stylesheet():
    return send_from_directory(Path(__file__).resolve().parent / "static", "theme-glass.css", mimetype="text/css")


'''
            text = text.replace(anchor, route + anchor, 1)
            notes.append("added theme-glass.css route")
        else:
            notes.append("WARN: theme-glass insert point not found")

    # --- Lab fallback routes (always serve Lab even if blueprint dies) ---
    if "def arbicore_api_lab_fallback" not in text:
        anchor = '@app.route("/api/auth/login"'
        if anchor in text:
            extra = '''
@app.route("/lab")
def arbicore_lab_page_fallback():
    """Strategy Lab – always available."""
    lab = Path(__file__).resolve().parent / "static" / "agent_lab.html"
    if lab.is_file():
        return Response(lab.read_text(encoding="utf-8"), mimetype="text/html")
    return Response("<h1>Lab</h1><p>static/agent_lab.html missing</p>", mimetype="text/html")


@app.route("/agent_lab.js")
def arbicore_lab_js_fallback():
    return send_from_directory(
        Path(__file__).resolve().parent / "static", "agent_lab.js",
        mimetype="application/javascript",
    )


@app.route("/api/lab", methods=["GET"])
def arbicore_api_lab_fallback():
    try:
        from arbicore.lab_api import build_lab_snapshot
        return jsonify(build_lab_snapshot()), 200
    except Exception as exc:
        try:
            from arbicore.agent_api import build_lab_payload
            return jsonify(build_lab_payload()), 200
        except Exception as exc2:
            return jsonify({"ok": False, "error": str(exc), "error2": str(exc2)}), 200


'''
            text = text.replace(anchor, extra + anchor, 1)
            notes.append("added Lab fallback routes /lab /api/lab /agent_lab.js")
        else:
            notes.append("WARN: Lab fallback insert point not found")

    if text != original:
        SERVER.write_text(text, encoding="utf-8")
        notes.append("server.py written")
    else:
        notes.append("server.py already up to date")

    for n in notes:
        print(n)

    # verify static files exist
    for rel in ("static/agent_lab.html", "static/agent_lab.js", "static/theme-glass.css"):
        ok = (ROOT / rel).is_file()
        print(f"{rel}: {'OK' if ok else 'MISSING'}")

    return 0 if all("WARN" not in n for n in notes) or "written" in " ".join(notes) or "up to date" in " ".join(notes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
