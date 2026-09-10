const { chromium } = require("playwright-core");
const fs = require("fs");

(async () => {
  const browserUser = `browser-test-${Date.now()}`;
  const localChrome = "C:/Program Files/Google/Chrome/Application/chrome.exe";
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || (fs.existsSync(localChrome) ? localChrome : chromium.executablePath()),
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => errors.push(`page: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() >= 400) errors.push(`http ${response.status()}: ${response.url()}`);
  });

  // The chart is tested with deterministic public-market fixtures. No real
  // credentials, exchange calls or order submissions occur in this test.
  let marketPolls = 0, staleMarket = false, marketRequestMode = "normal";
  await page.route("**/api/market/overview?*", async (route) => {
    marketPolls += 1;
    const query = new URL(route.request().url()).searchParams;
    const stamp = Math.floor(Date.now() / 60000) * 60;
    const candles = Array.from({ length: 80 }, (_, index) => ({
      time: stamp - (79 - index) * 60, open: 100 + index, high: 103 + index,
      low: 99 + index, close: 102 + index + (index === 79 ? marketPolls / 100 : 0), volume: 1000 + index,
      closed: index < 79,
    }));
    const payload = {
      available: true, fresh: !staleMarket, exchange: query.get("exchange"), symbol: query.get("symbol"),
      chart_timeframe: query.get("timeframe"), candles,
      ema9: candles.slice(8).map((r) => ({ time: r.time, value: r.close - 2 })),
      ema21: candles.slice(20).map((r) => ({ time: r.time, value: r.close - 4 })),
      as_of: Date.now() / 1000, last_price: candles.at(-1).close, last_closed_candle: stamp - 60,
      regime: "Rising", suggestion: "BUY CANDIDATE", suggestion_note: "EMA, momentum and volume agree (offline test)",
      signal: { action: "buy", reason: "Offline fixture", rsi14: 62, momentum5_pct: .5, momentum10_pct: 1, volume_ratio: 1.2 },
      account: { target: "paper", running: false, readiness: { ready: true, message: "Offline paper fixture" },
        daily_realized_pnl: -2, daily_loss_limit: 10, daily_loss_remaining: 8,
        signal_blocker: "Real-fund trend execution remains disabled pending authenticated qualification." },
    };
    if (marketRequestMode === "delay") await new Promise((resolve) => setTimeout(resolve, 800));
    await route.fulfill({ json: payload });
  });

  await page.goto(process.env.ARBICORE_URL || "http://127.0.0.1:8000", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#registerScreen", { state: "visible" });
  const spaceStart = await page.locator(".space-bg").evaluate((node) => getComputedStyle(node).backgroundPosition);
  await page.waitForTimeout(700);
  const spaceEnd = await page.locator(".space-bg").evaluate((node) => getComputedStyle(node).backgroundPosition);
  const spaceMoving = spaceStart !== spaceEnd;
  await page.fill("#registerUsername", browserUser);
  await page.fill("#registerEmail", `${browserUser}@localhost`);
  await page.fill("#registerPassword", "local-test-password");
  await page.click("#registerForm button[type=submit]");
  await page.waitForSelector("#loginScreen", { state: "visible" });
  await page.fill("#loginUsername", browserUser);
  await page.fill("#loginPassword", "local-test-password");
  await page.click("#loginForm button[type=submit]");
  await page.waitForSelector("#appContainer", { state: "visible" });
  await page.waitForSelector("#onboardingModal.active", { state: "visible" });
  await page.check("#riskConsent");
  await page.check("#termsConsent");
  await page.click("#completeOnboarding");
  await page.waitForSelector("#onboardingModal", { state: "hidden" });
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#appContainer", { state: "visible" });
  if (await page.locator("#loginScreen").isVisible() || await page.locator("#registerScreen").isVisible()) {
    throw new Error("Authenticated session was lost after browser refresh");
  }
  const paperDefaults = await page.evaluate(() => fetch("/api/state").then((response) => response.json()));
  if (paperDefaults.config?.mode !== "live" || paperDefaults.config?.execution_mode !== "paper") {
    throw new Error("New trader did not retain live-data paper mode after refresh");
  }
  const signalVenues = (paperDefaults.signal_capabilities || []).map((item) => item.exchange).sort();
  if (JSON.stringify(signalVenues) !== JSON.stringify(["binance", "bybit", "kucoin", "okx"])) {
    throw new Error("Signal qualification does not cover all four exchanges");
  }
  if (paperDefaults.signal_capabilities.some((item) => item.production_ready)) {
    throw new Error("Unqualified signal strategy was advertised as production ready");
  }
  if (Math.abs(Number(paperDefaults.paper_portfolio_value) - 20000) > 0.01) {
    throw new Error("Demo wallet did not start with a fixed 20,000 USDT balance");
  }
  await page.waitForFunction(async () => {
    const backendState = await fetch("/api/state").then((response) => response.json());
    const shownBalance = document.querySelector("#totalBalance")?.textContent || "";
    return Math.abs(Number(shownBalance.replace(/,/g, "")) - Number(backendState.portfolio_value || 0)) <= 0.01;
  });
  await page.click("#themeToggle");
  const lightMode = await page.locator("body").evaluate((body) => body.classList.contains("light-mode"));

  await page.click('[data-page="trading"]');
  await page.waitForFunction(() => document.querySelector("#marketSuggestion").textContent === "BUY CANDIDATE");
  const marketCanvas = await page.locator("#marketChart canvas").first().elementHandle();
  const firstMarketPrice = await page.textContent("#marketPrice");
  await page.waitForFunction((old) => document.querySelector("#marketPrice").textContent !== old, firstMarketPrice);
  if (!await marketCanvas.evaluate((canvas) => canvas.isConnected)) throw new Error("Market chart canvas was recreated on polling");
  if (!await page.locator("#marketLossRemaining").textContent().then((value) => value.includes("8.00"))) throw new Error("Account risk budget missing");
  await page.click("#themeToggle");
  if (!await marketCanvas.evaluate((canvas) => canvas.isConnected)) throw new Error("Theme change recreated market chart");
  if (process.env.ARBICORE_SCREENSHOT_DIR) {
    fs.mkdirSync(process.env.ARBICORE_SCREENSHOT_DIR, { recursive: true });
    await page.locator(".market-overview").screenshot({ path: `${process.env.ARBICORE_SCREENSHOT_DIR}/market-dark.png` });
  }
  await page.click("#themeToggle");
  if (process.env.ARBICORE_SCREENSHOT_DIR) await page.locator(".market-overview").screenshot({ path: `${process.env.ARBICORE_SCREENSHOT_DIR}/market-light.png` });
  staleMarket = true;
  await page.selectOption("#marketTimeframe", "5m");
  await page.waitForFunction(() => document.querySelector("#marketFreshness").textContent.includes("STALE"));
  if (await page.textContent("#marketSuggestion") !== "WAIT") throw new Error("Stale data retained a buy suggestion");
  staleMarket = false;
  marketRequestMode = "delay";
  await page.selectOption("#marketExchange", "bybit");
  await page.selectOption("#marketExchange", "okx");
  await page.waitForFunction(() => document.querySelector("#marketSuggestion").textContent === "BUY CANDIDATE");
  marketRequestMode = "normal";
  await page.setViewportSize({ width: 390, height: 844 });
  const marketWidth = await page.locator(".market-overview").evaluate((node) => ({ left: node.getBoundingClientRect().left, right: node.getBoundingClientRect().right }));
  if (marketWidth.left < 0 || marketWidth.right > 391) throw new Error("Market card overflows mobile viewport");
  await page.locator("#marketExecutionBlocker").scrollIntoViewIfNeeded();
  if (!await page.locator("#marketExecutionBlocker").isVisible()) throw new Error("Mobile risk details cannot be reached");
  if (process.env.ARBICORE_SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.ARBICORE_SCREENSHOT_DIR}/market-mobile.png` });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.selectOption("#tradingMode", "live");
  await page.selectOption("#executionTarget", "testnet");
  await page.selectOption("#tradingStrategy", "triangular");
  await page.waitForTimeout(3500);
  const stagedConfigPersists = await page.evaluate(() => ({
    mode: document.querySelector("#tradingMode").value,
    target: document.querySelector("#executionTarget").value,
    strategy: document.querySelector("#tradingStrategy").value,
  }));
  if (stagedConfigPersists.mode !== "live" || stagedConfigPersists.target !== "testnet" || stagedConfigPersists.strategy !== "triangular") {
    throw new Error("Pending trading configuration was overwritten by dashboard refresh");
  }
  await page.selectOption("#tradingStrategy", "signal_trend");
  const signalSetup = await page.evaluate(() => ({
    mode: document.querySelector("#tradingMode").value,
    target: document.querySelector("#executionTarget").value,
    venues: document.querySelectorAll('input[name="tradingExchange"]:checked').length,
  }));
  if (signalSetup.mode !== "live" || signalSetup.target !== "paper" || signalSetup.venues !== 1) {
    throw new Error("Signal strategy did not select live-data paper execution with one venue");
  }
  await page.selectOption("#executionTarget", "paper");
  await page.selectOption("#tradingStrategy", "cross_exchange");
  await page.locator('input[name="tradingExchange"]').evaluateAll((inputs) => inputs.forEach((input) => { input.checked = true; }));
  await page.selectOption("#tradingMode", "demo");
  await page.click("#startEngineButton");
  await page.waitForFunction(() => document.querySelector("#engineStatus")?.textContent.trim() === "Running");
  await page.waitForFunction(async () => {
    const state = await fetch("/api/state").then((response) => response.json());
    return state.scan_count > 0;
  });

  await page.click('[data-page="portfolio"]');
  await page.waitForFunction(() => document.querySelector("#portfolioTable")?.textContent.includes("Paper"));
  const portfolioRows = await page.locator("#portfolioTable tr").count();

  await page.click('[data-page="dashboard"]');
  await page.click("#viewAllTradesButton");
  await page.waitForSelector("#allTradesModal.active", { state: "visible" });
  const modalVisible = await page.locator("#allTradesModal").isVisible();
  const chartSizes = await page.locator("#profitChart, #distributionChart").evaluateAll(
    (nodes) => nodes.map((node) => ({ width: node.width, height: node.height }))
  );
  const chartInstanceBefore = await page.evaluate(() => Chart.getChart(document.querySelector("#profitChart"))?.id);
  await page.waitForTimeout(3000);
  const chartStability = await page.evaluate(() => {
    const chart = Chart.getChart(document.querySelector("#profitChart"));
    return { id: chart?.id, visibleTimeLabels: chart?.scales?.x?.ticks?.filter((tick) => tick.label).length || 0 };
  });
  if (chartStability.id !== chartInstanceBefore || chartStability.visibleTimeLabels > 7) {
    throw new Error("Profit chart was recreated or rendered too many time labels during polling");
  }

  await page.click("#allTradesModal .modal-close");
  await page.click('[data-page="trading"]');
  await page.click("#startEngineButton");
  await page.waitForFunction(() => document.querySelector("#engineStatus")?.textContent.trim() === "Paused");

  await page.click('aside [data-page="settings"]');
  await page.fill("#settingsScanInterval", "17");
  await page.locator("#settingsScanInterval").blur();
  await page.waitForTimeout(3500);
  if (await page.inputValue("#settingsScanInterval") !== "17") {
    throw new Error("Polling discarded unsaved runtime settings after blur");
  }
  await page.fill("#credentialApiKey", "dummy-not-a-real-key");
  await page.fill("#credentialApiSecret", "dummy-not-a-real-secret");
  await page.selectOption("#credentialExchange", "kucoin");
  if (await page.inputValue("#credentialApiKey") || await page.inputValue("#credentialApiSecret")) {
    throw new Error("Switching exchanges retained another venue's credentials");
  }
  const credentialOptions = await page.locator("#credentialExchange option").count();
  const passphraseVisible = await page.locator("#credentialPassphraseGroup").isVisible();
  if (credentialOptions !== 4 || !passphraseVisible) {
    throw new Error("Multi-exchange credential form is incomplete");
  }
  await page.fill("#settingsScanInterval", "2.5");
  await page.fill("#settingsMinProfit", "0.20");
  await page.fill("#settingsMaxSlippage", "0.20");
  await page.fill("#settingsOrdersMinute", "10");
  await page.fill("#settingsTradesHour", "8");
  await page.click("#runtimeSettingsForm button[type=submit]");
  await page.waitForFunction(async () => {
    const state = await fetch("/api/state").then((response) => response.json());
    return state.config.interval === 2.5 && state.config.max_trades_per_hour === 8;
  });
  const traderSettingsSaved = true;

  await page.click("#userAvatar");
  await page.click("#logoutButton");
  await page.waitForSelector("#loginScreen", { state: "visible" });
  await page.fill("#loginUsername", "admin");
  await page.fill("#loginPassword", "Admin@12345");
  await page.click("#loginForm button[type=submit]");
  await page.waitForSelector("#admin-dashboard.active", { state: "visible" });
  const adminStats = await page.evaluate(() => fetch("/api/admin/stats").then((response) => response.json()));
  await page.waitForFunction(
    (count) => Number(document.querySelector("#totalUsers")?.textContent) === count,
    adminStats.stats.total_users
  );
  await page.click('[data-page="users"]');
  const adminUserRows = await page.locator("#usersTable tr").count();

  // Reuse the authenticated page. A separate context correctly has no access
  // to backend-driven exchange and symbol data.
  const terminal = page;
  terminal.on("console", (message) => {
    if (message.type() === "error") errors.push(`terminal console: ${message.text()}`);
  });
  terminal.on("pageerror", (error) => errors.push(`terminal page: ${error.message}`));
  await terminal.goto(new URL("/arbitrage-bot-terminal.html", process.env.ARBICORE_URL || "http://127.0.0.1:8000").href, { waitUntil: "domcontentloaded" });
  await terminal.waitForFunction(() => document.querySelectorAll("#chipSymbols .chip").length > 0);
  const terminalSymbols = await terminal.locator("#chipSymbols .chip").count();
  const terminalExchanges = await terminal.locator("#chipExchanges .chip").count();

  console.log(JSON.stringify({ modalVisible, lightMode, spaceMoving, traderSettingsSaved, portfolioRows, credentialOptions, passphraseVisible, adminUserRows, terminalSymbols, terminalExchanges, chartSizes, marketPolls, errors }, null, 2));
  await browser.close();
  if (!modalVisible || !lightMode || !spaceMoving || portfolioRows < 1 || credentialOptions !== 4 || !passphraseVisible || adminUserRows < 1 || terminalSymbols < 1 || terminalExchanges < 1 || errors.length) process.exit(1);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
