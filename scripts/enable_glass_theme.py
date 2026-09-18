#!/usr/bin/env python3
"""Apply violet glass theme reliably.

1) Bakes static/theme-glass.css into the end of dashboard-pro.css
2) Links /theme-glass.css in dashboard-pro.html (optional extra)
3) Adds Flask route for /theme-glass.css (correct def name)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "dashboard-pro.html"
CSS = ROOT / "dashboard-pro.css"
THEME = ROOT / "static" / "theme-glass.css"
SERVER = ROOT / "server.py"
MARKER = "/* === BAKED VIOLET GLASS THEME === */"


def bake_css() -> bool:
    if not THEME.is_file():
        print("missing static/theme-glass.css – pull latest")
        return False
    base = CSS.read_text(encoding="utf-8") if CSS.is_file() else ""
    theme = THEME.read_text(encoding="utf-8")
    if MARKER in base:
        # refresh baked section
        head = base.split(MARKER)[0].rstrip()
        CSS.write_text(head + "\n\n" + MARKER + "\n" + theme + "\n", encoding="utf-8")
        print("refreshed baked theme in dashboard-pro.css")
        return True
    CSS.write_text(base + "\n\n" + MARKER + "\n" + theme + "\n", encoding="utf-8")
    print("baked theme into dashboard-pro.css")
    return True


def link_html() -> bool:
    html = HTML.read_text(encoding="utf-8")
    if "theme-glass.css" in html:
        print("html already links theme-glass.css")
        return False
    needle = '<link rel="stylesheet" href="/dashboard-pro.css" />'
    if needle not in html:
        print("dashboard-pro.html: css link not found")
        return False
    HTML.write_text(
        html.replace(
            needle,
            needle + '\n    <link rel="stylesheet" href="/theme-glass.css" />',
            1,
        ),
        encoding="utf-8",
    )
    print("linked theme-glass.css in dashboard-pro.html")
    return True


def patch_server() -> bool:
    srv = SERVER.read_text(encoding="utf-8")
    if '"/theme-glass.css"' in srv:
        print("server already serves theme-glass.css")
        return False
    # Match actual function name in this repo
    candidates = [
        (
            '@app.route("/dashboard-pro.css")\n'
            "def dashboard_stylesheet():\n"
            '    return send_from_directory(".", "dashboard-pro.css", mimetype="text/css")\n'
        ),
        (
            '@app.route("/dashboard-pro.css")\n'
            "def dashboard_pro_css():\n"
            '    return send_from_directory(".", "dashboard-pro.css", mimetype="text/css")\n'
        ),
    ]
    inject_tail = (
        "\n\n@app.route(\"/theme-glass.css\")\n"
        "def theme_glass_css():\n"
        '    return send_from_directory("static", "theme-glass.css", mimetype="text/css")\n'
    )
    for block in candidates:
        if block in srv:
            SERVER.write_text(srv.replace(block, block + inject_tail, 1), encoding="utf-8")
            print("added /theme-glass.css route to server.py")
            return True
    print("WARNING: could not auto-patch server.py – theme still works via baked CSS")
    return False


def main() -> int:
    if not bake_css():
        return 1
    link_html()
    patch_server()
    print("Restart server.py and hard-refresh the browser (Ctrl+F5).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
