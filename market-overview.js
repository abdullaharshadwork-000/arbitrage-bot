/* Public market display only. Never changes configuration or submits an order. */
(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  let accountId = null, active = false, timer = null, controller = null, generation = 0;
  let chart = null, priceSeries, fastSeries, slowSeries, volumeSeries, previous = [], chartKey = "";
  const selections = ["marketExchange", "marketSymbol", "marketTimeframe"];
  const fmt = (value, digits = 2) => value !== null && value !== undefined && Number.isFinite(Number(value))
    ? Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits }) : "—";
  const cash = (value) => value == null ? "Unavailable" : `${fmt(value)} USDT`;
  const set = (id, value) => { el(id).textContent = value; };
  const timeText = (seconds) => seconds == null ? "Unavailable" : new Date(seconds * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short",
  });

  function themeChanged() {
    if (!chart) return;
    const light = document.body.classList.contains("light-mode");
    chart.applyOptions({
      layout: { background: { type: "solid", color: "transparent" }, textColor: light ? "#334155" : "#cbd5e1" },
      grid: { vertLines: { color: light ? "#cbd5e144" : "#94a3b81a" }, horzLines: { color: light ? "#cbd5e177" : "#94a3b833" } },
      rightPriceScale: { borderColor: light ? "#94a3b8" : "#334155" },
      timeScale: { borderColor: light ? "#94a3b8" : "#334155" },
    });
  }

  function ensureChart() {
    if (chart) return;
    const lib = window.LightweightCharts;
    if (!lib) throw new Error("Chart library missing. Run npm.cmd ci and reload.");
    chart = lib.createChart(el("marketChart"), {
      autoSize: true,
      layout: { attributionLogo: true, fontFamily: "Inter, sans-serif" },
      rightPriceScale: { scaleMargins: { top: .08, bottom: .24 } },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 4, minBarSpacing: 4 },
      // Unix seconds remain UTC; labels do not repeat full dates at every tick.
      localization: { timeFormatter: (stamp) => new Date(Number(stamp) * 1000).toLocaleString([], {
        timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
      }) + " UTC" },
    });
    priceSeries = chart.addSeries(lib.CandlestickSeries, {
      upColor: "#10b981", downColor: "#f43f5e", wickUpColor: "#10b981", wickDownColor: "#f43f5e", borderVisible: false,
    });
    fastSeries = chart.addSeries(lib.LineSeries, { color: "#a78bfa", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    slowSeries = chart.addSeries(lib.LineSeries, { color: "#f59e0b", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    volumeSeries = chart.addSeries(lib.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "volume", priceLineVisible: false, lastValueVisible: false });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: .82, bottom: 0 }, visible: false });
    themeChanged();
  }

  function draw(data) {
    if (!data.candles?.length) return;
    ensureChart();
    const key = `${data.exchange}|${data.symbol}|${data.chart_timeframe}`;
    const rows = data.candles;
    const sameKey = chartKey === key;
    const oldByTime = new Map(previous.map((row) => [row.time, row]));
    const latest = previous.at(-1)?.time;
    // setData only on first load, history corrections or bounded-history compaction.
    const corrected = sameKey && rows.some((row) => row.time < latest && oldByTime.has(row.time)
      && JSON.stringify(row) !== JSON.stringify(oldByTime.get(row.time)));
    const reset = !sameKey || !previous.length || corrected || previous.length > 1000
      || rows[0].time > latest || rows.at(-1).time < latest;
    const oldRange = sameKey && reset ? chart.timeScale().getVisibleRange() : null;
    const values = reset ? rows : rows.filter((row) => row.time >= latest);
    const volumes = values.map((row) => ({ time: row.time, value: row.volume, color: row.close >= row.open ? "#10b98166" : "#f43f5e66" }));
    if (reset) {
      priceSeries.setData(rows);
      fastSeries.setData(data.ema9); slowSeries.setData(data.ema21); volumeSeries.setData(volumes);
      previous = rows.slice();
      if (oldRange) chart.timeScale().setVisibleRange(oldRange);
      else chart.timeScale().fitContent();
    } else {
      values.forEach((row) => priceSeries.update(row));
      data.ema9.filter((row) => row.time >= latest).forEach((row) => fastSeries.update(row));
      data.ema21.filter((row) => row.time >= latest).forEach((row) => slowSeries.update(row));
      volumes.forEach((row) => volumeSeries.update(row));
      previous = previous.filter((row) => row.time < latest).concat(values);
    }
    const price = Number(data.last_price);
    const precision = price < 1 ? 6 : price < 100 ? 4 : 2;
    priceSeries.applyOptions({ priceFormat: { type: "price", precision, minMove: 10 ** -precision } });
    chartKey = key;
  }

  function accountPanel(account) {
    set("marketTarget", `${String(account.target || "unknown").toUpperCase()} · ${account.running ? "Running" : "Paused"}`);
    set("marketReadiness", account.halted ? `HALTED: ${account.halt_reason || "Reconciliation required"}` : account.readiness?.message || "Account checks pending");
    set("marketDailyPnl", cash(account.daily_realized_pnl));
    set("marketLossRemaining", cash(account.daily_loss_remaining));
    set("marketLossLimit", `Saved limit: ${cash(account.daily_loss_limit)}. Not a guarantee of maximum loss.`);
    const pos = account.position;
    set("marketProtection", pos && account.position_source === "paper" ? "Simulated stop / target only" : "Not verified for real funds");
    set("marketPosition", pos
      ? `${pos.exchange} ${pos.symbol} · ${pos.quantity} units · Stop ${cash(pos.stop)} · Target ${cash(pos.target)}`
      : "No tracked trend position. Existing arbitrage exposure is managed separately.");
    set("marketExecutionBlocker", account.signal_blocker || "This analysis never authorizes an order.");
  }

  function render(data) {
    draw(data);
    const usable = Boolean(data.available && data.fresh);
    const signal = usable ? data.signal || {} : {};
    set("marketPrice", data.available ? `${fmt(data.last_price, Number(data.last_price) < 1 ? 6 : 2)} USDT` : "Price unavailable");
    set("marketRegime", usable ? data.regime : "Unconfirmed trend");
    set("marketFreshness", usable ? `Updated ${timeText(data.as_of)}` : "STALE / UNAVAILABLE — entries blocked");
    el("marketFreshness").className = usable ? "text-success" : "text-danger";
    el("marketChart").classList.toggle("market-stale", !usable);
    set("marketChartMessage", usable ? "Live public candles. Last candle may still be forming; decisions use completed 1m candles."
      : "Current feed is not verified. Any chart shown is last-known history, not a current signal.");
    set("marketSuggestion", usable ? data.suggestion : "WAIT");
    el("marketSuggestion").dataset.action = usable ? data.suggestion : "WAIT";
    set("marketReason", data.suggestion_note || data.signal?.reason || "Market data unavailable; no new entry signal.");
    set("marketRsi", fmt(signal.rsi14, 1));
    set("marketMove5", signal.momentum5_pct == null ? "—" : `${fmt(signal.momentum5_pct)}%`);
    set("marketMove10", signal.momentum10_pct == null ? "—" : `${fmt(signal.momentum10_pct)}%`);
    set("marketVolumeRatio", signal.volume_ratio == null ? "—" : `${fmt(signal.volume_ratio)}×`);
    set("marketCandleTime", `Last completed signal candle opened: ${timeText(data.last_closed_candle)}`);
    accountPanel(data.account || {});
  }

  function unavailable(message) {
    set("marketSuggestion", "WAIT"); el("marketSuggestion").dataset.action = "WAIT";
    set("marketFreshness", "Market update unavailable"); el("marketFreshness").className = "text-danger";
    set("marketReason", message); set("marketRegime", "Unconfirmed trend");
    set("marketPrice", "Price unavailable"); el("marketChart").classList.add("market-stale");
    set("marketChartMessage", "Last-known history only. No current signal while the connection is unavailable.");
    ["marketRsi", "marketMove5", "marketMove10", "marketVolumeRatio", "marketDailyPnl", "marketLossRemaining"].forEach((id) => set(id, "—"));
    set("marketReadiness", "Account status unavailable; do not rely on a previous readiness result.");
  }

  async function poll() {
    if (!active || !accountId || document.hidden || controller) return;
    const version = generation;
    const pending = new AbortController(); controller = pending;
    const timeout = setTimeout(() => pending.abort(), 18000);
    try {
      const query = new URLSearchParams({ exchange: el("marketExchange").value, symbol: el("marketSymbol").value, timeframe: el("marketTimeframe").value });
      const response = await fetch(`/api/market/overview?${query}`, { credentials: "same-origin", cache: "no-store", signal: pending.signal });
      if (!response.ok) throw new Error(response.status === 401 ? "Sign in again to view your account." : `Market request failed (${response.status})`);
      const data = await response.json();
      if (version === generation && active) render(data);
    } catch (error) {
      if (version === generation && active) unavailable(error.name === "AbortError" ? "Market request timed out. Retrying without submitting orders." : error.message);
    } finally {
      clearTimeout(timeout);
      if (controller === pending) controller = null;
      if (version === generation && active && !document.hidden) timer = setTimeout(() => { timer = null; poll(); }, 5000);
    }
  }

  function cancel() {
    generation += 1; clearTimeout(timer); timer = null;
    controller?.abort(); controller = null;
  }

  function clearChart() {
    if (chart) [priceSeries, fastSeries, slowSeries, volumeSeries].forEach((series) => series.setData([]));
    previous = []; chartKey = "";
  }

  function stop() {
    cancel(); active = false; accountId = null; clearChart();
    unavailable("Sign in to load market analysis.");
    accountPanel({});
  }

  function options(id, values) {
    const select = el(id), old = select.value;
    if (JSON.stringify([...select.options].map((option) => option.value)) === JSON.stringify(values)) return;
    select.replaceChildren(...values.map((value) => new Option(value === "okx" ? "OKX" : value, value)));
    if (values.includes(old)) select.value = old;
  }

  function sync(user, state, visible) {
    if (!user) { stop(); return; }
    const identity = String(user.id ?? user.username);
    if (accountId !== identity) { stop(); accountId = identity; }
    options("marketExchange", state?.available_exchanges || []);
    options("marketSymbol", state?.available_symbols || []);
    const next = Boolean(visible && el("marketExchange").value && el("marketSymbol").value && !document.hidden);
    if (!next && active) cancel();
    active = next;
    if (active && !timer && !controller) poll();
  }

  selections.forEach((id) => el(id).addEventListener("change", () => {
    cancel(); clearChart(); unavailable("Loading selected exchange candles…");
    if (active) poll();
  }));
  document.addEventListener("visibilitychange", () => {
    cancel();
    if (document.hidden) unavailable("Tab paused. Waiting for a fresh update when you return.");
    else if (active) poll();
  });
  window.MarketOverviewUI = { sync, stop, themeChanged };
})();
