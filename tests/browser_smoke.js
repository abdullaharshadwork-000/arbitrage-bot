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
  await page.waitForFunction(async () => {
    const backendState = await fetch("/api/state").then((response) => response.json());
    const shownBalance = document.querySelector("#totalBalance")?.textContent || "";
    return Math.abs(Number(shownBalance.replace(/,/g, "")) - Number(backendState.portfolio_value || 0)) <= 0.01;
  });
  await page.click("#themeToggle");
  const lightMode = await page.locator("body").evaluate((body) => body.classList.contains("light-mode"));

  await page.click('[data-page="trading"]');
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
  await page.selectOption("#executionTarget", "paper");
  await page.selectOption("#tradingStrategy", "cross_exchange");
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

  console.log(JSON.stringify({ modalVisible, lightMode, spaceMoving, traderSettingsSaved, portfolioRows, adminUserRows, terminalSymbols, terminalExchanges, chartSizes, errors }, null, 2));
  await browser.close();
  if (!modalVisible || !lightMode || !spaceMoving || portfolioRows < 1 || adminUserRows < 1 || terminalSymbols < 1 || terminalExchanges < 1 || errors.length) process.exit(1);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
