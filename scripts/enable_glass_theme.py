#!/usr/bin/env python3
"""Link static/theme-glass.css into the dashboard and serve it."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "dashboard-pro.html"
SERVER = ROOT / "server.py"


def main() -> int:
    changed = False

    html = HTML.read_text(encoding="utf-8")
    if "theme-glass.css" not in html:
        needle = '<link rel="stylesheet" href="/dashboard-pro.css" />'
        if needle not in html:
            print("dashboard-pro.html: css link not found")
            return 1
        html = html.replace(
            needle,
            needle + '\n    <link rel="stylesheet" href="/theme-glass.css" />',
            1,
        )
        HTML.write_text(html, encoding="utf-8")
        changed = True
        print("linked theme-glass.css in dashboard-pro.html")
    else:
        print("html already links theme-glass.css")

    srv = SERVER.read_text(encoding="utf-8")
    if '"/theme-glass.css"' not in srv:
        anchor = (
            '@app.route("/dashboard-pro.css")\n'
            "def dashboard_pro_css():\n"
            '    return send_from_directory(".", "dashboard-pro.css", '
            'mimetype="text/css")\n'
        )
        # tolerant match: find the route block
        if '@app.route("/dashboard-pro.css")' not in srv:
            print("server.py: dashboard-pro.css route not found")
            return 1
        insert = (
            '\n\n@app.route("/theme-glass.css")\n'
            "def theme_glass_css():\n"
            '    return send_from_directory("static", "theme-glass.css", '
            'mimetype="text/css")\n'
        )
        # insert after the css route function line
        lines = srv.splitlines(keepends=True)
        out = []
        i = 0
        while i < len(lines):
            out.append(lines[i])
            if '@app.route("/dashboard-pro.css")' in lines[i]:
                # copy next 2-3 lines of function
                j = i + 1
                while j < len(lines) and (
                    lines[j].startswith(" ") or lines[j].startswith("\t") or lines[j].strip() == ""
                ):
                    out.append(lines[j])
                    if "send_from_directory" in lines[j] and "dashboard-pro.css" in lines[j]:
                        out.append(insert)
                        changed = True
                        j += 1
                        break
                    j += 1
                i = j
                continue
            i += 1
        if changed:
            SERVER.write_text("".join(out), encoding="utf-8")
            print("added /theme-glass.css route to server.py")
        else:
            print("could not insert route; add manually")
            return 1
    else:
        print("server already serves theme-glass.css")

    print("done" if changed else "no changes needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
