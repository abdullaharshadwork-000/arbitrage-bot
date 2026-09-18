/* ArbiCore Strategy Lab – external JS (CSP script-src 'self') */
(function () {
  function row(k, v, cls) {
    return (
      '<div class="row"><span>' +
      k +
      '</span><span class="val ' +
      (cls || "") +
      '">' +
      v +
      "</span></div>"
    );
  }

  function showErr(msg) {
    var el = document.getElementById("errBox");
    if (!el) return;
    el.style.display = msg ? "block" : "none";
    el.textContent = msg || "";
  }

  function setBox(id, html) {
    var el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  async function load() {
    showErr("");
    try {
      var ctrl = new AbortController();
      var t = setTimeout(function () {
        ctrl.abort();
      }, 12000);
      var res = await fetch("/api/lab", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: ctrl.signal,
      });
      clearTimeout(t);
      var text = await res.text();
      var data;
      try {
        data = JSON.parse(text);
      } catch (_) {
        showErr(
          "API returned non-JSON (HTTP " +
            res.status +
            "). Is /api/lab registered? Restart after git pull + fix_project."
        );
        setBox("raw", text.slice(0, 2000));
        return;
      }
      setBox("raw", JSON.stringify(data, null, 2));
      if (!res.ok) {
        showErr(
          "HTTP " +
            res.status +
            ": " +
            (data.error || data.message || "request failed")
        );
        return;
      }
      var agent = data.agent || {};
      var st = agent.state || {};
      var loopPill = document.getElementById("loopPill");
      var paperPill = document.getElementById("paperPill");
      if (loopPill) {
        loopPill.textContent =
          "agent loop: " + (agent.agent_loop_enabled ? "ON" : "OFF");
        loopPill.className =
          "pill" + (agent.agent_loop_enabled ? " on" : "");
      }
      if (paperPill) {
        paperPill.textContent =
          "paper: " + (agent.paper_exec_enabled ? "ON" : "OFF");
        paperPill.className =
          "pill" + (agent.paper_exec_enabled ? " on" : "");
      }
      setBox(
        "agentBox",
        row("cycles", st.cycles != null ? st.cycles : "—") +
          row("paper fills", st.paper_fills != null ? st.paper_fills : "—") +
          row("reflections", st.reflections != null ? st.reflections : "—") +
          row("last action", st.last_action || "—") +
          row("last critique", st.last_critique || "—")
      );
      var sc = (data.scorecard && data.scorecard.scorecard) || {};
      setBox(
        "scoreBox",
        row("samples", sc.total_experiences != null ? sc.total_experiences : 0) +
          row("early WR", sc.early_win_rate != null ? sc.early_win_rate : "—") +
          row("late WR", sc.late_win_rate != null ? sc.late_win_rate : "—") +
          row(
            "improving",
            sc.improving ? "yes" : "no",
            sc.improving ? "ok" : "warn"
          ) +
          row("reason", sc.reason || "—")
      );
      var strats = (data.strategies && data.strategies.strategies) || [];
      setBox(
        "stratBox",
        strats.length
          ? strats
              .map(function (s) {
                return row(
                  (s.name || s.id) + " v" + s.version,
                  s.status
                );
              })
              .join("")
          : row("count", "0")
      );
      var cans = (data.canary && data.canary.deployments) || [];
      setBox(
        "canaryBox",
        cans.length
          ? cans
              .map(function (c) {
                return row(
                  c.strategy_id + " s" + c.stage,
                  c.status +
                    " @ " +
                    ((c.allocation_fraction || 0) * 100).toFixed(0) +
                    "%"
                );
              })
              .join("")
          : row("deployments", "0")
      );
      var reports = (data.drift && data.drift.reports) || [];
      setBox(
        "driftBox",
        reports.length
          ? reports
              .map(function (r) {
                return row(
                  r.strategy_id,
                  r.severity + (r.drifted ? " DRIFT" : ""),
                  r.drifted ? "bad" : "ok"
                );
              })
              .join("")
          : row("reports", "0")
      );
      var hand = data.handoff || {};
      setBox(
        "handBox",
        row("enabled", hand.enabled ? "yes" : "no", hand.enabled ? "ok" : "") +
          row("queued", hand.queued != null ? hand.queued : 0)
      );
      var live = data.live || {};
      setBox(
        "liveBox",
        row(
          "LIVE_EXEC flag",
          live.live_exec_enabled ? "ON" : "OFF",
          live.live_exec_enabled ? "ok" : "warn"
        ) +
          row(
            "wire registered",
            live.wire_registered ? "yes" : "no",
            live.wire_registered ? "ok" : "warn"
          ) +
          row(
            "risk context",
            live.risk_context ? "yes" : "no",
            live.risk_context ? "ok" : "warn"
          ) +
          row(
            "canary",
            live.canary_fraction != null
              ? (live.canary_fraction * 100).toFixed(1) + "%"
              : "—"
          ) +
          row(
            "recent fills",
            Array.isArray(live.recent) ? live.recent.length : 0
          )
      );
      var pos = live.positions || {};
      setBox(
        "posBox",
        row("open", pos.open_count != null ? pos.open_count : 0) +
          row("closed", pos.closed_count != null ? pos.closed_count : 0)
      );
    } catch (e) {
      var msg =
        e.name === "AbortError"
          ? "Request timed out. Is the server running on this host/port?"
          : String(e);
      showErr(msg);
      setBox("raw", msg);
      [
        "agentBox",
        "scoreBox",
        "stratBox",
        "canaryBox",
        "driftBox",
        "handBox",
        "liveBox",
        "posBox",
      ].forEach(function (id) {
        setBox(id, row("error", "failed", "bad"));
      });
    }
  }

  function boot() {
    var btn = document.getElementById("refresh");
    if (btn) btn.addEventListener("click", load);
    load();
    setInterval(load, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
