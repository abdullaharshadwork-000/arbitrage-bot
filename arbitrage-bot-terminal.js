let ALL_SYMBOLS = [];
      let ALL_EXCHANGES = [];
      let activeExchanges = new Set(ALL_EXCHANGES);
      let activeSymbols = new Set(ALL_SYMBOLS);
      let running = false;
      let latestRecovery = null;
      let chipUpdatePending = false;

      const $ = (id) => document.getElementById(id);

      function fmtPrice(v) {
        v = Number(v);
        if (!v) return "—";
        const dp = v >= 100 ? 2 : v >= 1 ? 4 : 6;
        return v.toLocaleString(undefined, {
          minimumFractionDigits: dp,
          maximumFractionDigits: dp,
        });
      }

      function fmtTime(iso) {
        try {
          return iso.slice(11, 19);
        } catch (e) {
          return iso;
        }
      }

      async function api(path, opts) {
        const res = await fetch(path, opts);
        if (!res.ok) {
          let detail = "HTTP " + res.status;
          try {
            const body = await res.json();
            detail = body.error || body.message || detail;
          } catch (e) {
            // Keep the HTTP status when the server has no JSON error body.
          }
          throw new Error(detail);
        }
        return res.json();
      }

      // How long a message the operator caused stays on screen. The state poll
      // runs every second and writes the same notice bar, so without a hold a
      // refusal lived for one frame: the setting snapped back to its old value
      // with nothing to say why, which looks exactly like a setting that was
      // accepted and then ignored.
      const NOTICE_HOLD_MS = 8000;
      let noticeHoldUntil = 0;

      function showNotice(message, { hold = false } = {}) {
        const notice = $("stateNotice");
        if (!hold && noticeHoldUntil > Date.now()) return;
        noticeHoldUntil = hold && message ? Date.now() + NOTICE_HOLD_MS : 0;
        notice.textContent = message || "";
        notice.classList.toggle("visible", Boolean(message));
      }

      function clearNotice() {
        // An action that worked answers the previous complaint about it.
        noticeHoldUntil = 0;
        showNotice("");
      }

      async function runAction(action) {
        try {
          await action();
          clearNotice();
        } catch (e) {
          showNotice(e.message || "Request failed.", { hold: true });
        }
      }

      async function testConnection() {
        try {
          const result = await api("/api/test-connection", { method: "POST" });
          const failed = (result.exchanges || []).filter((item) => !item.ok);
          showNotice(
            failed.length
              ? failed
                  .map((item) => `${item.exchange}: ${item.error || "failed"}`)
                  .join(" | ")
              : (result.exchanges || [])
                  .map(
                    (item) =>
                      `${item.exchange}: ${item.latency_ms}ms, fee ${item.fee ?? "unknown"}`,
                  )
                  .join(" | "),
            { hold: true },
          );
        } catch (e) {
          showNotice(e.message || "Connection test failed.", { hold: true });
        }
      }

      async function applyStructuralConfig(body) {
        const wasRunning = running;
        if (wasRunning) await api("/api/pause", { method: "POST" });
        await api("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        await api("/api/reset", { method: "POST" });
        if (wasRunning) await api("/api/start", { method: "POST" });
      }

      function renderMatrix(quotes) {
        const body = $("matrixBody");
        if (!quotes) {
          body.innerHTML = "";
          return;
        }

        body.innerHTML = ALL_SYMBOLS.map((sym) => {
          const pairQuotes = quotes[sym] || {};
          const cheapestAskExchange = Object.keys(pairQuotes).reduce(
            (best, ex) => {
              if (!best) return ex;
              return pairQuotes[ex].ask < pairQuotes[best].ask ? ex : best;
            },
            null,
          );
          const highestBidExchange = Object.keys(pairQuotes).reduce(
            (best, ex) => {
              if (!best) return ex;
              return pairQuotes[ex].bid > pairQuotes[best].bid ? ex : best;
            },
            null,
          );

          const buyPrice = cheapestAskExchange
            ? pairQuotes[cheapestAskExchange].ask
            : null;
          const sellPrice = highestBidExchange
            ? pairQuotes[highestBidExchange].bid
            : null;
          let gap = "—";
          if (buyPrice && sellPrice) {
            const diff = ((sellPrice - buyPrice) / buyPrice) * 100;
            gap = `${diff >= 0 ? "+" : ""}${diff.toFixed(2)}%`;
          }

          return `
            <tr>
              <td style="font-weight:700; color:var(--text);">${sym}</td>
              <td>${buyPrice ? "$" + fmtPrice(buyPrice) : "—"}</td>
              <td>${sellPrice ? "$" + fmtPrice(sellPrice) : "—"}</td>
              <td class="${gap.startsWith("+") ? "c-green" : "c-red"}">${gap}</td>
            </tr>
          `;
        }).join("");
      }

      function renderBlotter(recent) {
        const el = $("blotter");
        if (!recent || !recent.length) {
          el.innerHTML =
            '<div style="padding: 22px 18px; color: var(--muted); font-size: 12px;">Press Start Engine to begin scanning for cross-exchange gaps.</div>';
          return;
        }

        el.innerHTML = recent
          .map((e, i) => {
            if (e.type === "miss") {
              return `
              <div class="blot-row miss">
                <div class="blot-time">${fmtTime(e.time)}</div>
                <div class="blot-msg">scan #${e.scan_num} — no profitable gap</div>
                <div></div>
              </div>
            `;
            }
            if (e.type === "blocked") {
              // A risk veto is not a miss: the gap was there and the bot chose
              // not to take it. Saying which limit stopped it is the whole
              // point of showing the row at all. Repeats of one reason are
              // collapsed by the server, so a count here means "this kept
              // happening" rather than sixty copies of one sentence.
              const pairs = (
                e.symbols && e.symbols.length ? e.symbols : [e.symbol]
              )
                .map(esc)
                .join(", ");
              const repeats =
                e.count > 1
                  ? ` <span style="color:var(--muted);">×${Number(e.count)}</span>`
                  : "";
              return `
              <div class="blot-row blocked">
                <div class="blot-time">${fmtTime(e.time)}</div>
                <div class="blot-msg">
                  <span style="font-weight:700; color:var(--text);">${pairs}</span>
                  blocked — ${esc(e.reason)}${repeats}
                </div>
                <div class="blot-side">
                  <div class="c-yellow" style="font-weight:700;">${esc(e.limit)}</div>
                </div>
              </div>
            `;
            }
            return `
            <div class="blot-row ${i === 0 ? "hit" : ""}">
              <div class="blot-time">${fmtTime(e.time)}</div>
              <div class="blot-msg">
                <span style="font-weight:700; color:var(--text);">${e.symbol}</span>
                buy <span class="c-green">${e.buy_exchange}</span> @ ${fmtPrice(e.buy_price)}
                → sell <span class="c-red">${e.sell_exchange}</span> @ ${fmtPrice(e.sell_price)}
              </div>
              <div class="blot-side">
                <div class="c-yellow" style="font-weight:700;">+${Number(e.net_profit_pct).toFixed(2)}%</div>
                <div class="c-green" style="font-weight:700;">+$${Number(e.profit_usdt).toFixed(2)}</div>
              </div>
            </div>
          `;
          })
          .join("");
      }

      function renderLog(trades) {
        $("logCount").textContent = "(" + (trades ? trades.length : 0) + ")";
        const body = $("logBody");
        if (!trades || !trades.length) {
          body.innerHTML = "";
          return;
        }

        body.innerHTML = trades
          .map(
            (e) => `
          <tr>
            <td>${e.time.replace("T", " ")}</td>
            <td style="font-weight:700; color:var(--text);">${e.symbol}</td>
            <td class="c-green">${e.buy_exchange}</td>
            <td class="c-red">${e.sell_exchange}</td>
            <td>${fmtPrice(e.buy_price)}</td>
            <td>${fmtPrice(e.sell_price)}</td>
            <td>${Number(e.trade_size_usdt).toFixed(2)}</td>
            <td class="c-green">+${Number(e.profit_usdt).toFixed(4)}</td>
            <td class="c-yellow">+${Number(e.net_profit_pct).toFixed(3)}%</td>
            <td>${e.buy_order_id || e.sell_order_id ? "real" : "paper"}</td>
            <td title="${e.buy_order_id || ""} / ${e.sell_order_id || ""}">
              ${e.buy_order_id || e.sell_order_id ? "tracked" : "—"}
            </td>
          </tr>
        `,
          )
          .join("");
      }

      function renderBalances(s) {
        const panel = $("balancePanel");
        const body = $("balanceBody");
        const isReal = s.config && s.config.execution_mode === "real";
        panel.style.display = isReal ? "block" : "none";
        if (!isReal) return;
        const balances = s.balances || {};
        const valuation = s.balance_valuation || {};
        const rows = [];
        rows.push(
          `<div style="border-bottom:1px solid var(--border); padding-bottom:8px; margin-bottom:8px">
            <div style="display:flex; justify-content:space-between"><span>Free value</span><strong>${Number(valuation.free_usdt || 0).toFixed(2)} USDT</strong></div>
            <div style="display:flex; justify-content:space-between; margin-top:4px"><span>Total value</span><strong>${Number(valuation.total_usdt || 0).toFixed(2)} USDT</strong></div>
          </div>`,
        );
        Object.entries(balances).forEach(([exchange, currencies]) => {
          if (currencies.error) {
            rows.push(
              `<div><strong>${exchange}</strong>: ${currencies.error}</div>`,
            );
            return;
          }
          Object.entries(currencies).forEach(([currency, value]) => {
            if (Number(value.total || 0) > 0 || Number(value.used || 0) > 0) {
              rows.push(
                `<div style="display:flex; justify-content:space-between; gap:8px; margin:5px 0">
                  <span>${exchange} ${currency}</span>
                  <span>${Number(value.free || 0).toFixed(8)} (${Number(value.value_free_usdt || 0).toFixed(2)} USDT)</span>
                </div>`,
              );
            }
          });
        });
        body.innerHTML = rows.length
          ? rows.join("")
          : "No non-zero balances reported.";
      }

      // Exchange error text and alert bodies end up inside innerHTML below, and
      // they are strings an exchange chose, not ones this file wrote.
      function esc(value) {
        return String(value ?? "").replace(
          /[&<>"']/g,
          (c) =>
            ({
              "&": "&amp;",
              "<": "&lt;",
              ">": "&gt;",
              '"': "&quot;",
              "'": "&#39;",
            })[c],
        );
      }

      function row(label, value, colour) {
        const tone = colour ? ` class="${colour}"` : "";
        return `<div style="display:flex; justify-content:space-between; gap:8px">
          <span>${esc(label)}</span><strong${tone}>${esc(value)}</strong>
        </div>`;
      }

      function renderRisk(s) {
        const risk = s.risk || {};
        const body = $("riskBody");
        const stranded = risk.stranded || [];
        if (!Object.keys(risk).length) {
          body.innerHTML = "Waiting for the first scan...";
          $("btnResume").style.display = "none";
          $("btnClearStranded").style.display = "none";
          return;
        }

        const pnl = Number(risk.realized_today || 0);
        const used = Number(risk.daily_loss_used_pct || 0);
        const rows = [];
        if (risk.halted) {
          rows.push(
            `<div class="c-red" style="margin-bottom:6px"><strong>HALTED</strong> —
             ${esc(risk.halt_limit || "risk limit")}: ${esc(risk.halt_reason || "")}</div>`,
          );
        }
        if (risk.killed) {
          rows.push(
            `<div class="c-red" style="margin-bottom:6px"><strong>KILL SWITCH ENGAGED</strong>
             — restart the server to clear it.</div>`,
          );
        }
        rows.push(
          row(
            "Realized today",
            `${pnl.toFixed(2)} USDT`,
            pnl < 0 ? "c-red" : "c-green",
          ),
        );
        rows.push(
          row(
            "Daily loss budget",
            `${used.toFixed(0)}% of ${Number(risk.daily_loss_limit || 0).toFixed(2)} used`,
            used >= 80 ? "c-yellow" : null,
          ),
        );
        rows.push(
          row(
            "Trades today",
            `${risk.trades_today || 0} (${risk.wins_today || 0}W / ${risk.losses_today || 0}L)`,
          ),
        );
        rows.push(
          row(
            "Failures in a row",
            `${risk.consecutive_failures || 0} of ${risk.max_consecutive_failures || 0}`,
            (risk.consecutive_failures || 0) > 0 ? "c-yellow" : null,
          ),
        );
        rows.push(
          row(
            "Orders last minute",
            `${risk.orders_last_minute || 0} of ${risk.order_rate_limit || 0}`,
          ),
        );
        if (Number(risk.peak_equity || 0) > 0) {
          rows.push(
            row(
              "Drawdown from peak",
              `${Number(risk.drawdown || 0).toFixed(2)} USDT`,
            ),
          );
        }
        stranded.forEach((item) => {
          rows.push(
            `<div class="c-red" style="margin-top:6px">Stranded: ${esc(item.quantity)}
             ${esc(item.currency)} on ${esc(item.exchange)} — ${esc(item.detail || "")}</div>`,
          );
        });
        const limits = risk.recent_limits || [];
        if (limits.length) {
          const last = limits[limits.length - 1];
          rows.push(
            `<div style="margin-top:6px">Last limit hit: ${esc(last.limit)} —
             ${esc(last.reason)}</div>`,
          );
        }
        body.innerHTML = rows.join("");
        $("btnResume").style.display =
          risk.halted && !risk.killed ? "block" : "none";
        $("btnClearStranded").style.display = stranded.length
          ? "block"
          : "none";
      }

      function renderStartupCheck(s) {
        const panel = $("startupPanel");
        const check = s.startup_check;
        if (!check) {
          panel.style.display = "none";
          return;
        }
        panel.style.display = "block";
        const rows = [
          `<div class="${check.blocking ? "c-red" : check.clean ? "c-green" : "c-yellow"}">
            ${esc(check.summary || "")}
          </div>`,
        ];
        (check.discrepancies || []).forEach((item) => {
          rows.push(
            `<div style="margin-top:5px">${item.blocking ? "[BLOCKING] " : ""}${esc(item.detail)}</div>`,
          );
        });
        (check.errors || []).forEach((error) => {
          rows.push(
            `<div class="c-yellow" style="margin-top:5px">${esc(error)}</div>`,
          );
        });
        $("startupBody").innerHTML = rows.join("");
      }

      function renderAlerts(s) {
        const alerts = s.alerts || {};
        const recent = alerts.recent || [];
        const panel = $("alertPanel");
        if (!recent.length) {
          panel.style.display = "none";
          return;
        }
        panel.style.display = "block";
        $("alertSummary").textContent = alerts.configured
          ? `(${alerts.sent} sent, ${alerts.suppressed} deduped)`
          : "(local only — no webhook configured)";
        $("alertBody").innerHTML = recent
          .slice()
          .reverse()
          .map(
            (item) =>
              `<div style="margin-bottom:5px"><strong>${esc(item.severity || "")}</strong>
               ${esc(item.title || "")}${item.body ? " — " + esc(item.body) : ""}</div>`,
          )
          .join("");
      }

      function syncConfigInputs(s) {
        const cfg = s.config || {};
        const setWhenIdle = (id, value) => {
          const input = $(id);
          if (document.activeElement !== input) input.value = value;
        };
        setWhenIdle("cfgMode", cfg.mode || "demo");
        setWhenIdle("cfgExecutionMode", cfg.execution_mode || "paper");
        setWhenIdle("cfgStrategy", cfg.strategy || "cross_exchange");
        setWhenIdle("cfgTradeSize", Number(cfg.trade_size ?? 200));
        setWhenIdle("cfgFee", Number(cfg.fee ?? 0.001));
        setWhenIdle("cfgMinProfit", Number(cfg.min_profit ?? 0.15));
        setWhenIdle("cfgSlippage", Number(cfg.max_slippage ?? 0.25));
        setWhenIdle("cfgInterval", Number(cfg.interval ?? 2));
        if (document.activeElement !== $("cfgInterval")) {
          $("cfgIntervalVal").textContent =
            `${Number(cfg.interval ?? 2).toFixed(1)}s`;
        }

        activeExchanges = new Set(
          Array.isArray(s.active_exchanges) && s.active_exchanges.length
            ? s.active_exchanges
            : ALL_EXCHANGES,
        );
        activeSymbols = new Set(
          Array.isArray(s.active_symbols) && s.active_symbols.length
            ? s.active_symbols
            : ALL_SYMBOLS,
        );

        if (!chipUpdatePending) {
          rebuildChips(
            "chipExchanges",
            ALL_EXCHANGES,
            activeExchanges,
            "exchanges",
          );
          rebuildChips("chipSymbols", ALL_SYMBOLS, activeSymbols, "symbols");
        }
      }

      function applyState(s) {
        ALL_SYMBOLS = Array.isArray(s.available_symbols) ? s.available_symbols : [];
        ALL_EXCHANGES = Array.isArray(s.available_exchanges) ? s.available_exchanges : [];
        running = !!s.running;
        $("btnStart").style.display = running ? "none" : "inline-block";
        $("btnPause").style.display = running ? "inline-block" : "none";
        $("statusText").textContent = running ? "RUNNING" : "PAUSED";
        $("statusText").className =
          "metric-value " + (running ? "c-green" : "c-red");
        $("modeBadge").textContent =
          `${(s.config.mode || "demo").toUpperCase()} / ${(s.config.execution_mode || "paper").toUpperCase()}`;
        const readiness = s.readiness || {};
        $("readinessLabel").textContent = readiness.ready
          ? "REAL READY"
          : readiness.message || "Not ready";
        $("readinessLabel").className =
          "readiness-label " + (readiness.ready ? "c-green" : "c-yellow");
        const recovery = (s.unhedged_positions || [])[0];
        latestRecovery = recovery || null;
        $("btnRecover").style.display = recovery ? "block" : "none";
        showNotice(
          recovery
            ? `RECOVERY REQUIRED: ${recovery.symbol} quantity ${recovery.quantity} on ${recovery.recovery_exchange || recovery.buy_exchange}. ${recovery.error || "Manual recovery required."} Order ${recovery.buy_order_id || "unknown"}.`
            : s.error || "",
        );

        $("statPL").textContent = "$" + (s.total_profit || 0).toFixed(2);
        $("statTrades").textContent = s.trades_count || 0;
        const isRealExecution = s.config.execution_mode === "real";
        $("statValueLabel").textContent = isRealExecution
          ? "Live Account"
          : "Paper Portfolio";
        $("statValue").textContent = isRealExecution
          ? "$" + Number(s.portfolio_value || 0).toFixed(2)
          : "$" + Number(s.portfolio_value || 0).toFixed(2);
        $("scanCountLabel").textContent = "Scan #" + (s.scan_count || 0);

        syncConfigInputs(s);
        renderBalances(s);
        renderRisk(s);
        renderStartupCheck(s);
        renderAlerts(s);
        renderMatrix(s.quotes);
        renderBlotter(s.recent);
        renderLog(s.trades);
        renderMarketChart(s);
      }

      async function poll() {
        try {
          const s = await api("/api/state");
          applyState(s);
        } catch (e) {
          $("statusText").textContent = "OFFLINE";
          $("statusText").className = "metric-value c-red";
        }
        setTimeout(poll, 1000);
      }

      function bindControls() {
        $("btnTestConnection").onclick = testConnection;
        $("btnStart").onclick = () =>
          runAction(() => api("/api/start", { method: "POST" }));
        $("btnPause").onclick = () =>
          runAction(() => api("/api/pause", { method: "POST" }));
        $("btnReset").onclick = () =>
          runAction(() => api("/api/reset", { method: "POST" }));
        $("btnEmergency").onclick = () => {
          if (
            window.confirm(
              "Stop the engine immediately? Reset will be required before restarting.",
            )
          ) {
            runAction(() => api("/api/emergency-stop", { method: "POST" }));
          }
        };
        $("btnRecover").onclick = () => {
          if (!latestRecovery) return;
          if (
            !window.confirm(
              "Submit a market sell to close this unhedged position?",
            )
          )
            return;
          runAction(() =>
            api("/api/recovery/close", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                confirmation: "CLOSE_UNHEDGED_POSITION",
                buy_order_id: latestRecovery.buy_order_id,
              }),
            }),
          );
        };

        $("btnResume").onclick = () => {
          if (
            !window.confirm(
              "Resume after a risk halt?\n\nThis says the condition that " +
                "stopped the bot is understood and dealt with. The counters " +
                "for today are kept, so the same limit can halt it again.",
            )
          )
            return;
          runAction(() =>
            api("/api/risk/resume", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ confirmation: "RESUME_AFTER_HALT" }),
            }),
          );
        };

        $("btnClearStranded").onclick = () => {
          if (
            !window.confirm(
              "Confirm the stranded position is actually closed?\n\nThis " +
                "sells nothing. It only clears the record, and clearing it " +
                "while the coin is still there is how the next run trades " +
                "around inventory it does not know it holds.",
            )
          )
            return;
          runAction(() =>
            api("/api/risk/stranded/clear", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                confirmation: "STRANDED_POSITION_UNWOUND",
              }),
            }),
          );
        };

        $("cfgMode").onchange = async (e) => {
          await runAction(() =>
            applyStructuralConfig({ mode: e.target.value }),
          );
        };

        $("cfgExecutionMode").onchange = async (e) => {
          await runAction(() =>
            applyStructuralConfig({ execution_mode: e.target.value }),
          );
        };

        $("cfgStrategy").onchange = async (e) => {
          await runAction(() =>
            applyStructuralConfig({ strategy: e.target.value }),
          );
        };

        $("cfgTradeSize").onchange = async (e) => {
          await runAction(() =>
            api("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ trade_size: Number(e.target.value) }),
            }),
          );
        };

        $("cfgFee").onchange = async (e) => {
          await runAction(() =>
            api("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ fee: Number(e.target.value) }),
            }),
          );
        };

        $("cfgMinProfit").onchange = async (e) => {
          await runAction(() =>
            api("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ min_profit: Number(e.target.value) }),
            }),
          );
        };

        $("cfgSlippage").onchange = async (e) => {
          await runAction(() =>
            api("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ max_slippage: Number(e.target.value) }),
            }),
          );
        };

        $("cfgInterval").oninput = (e) => {
          $("cfgIntervalVal").textContent =
            `${Number(e.target.value).toFixed(1)}s`;
        };

        $("cfgInterval").onchange = async (e) => {
          await runAction(() =>
            api("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ interval: Number(e.target.value) }),
            }),
          );
        };
      }

      function rebuildChips(container, items, activeSet, kind) {
        const el = $(container);
        el.innerHTML = items
          .map(
            (it) =>
              `<button type="button" class="chip ${activeSet.has(it) ? "active" : ""}" aria-pressed="${activeSet.has(it)}" data-val="${it}">${it}</button>`,
          )
          .join("");
        el.querySelectorAll(".chip").forEach((chip) => {
          chip.onclick = async () => {
            if (chipUpdatePending) return;
            const val = chip.dataset.val;
            const nextSet = new Set(activeSet);
            if (nextSet.has(val)) nextSet.delete(val);
            else nextSet.add(val);
            const body = {};
            body[kind] = [...nextSet];
            chipUpdatePending = true;
            rebuildChips(container, items, nextSet, kind);
            try {
              await runAction(async () => {
                await applyStructuralConfig(body);
                const state = await api("/api/state");
                applyState(state);
                rebuildChips(
                  "chipExchanges",
                  ALL_EXCHANGES,
                  new Set(state.active_exchanges || []),
                  "exchanges",
                );
                rebuildChips(
                  "chipSymbols",
                  ALL_SYMBOLS,
                  new Set(state.active_symbols || []),
                  "symbols",
                );
              });
            } finally {
              chipUpdatePending = false;
            }
          };
        });
      }

      function renderMarketChart(s) {
        const canvas = document.getElementById("marketChart");
        const ctx = canvas.getContext("2d");
        const rect = canvas.getBoundingClientRect();
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.max(rect.width * ratio, 1);
        canvas.height = Math.max(rect.height * ratio, 1);
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

        const quotes = s.quotes || {};
        const pair = Object.keys(quotes)[0] || "BTC/USDT";
        const exchangeMap = quotes[pair] || {};
        const values = Object.values(exchangeMap)
          .map((q) => Number(q.bid || q.ask || 0))
          .filter((v) => v > 0);

        const isTriangular = s.config && s.config.strategy === "triangular";
        const cycle = s.latest_cycle;
        const series =
          s.chart_series && s.chart_series.length
            ? s.chart_series.map(Number)
            : values.length
              ? values
              : [
                  47000, 47250, 47180, 47440, 47360, 47620, 47580, 47750, 47690,
                  47920, 48050, 48210,
                ];

        const width = rect.width;
        const height = rect.height;
        const pad = 18;
        const min = Math.min(...series) * 0.995;
        const max = Math.max(...series) * 1.005;

        ctx.clearRect(0, 0, width, height);

        ctx.strokeStyle = "rgba(148, 163, 184, 0.18)";
        ctx.lineWidth = 1;
        for (let i = 0; i < 5; i++) {
          const y = pad + ((height - pad * 2) / 4) * i;
          ctx.beginPath();
          ctx.moveTo(pad, y);
          ctx.lineTo(width - pad, y);
          ctx.stroke();
        }

        const points = series.map((value, index) => {
          const x = pad + (index / (series.length - 1)) * (width - pad * 2);
          const y =
            height -
            pad -
            ((value - min) / (max - min || 1)) * (height - pad * 2);
          return { x, y };
        });

        ctx.beginPath();
        points.forEach((pt, idx) =>
          idx === 0 ? ctx.moveTo(pt.x, pt.y) : ctx.lineTo(pt.x, pt.y),
        );
        ctx.strokeStyle = "#25d290";
        ctx.lineWidth = 2.6;
        ctx.stroke();

        const labelColor = getComputedStyle(document.body)
          .getPropertyValue("--muted-2")
          .trim();
        const labelStep = Math.max(1, Math.ceil(points.length / 10));
        ctx.font = '10px "JetBrains Mono", monospace';
        ctx.textAlign = "center";
        points.forEach((pt, index) => {
          const shouldLabel =
            index === 0 ||
            index === points.length - 1 ||
            index % labelStep === 0;
          ctx.beginPath();
          ctx.arc(
            pt.x,
            pt.y,
            index === points.length - 1 ? 3.5 : 2.5,
            0,
            Math.PI * 2,
          );
          ctx.fillStyle = index === points.length - 1 ? "#f4b940" : "#25d290";
          ctx.fill();
          if (shouldLabel) {
            const label = isTriangular
              ? Number(series[index]).toFixed(4)
              : Number(series[index]).toLocaleString(undefined, {
                  maximumFractionDigits: 2,
                });
            const labelY = pt.y < pad + 24 ? pt.y + 16 : pt.y - 10;
            ctx.fillStyle = labelColor || "#c0d0e0";
            ctx.fillText(label, pt.x, labelY);
          }
        });

        const current = series[series.length - 1];
        const first = series[0];
        const deltaPct = ((current - first) / first) * 100;
        $("pairLabel").textContent = isTriangular
          ? cycle
            ? cycle.route.join(" → ")
            : "USDT → asset → asset → USDT"
          : pair;
        $("pairPrice").textContent =
          "$" +
          Number(current).toLocaleString(undefined, {
            maximumFractionDigits: 2,
          });
        const displayedPct =
          isTriangular && cycle ? Number(cycle.profit_pct) : deltaPct;
        $("pairChange").textContent =
          `${displayedPct >= 0 ? "+" : ""}${displayedPct.toFixed(2)}%`;
        $("pairVolume").textContent = isTriangular
          ? `Final ${Number(current).toFixed(4)} USDT`
          : "Vol " + (series.length * 2500).toLocaleString();

        const bids = isTriangular
          ? [cycle ? cycle.prices[cycle.symbols[2]] : 0]
          : values.slice().sort((a, b) => b - a);
        const asks = isTriangular
          ? [cycle ? cycle.prices[cycle.symbols[0]] : 0]
          : values.slice().sort((a, b) => a - b);
        $("bestBid").textContent =
          "$" +
          (bids[0] || 0).toLocaleString(undefined, {
            maximumFractionDigits: 2,
          });
        $("bestAsk").textContent =
          "$" +
          (asks[0] || 0).toLocaleString(undefined, {
            maximumFractionDigits: 2,
          });
        $("spreadValue").textContent =
          isTriangular && cycle
            ? `${Number(cycle.profit_usdt).toFixed(4)} USDT`
            : `${(((asks[0] - bids[0]) / (bids[0] || 1)) * 100 || 0).toFixed(3)}%`;
      }

      function toggleTheme() {
        const body = document.body;
        const isLight = body.classList.toggle("theme-light");
        document.getElementById("themeToggle").textContent = isLight
          ? "Dark"
          : "Light";
      }

      document
        .getElementById("themeToggle")
        .addEventListener("click", toggleTheme);
      bindControls();
      poll();
