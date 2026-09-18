#!/usr/bin/env python3
"""Restore dashboard-pro.html / dashboard-pro.js and add Strategy Lab + agent status.

Run from repo root:
  python scripts/restore_dashboard_agent_ui.py
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "2d0f49d1"  # last known-good dashboard before agent UI patch
BASE = f"https://raw.githubusercontent.com/abdullaharshadwork-000/arbitrage-bot/{COMMIT}"


def fetch(name: str) -> str:
    url = f"{BASE}/{name}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8")


def patch_html(html: str) -> str:
    old_sidebar = (
        '          <a class="sidebar-link" data-page="history" role="button" tabindex="0">\n'
        '            <i class="fas fa-history"></i> Trade History\n'
        "          </a>\n"
        "        </div>"
    )
    new_sidebar = (
        '          <a class="sidebar-link" data-page="history" role="button" tabindex="0">\n'
        '            <i class="fas fa-history"></i> Trade History\n'
        "          </a>\n"
        '          <a class="sidebar-link" href="/lab" target="_blank" rel="noopener" '
        'role="link" tabindex="0" title="Opens Strategy Lab in a new tab">\n'
        '            <i class="fas fa-robot"></i> Strategy Lab\n'
        "          </a>\n"
        "        </div>"
    )
    if old_sidebar not in html:
        raise SystemExit("sidebar anchor not found in downloaded HTML")
    html = html.replace(old_sidebar, new_sidebar, 1)

    old_intel = (
        '                  <div class="flex justify-between">\n'
        '                    <span class="text-muted">Strategy evidence:</span>\n'
        '                    <span id="statusStrategyEvidence">Collecting paper results</span>\n'
        "                  </div>\n"
        '                  <div class="text-muted" id="intelligenceExplanation" '
        'style="font-size:.78rem;line-height:1.45;margin-top:.5rem">\n'
        "                    The model estimates whether an observed arbitrage edge can "
        "survive volatility and execution delay. It does not guarantee profit.\n"
        "                  </div>"
    )
    new_intel = (
        '                  <div class="flex justify-between">\n'
        '                    <span class="text-muted">Strategy evidence:</span>\n'
        '                    <span id="statusStrategyEvidence">Collecting paper results</span>\n'
        "                  </div>\n"
        '                  <div class="flex justify-between">\n'
        '                    <span class="text-muted">Agent loop:</span>\n'
        '                    <span id="statusAgentLoop">Off</span>\n'
        "                  </div>\n"
        '                  <div class="flex justify-between">\n'
        '                    <span class="text-muted">Agent cycles:</span>\n'
        '                    <span id="statusAgentCycles">0</span>\n'
        "                  </div>\n"
        '                  <div class="flex justify-between">\n'
        '                    <span class="text-muted">Agent memory:</span>\n'
        '                    <span id="statusAgentMemory">0</span>\n'
        "                  </div>\n"
        '                  <div class="flex justify-between">\n'
        '                    <span class="text-muted">Agent paper fills:</span>\n'
        '                    <span id="statusAgentPaper">0</span>\n'
        "                  </div>\n"
        '                  <div class="text-muted" id="intelligenceExplanation" '
        'style="font-size:.78rem;line-height:1.45;margin-top:.5rem">\n'
        "                    The model estimates whether an observed arbitrage edge can "
        "survive volatility and execution delay. It does not guarantee profit.\n"
        '                    <br><a href="/lab" target="_blank" rel="noopener" '
        'style="color:var(--primary-light)">Open Strategy Lab</a> for drift, canary, '
        "scorecard, and handoff queue.\n"
        "                  </div>"
    )
    if old_intel not in html:
        raise SystemExit("intelligence anchor not found in downloaded HTML")
    return html.replace(old_intel, new_intel, 1)


def patch_js(js: str) -> str:
    if "refreshAgentStatus" not in js:
        agent_fn = '''
        async function refreshAgentStatus() {
          try {
            const agent = await api("/api/agent", { cache: "no-store", signal: AbortSignal.timeout(8000) });
            const enabled = !!(agent && agent.agent_loop_enabled);
            const st = (agent && agent.state) || {};
            const mem = st.memory || {};
            if ($("statusAgentLoop")) {
              $("statusAgentLoop").textContent = enabled ? "On" : "Off";
              $("statusAgentLoop").className = enabled ? "text-success" : "text-muted";
            }
            if ($("statusAgentCycles")) $("statusAgentCycles").textContent = String(st.cycles ?? 0);
            if ($("statusAgentMemory")) $("statusAgentMemory").textContent = String(mem.experience_count ?? 0);
            if ($("statusAgentPaper")) $("statusAgentPaper").textContent = String(st.paper_fills ?? 0);
          } catch (error) {
            if ($("statusAgentLoop")) {
              $("statusAgentLoop").textContent = "Unavailable";
              $("statusAgentLoop").className = "text-muted";
            }
          }
        }
'''
        idx = js.find("async function refreshOperationalInsights")
        if idx < 0:
            raise SystemExit("refreshOperationalInsights not found in JS")
        js = js[:idx] + agent_fn + "\n        " + js[idx:]

    old = (
        'await Promise.all([refreshUserStats(), refreshCredentialStatus(), '
        'refreshAccountSecurity(), refreshOperationalInsights(), '
        'currentUser.role === "admin" ? refreshAdmin() : Promise.resolve()]);'
    )
    new = (
        'await Promise.all([refreshUserStats(), refreshCredentialStatus(), '
        'refreshAccountSecurity(), refreshOperationalInsights(), refreshAgentStatus(), '
        'currentUser.role === "admin" ? refreshAdmin() : Promise.resolve()]);'
    )
    if old in js:
        js = js.replace(old, new, 1)

    old2 = (
        "async function refreshOperationalInsights() {\n"
        "        try {\n"
        "          const [qualityResult, inventoryResult, intelligenceResult] = await Promise.all(["
    )
    new2 = (
        "async function refreshOperationalInsights() {\n"
        "        try {\n"
        "          refreshAgentStatus();\n"
        "          const [qualityResult, inventoryResult, intelligenceResult] = await Promise.all(["
    )
    if old2 in js and "refreshAgentStatus();\n          const [qualityResult" not in js:
        js = js.replace(old2, new2, 1)
    return js


def main() -> int:
    html = patch_html(fetch("dashboard-pro.html"))
    js = patch_js(fetch("dashboard-pro.js"))
    (ROOT / "dashboard-pro.html").write_text(html, encoding="utf-8")
    (ROOT / "dashboard-pro.js").write_text(js, encoding="utf-8")
    print("Restored and patched dashboard-pro.html + dashboard-pro.js")
    print("Hard-refresh the browser (Ctrl+F5). Strategy Lab is in the sidebar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
