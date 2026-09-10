let currentUser = null;
      let engineState = null;
      let allTrades = [];
      let refreshTimer = null;
      let refreshInFlight = false;
      let tradingConfigDirty = false;
      let runtimeSettingsDirty = false;
      let lastConnectionTestResult = null;
      let connectionTestInFlight = false;
      let credentialStatuses = [];
      let chartSignature = "";
      const charts = {};
      const $ = (id) => document.getElementById(id);

      async function api(path, options = {}) {
        const response = await fetch(path, {
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...(options.headers || {}) },
          ...options,
        });
        const contentType = response.headers.get("content-type") || "";
        const result = contentType.includes("json") ? await response.json() : await response.text();
        if (!response.ok || result?.ok === false) throw new Error(result?.error || `Request failed (${response.status})`);
        return result;
      }

      function notify(message, error = false) {
        // Safety/error notices are never suppressed by cosmetic preferences.
        if (!error && currentUser && $("notificationPreference").value !== "all") return;
        const toast = $("toast");
        toast.textContent = message;
        toast.className = `toast active${error ? " error" : ""}`;
        clearTimeout(notify.timer);
        notify.timer = setTimeout(() => toast.classList.remove("active"), 3500);
      }

      function escapeHtml(value) {
        const node = document.createElement("span");
        node.textContent = value == null ? "" : String(value);
        return node.innerHTML;
      }

      function money(value) {
        return Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      }

      function formatDateTime(value) {
        if (!value) return "--";
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return date.toLocaleString([], {
          year: "numeric", month: "short", day: "numeric",
          hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
      }

      function animateMetric(id, nextValue, digits = 2) {
        const element = $(id);
        const target = Number(nextValue);
        if (!element || !Number.isFinite(target)) return;
        const rendered = Number(element.dataset.metricValue);
        const fallback = Number(String(element.textContent).replace(/,/g, ""));
        const start = Number.isFinite(rendered) ? rendered : (Number.isFinite(fallback) ? fallback : target);
        if (Math.abs(start - target) < Math.pow(10, -digits) / 2) {
          element.dataset.metricValue = String(target);
          return;
        }
        if (element.metricAnimation) cancelAnimationFrame(element.metricAnimation);
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const duration = reduceMotion ? 0 : 550;
        const started = performance.now();
        const format = (value) => value.toLocaleString(undefined, {
          minimumFractionDigits: digits, maximumFractionDigits: digits,
        });
        const frame = (now) => {
          const progress = duration ? Math.min(1, (now - started) / duration) : 1;
          const eased = 1 - Math.pow(1 - progress, 3);
          element.textContent = format(start + (target - start) * eased);
          if (progress < 1) element.metricAnimation = requestAnimationFrame(frame);
          else {
            element.dataset.metricValue = String(target);
            element.metricAnimation = null;
          }
        };
        element.metricAnimation = requestAnimationFrame(frame);
      }

      function setTheme(theme, persist = true) {
        const light = theme === "light";
        document.body.classList.toggle("light-mode", light);
        $("themeToggle").innerHTML = `<i class="fas fa-${light ? "sun" : "moon"}"></i>`;
        $("themeToggle").title = `Switch to ${light ? "dark" : "light"} mode`;
        document.querySelectorAll("#themePreference").forEach((select) => select.value = theme);
        if (persist && currentUser) api("/api/account/preferences", {
          method: "POST", body: JSON.stringify({theme}),
        }).catch((error) => notify(error.message, true));
        Object.values(charts).forEach((chart) => chart.destroy());
        Object.keys(charts).forEach((key) => delete charts[key]);
        chartSignature = "";
        if (currentUser) renderCharts(allTrades);
        window.MarketOverviewUI?.themeChanged();
      }

      $("themeToggle").addEventListener("click", () =>
        setTheme(document.body.classList.contains("light-mode") ? "dark" : "light"));
      setTheme("dark", false);

      function showRegister() {
        $("loginScreen").classList.add("hidden");
        $("registerScreen").classList.remove("hidden");
        $("registerUsername").focus();
      }

      function showLogin(username = "") {
        $("registerScreen").classList.add("hidden");
        $("loginScreen").classList.remove("hidden");
        if (username) $("loginUsername").value = username;
        $(username ? "loginPassword" : "loginUsername").focus();
      }

      $("showRegisterButton").addEventListener("click", showRegister);
      $("showLoginButton").addEventListener("click", () => showLogin());
      $("forgotPasswordButton").addEventListener("click", async () => {
        const identity = window.prompt("Enter your ArbiCore username or email address:");
        if (!identity) return;
        try {
          const result = await api("/api/auth/password-reset/request", {
            method: "POST", body: JSON.stringify({ identity: identity.trim() }),
          });
          notify(result.message);
        } catch (error) { notify(error.message, true); }
      });

      $("registerForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = event.submitter;
        if (button) button.disabled = true;
        const username = $("registerUsername").value.trim();
        try {
          await api("/api/auth/register", {
            method: "POST",
            body: JSON.stringify({
              username,
              email: $("registerEmail").value.trim(),
              password: $("registerPassword").value,
            }),
          });
          $("registerForm").reset();
          showLogin(username);
          notify("Trader account created. Sign in with your new password.");
        } catch (error) {
          notify(error.message, true);
        } finally {
          if (button) button.disabled = false;
        }
      });

      $("loginForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = event.submitter;
        if (button) button.disabled = true;
        try {
          const result = await api("/api/auth/login", {
            method: "POST",
            body: JSON.stringify({
              username: $("loginUsername").value.trim(),
              password: $("loginPassword").value,
              totp_code: $("loginTotp").value.trim(),
            }),
          });
          currentUser = result.user;
          showApp();
          await refreshAll();
          await checkOnboarding();
          refreshTimer = setInterval(refreshAll, 2500);
        } catch (error) {
          notify(error.message, true);
        } finally {
          if (button) button.disabled = false;
        }
      });

      function showApp() {
        tradingConfigDirty = false;
        runtimeSettingsDirty = false;
        lastConnectionTestResult = null;
        $("exchangeCredentialForm").reset();
        $("loginScreen").classList.add("hidden");
        $("appContainer").style.display = "grid";
        $("userName").textContent = currentUser.username;
        $("userAvatar").textContent = currentUser.username.slice(0, 1).toUpperCase();
        $("profileUsername").value = currentUser.username;
        $("profileEmail").value = localStorage.getItem(`arbicore-email-${currentUser.username}`) || "";
        $("notificationPreference").value = "all";
        setTheme("dark", false);
        const accountId = currentUser.id;
        api("/api/account/preferences").then((preferences) => {
          if (currentUser?.id !== accountId) return;
          setTheme(preferences.theme, false);
          $("notificationPreference").value = preferences.notifications;
        }).catch((error) => notify(error.message, true));
        const admin = currentUser.role === "admin";
        $("adminSection").style.display = admin ? "block" : "none";
        openPage(admin ? "admin-dashboard" : "dashboard");
      }

      async function checkOnboarding() {
        if (!currentUser || currentUser.role === "admin") return;
        try {
          const status = await api("/api/onboarding");
          $("experienceMode").value = status.experience_mode || "beginner";
          $("riskConsent").checked = Boolean(status.risk_accepted);
          $("termsConsent").checked = Boolean(status.terms_accepted);
          $("onboardingModal").classList.toggle("active", !status.completed);
        } catch (error) {
          notify(`Onboarding status failed: ${error.message}`, true);
        }
      }

      $("completeOnboarding").addEventListener("click", async () => {
        const button = $("completeOnboarding");
        button.disabled = true;
        try {
          await api("/api/onboarding", {
            method: "POST",
            body: JSON.stringify({
              experience_mode: $("experienceMode").value,
              risk_accepted: $("riskConsent").checked,
              terms_accepted: $("termsConsent").checked,
            }),
          });
          // A new trader should practise against the market that exists now,
          // while keeping every fill and balance simulated. The synthetic feed
          // remains an explicit offline tutorial choice.
          $("tradingMode").value = "live";
          $("executionTarget").value = "paper";
          tradingConfigDirty = true;
          $("onboardingModal").classList.remove("active");
          openPage("trading");
          notify("Safety setup saved. Paper trading will use real market data.");
        } catch (error) {
          notify(error.message, true);
        } finally {
          button.disabled = false;
        }
      });

      function openPage(page) {
        if (currentUser?.role !== "admin" && ["admin-dashboard", "users", "trades", "analytics"].includes(page)) {
          notify("Administrator access is required.", true);
          return;
        }
        document.querySelectorAll(".page").forEach((item) => item.classList.remove("active"));
        $(page)?.classList.add("active");
        document.querySelectorAll(".sidebar-link").forEach((item) => item.classList.remove("active"));
        document.querySelector(`[data-page="${page}"]`)?.classList.add("active");
        document.querySelector("aside").classList.remove("mobile-open");
        $("mobileMenu").setAttribute("aria-expanded", "false");
        requestAnimationFrame(() => Object.values(charts).forEach((chart) => chart.resize()));
        window.MarketOverviewUI?.sync(currentUser, engineState, page === "trading");
      }

      function toggleDropdown() {
        const open = $("dropdownMenu").classList.toggle("active");
        $("userAvatar").setAttribute("aria-expanded", String(open));
      }
      document.addEventListener("click", (event) => {
        if (!event.target.closest(".user-menu")) $("dropdownMenu").classList.remove("active");
        if (event.target.classList.contains("modal-backdrop")) closeAllTradesModal();
      });
      document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeAllTradesModal(); });
      document.querySelectorAll("[data-page]").forEach((item) =>
        {
          item.addEventListener("click", () => openPage(item.dataset.page));
          if (item.matches('[role="button"]')) item.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              openPage(item.dataset.page);
            }
          });
        });
      $("userAvatar").addEventListener("click", toggleDropdown);
      $("logoutButton").addEventListener("click", logout);
      $("viewAllTradesButton").addEventListener("click", openAllTradesModal);
      $("closeAllTradesButton").addEventListener("click", closeAllTradesModal);
      $("exportTradesButton").addEventListener("click", exportTrades);
      $("startEngineButton").addEventListener("click", toggleEngine);
      $("testConnectionButton").addEventListener("click", testExchangeConnection);
      $("themePreference").addEventListener("change", (event) => setTheme(event.target.value));

      async function refreshAll() {
        if (refreshInFlight || !currentUser) return;
        refreshInFlight = true;
        try {
          await Promise.all([refreshState(), refreshHistory()]);
          await Promise.all([refreshUserStats(), refreshCredentialStatus(), refreshAccountSecurity(), refreshOperationalInsights(), currentUser.role === "admin" ? refreshAdmin() : Promise.resolve()]);
        } finally {
          refreshInFlight = false;
        }
      }

      async function refreshUserStats() {
        try {
          const result = await api("/api/user/stats");
          const stats = result.stats || {};
          animateMetric("totalProfit", stats.total_profit, 2);
          animateMetric("winRate", stats.win_rate, 1);
          $("profileMemberSince").textContent = stats.member_since || "This session";
          $("profileTradeCount").textContent = Number(stats.total_trades || 0).toLocaleString();
          $("profileWinRate").textContent = `${Number(stats.win_rate || 0).toFixed(1)}%`;
          $("profileTotalProfit").textContent = `$${money(stats.total_profit)}`;
        } catch (error) {
          notify(`User statistics failed: ${error.message}`, true);
        }
      }

      async function refreshOperationalInsights() {
        try {
          const [qualityResult, inventoryResult, intelligenceResult] = await Promise.all([
            api("/api/execution-quality"), api("/api/inventory-plan"),
            api("/api/intelligence"),
          ]);
          const quality = qualityResult.quality || {};
          $("statusSlippage").textContent = `${money(quality.realized_slippage_usdt)} USDT`;
          $("statusRejectedOrders").textContent = Number(quality.rejected_orders || 0);
          const paperExecution = engineState?.config?.execution_mode !== "real";
          $("statusInventory").textContent = paperExecution
            ? "Simulated wallet"
            : (inventoryResult.ready ? "Ready for one route" : "Funding incomplete");
          $("statusInventory").className = paperExecution || inventoryResult.ready ? "text-success" : "text-warning";
          const model = intelligenceResult.model || {};
          const latest = (model.latest_decisions || [])[0] || null;
          $("statusModelRegime").textContent = String(model.regime || "warming_up").replaceAll("_", " ");
          $("statusModelRegime").className = model.regime === "stressed" ? "text-danger" : (model.ready ? "text-success" : "text-warning");
          $("statusModelConfidence").textContent = latest
            ? `${(Number(latest.confidence || 0) * 100).toFixed(1)}% (${Number(latest.observations || 0)} samples)`
            : `${Number(model.observations || 0)}/${Number(model.minimum_observations || 0)} observations`;
          $("statusPredictedEdge").textContent = latest
            ? `${Number(latest.predicted_edge_pct || 0) >= 0 ? "+" : ""}${Number(latest.predicted_edge_pct || 0).toFixed(3)}% / floor ${Number(latest.adaptive_floor_pct || 0).toFixed(3)}%`
            : "Waiting for an opportunity";
          const evidence = intelligenceResult.strategy_evidence || {};
          $("statusStrategyEvidence").textContent = evidence.recommended_strategy
            ? `${evidence.recommended_strategy} leads live-data history`
            : "No live-data strategy qualified yet";
          $("intelligenceExplanation").textContent = latest?.reason || evidence.message || model.warning || "Probabilistic estimate only.";
        } catch (_) {
          $("statusInventory").textContent = "Unavailable";
          $("statusModelRegime").textContent = "Unavailable";
        }
      }

      async function refreshState() {
        try {
          engineState = await api("/api/state");
          const running = Boolean(engineState.running);
          const signalMode = engineState.config?.strategy === "signal_trend";
          $("signalPanel").classList.toggle("hidden", !signalMode);
          if (signalMode) {
            $("signalCapabilities").innerHTML = (engineState.signal_capabilities || []).map((item) =>
              `<p><strong>${escapeHtml(item.label)}</strong> · Live-data paper supported · ${escapeHtml(item.environment)}<br><span class="text-muted">${escapeHtml(item.blocker)}</span></p>`).join("");
            const position = engineState.signal_position;
            $("signalPosition").textContent = position
              ? `${position.symbol}: ${Number(position.quantity).toPrecision(6)} units · Entry $${money(position.entry)} · Stop $${money(position.stop)} · Target $${money(position.target)}`
              : "No open signal position.";
            $("signalReports").innerHTML = Object.entries(engineState.signal_reports || {}).map(([symbol, report]) =>
              `<p><strong>${escapeHtml(symbol)} — ${escapeHtml(report.action)}</strong><br>${escapeHtml(report.reason)}${report.rsi14 != null ? `<br>RSI ${Number(report.rsi14).toFixed(1)} · 5m ${Number(report.momentum5_pct).toFixed(2)}% · 10m ${Number(report.momentum10_pct).toFixed(2)}%` : ""}</p>`).join("") || "Waiting for the first candle scan.";
          }
          const workerFailed = engineState.last_scan_status === "crashed";
          $("engineStatus").textContent = workerFailed ? "Crashed" : (running ? "Running" : "Paused");
          $("engineStatus").className = `badge badge-${workerFailed ? "danger" : (running ? "success" : "warning")}`;
          $("startEngineButton").innerHTML = `<i class="fas fa-${running ? "pause" : "rocket"}"></i> ${running ? "Pause Engine" : "Start Engine"}`;
          $("scanRate").textContent = signalMode ? "Automatic · 5s checks / closed 1m signals" : `${Number(engineState.config?.interval || 0).toFixed(1)}s / cycle`;
          $("opportunityCount").textContent = `${Number(engineState.attempts_count || 0)} detected`;
          animateMetric("activeTrades", engineState.scan_count, 0);
          const scanStatus = String(engineState.last_scan_status || (running ? "starting" : "paused"));
          const scanLabel = scanStatus.replaceAll("_", " ");
          const scanTime = engineState.last_scan_completed_at
            ? formatDateTime(engineState.last_scan_completed_at)
            : "waiting for first scan";
          $("scanProgressText").textContent = running
            ? `${scanLabel} · ${scanTime}`
            : (workerFailed
                ? `Crashed · ${scanTime}`
                : `Paused · ${Number(engineState.scan_count || 0)} completed`);
          $("scanProgressText").title = engineState.last_scan_message || "";
          $("scanProgressText").className = `metric-change ${scanStatus === "feed_unavailable" || scanStatus === "crashed" ? "text-danger" : ""}`;
          const target = engineState.config?.execution_mode === "real"
            ? (engineState.config?.sandbox_mode ? "testnet" : "production") : "paper";
          $("statusDataSource").textContent = engineState.config?.mode === "live" ? "Live exchange order books" : "Synthetic tutorial";
          $("statusExecution").textContent = target === "production" ? "REAL FUNDS" : (target === "testnet" ? "Testnet" : "Paper");
          $("statusExecution").className = target === "production" ? "text-danger" : "text-success";
          const paperCash = Object.values(engineState.balances || {}).reduce((venueTotal, venue) =>
            venueTotal + Object.values(venue || {}).reduce((total, balance) =>
              total + (balance?.source === "paper" ? Number(balance.cash_usdt || 0) : 0), 0), 0);
          $("statusFreeUsdt").textContent = target === "paper"
            ? `${money(paperCash)} virtual USDT`
            : `${money(engineState.balance_valuation?.free_usdt)} USDT`;
          const latestExchange = (engineState.exchange_status || [])[0];
          $("statusConnectionStage").textContent = target === "paper"
            ? "Not required (paper)"
            : (latestExchange?.stage || (engineState.readiness?.ready ? "complete" : "Not tested"));
          const soak = engineState.readiness?.testnet_soak;
          $("statusSoak").textContent = soak ? `${Number(soak.completed || 0)}/${Number(soak.required || 0)} cycles` : "--";
          const feedHealth = engineState.feed_health || {};
          const feedSource = feedHealth.source || (engineState.config?.mode === "demo" ? "demo" : "REST");
          $("statusFeedHealth").textContent = `${feedSource} · ${feedHealth.status || "warming up"} · ${Number(feedHealth.usable_symbols || 0)}/${Number(feedHealth.symbols || 0)} markets`;
          $("statusFeedHealth").className = feedHealth.status === "online"
            ? "text-success"
            : (feedHealth.status === "degraded" ? "text-warning" : "text-danger");
          $("scanIntervalGuidance").textContent = `Recommended minimum for this setup: ${Number(engineState.recommended_scan_interval || 1).toFixed(1)} seconds. Faster values are adjusted automatically.`;
          updateDisplayedBalance(target);
          if (!tradingConfigDirty) {
            $("tradingMode").value = engineState.config?.mode || "demo";
            $("executionTarget").value = target;
            $("tradingStrategy").value = engineState.config?.strategy || "cross_exchange";
            $("tradeSize").value = engineState.config?.trade_size || 200;
            $("maxPosition").value = engineState.config?.max_position_notional || 10;
            $("maxDailyLoss").value = engineState.config?.max_daily_loss || 1;
            $("realAcknowledgementGroup").style.display = target === "production" ? "block" : "none";
          }
          if (!runtimeSettingsDirty && !document.activeElement?.closest("#runtimeSettingsForm")) {
            $("settingsScanInterval").value = engineState.config?.interval ?? 5;
            $("settingsMinProfit").value = engineState.config?.min_profit ?? 0.15;
            $("settingsMaxSlippage").value = engineState.config?.max_slippage ?? 0.25;
            $("settingsOrdersMinute").value = engineState.config?.max_orders_per_minute ?? 20;
            $("settingsTradesHour").value = engineState.config?.max_trades_per_hour ?? 12;
            $("settingsIntelligence").value = engineState.config?.intelligence_enabled === false ? "disabled" : "enabled";
            $("settingsModelConfidence").value = Math.round(Number(engineState.config?.min_model_confidence ?? 0.65) * 100);
            renderExchangeChoices(engineState.available_exchanges || [], engineState.active_exchanges || []);
          }
          const readiness = engineState.readiness || {};
          const paperExecution = engineState.config?.execution_mode !== "real";
          const credentialText = paperExecution ? "" : (readiness.credentials || []).map((item) =>
            `${item.exchange}: ${item.configured ? "configured" : "missing"}`).join(" · ");
          $("realReadiness").innerHTML = paperExecution
            ? '<strong>Paper ready</strong> — Simulated execution uses no API key or real funds.'
            : `<strong>${readiness.ready ? "Ready" : "Not ready"}</strong> — ${escapeHtml(readiness.message || "")}${credentialText ? `<br>${escapeHtml(credentialText)}` : ""}`;
          if (connectionTestInFlight) {
            $("realReadiness").textContent = "Testing exchange access… Please wait.";
            $("realReadiness").classList.remove("text-danger", "text-success");
          } else if (lastConnectionTestResult) {
            $("realReadiness").textContent = lastConnectionTestResult.message;
            $("realReadiness").classList.toggle("text-danger", lastConnectionTestResult.failed);
            $("realReadiness").classList.toggle("text-success", !lastConnectionTestResult.failed);
          }
          document.querySelector(".status-indicator span").textContent = running ? "Engine running" : "Engine paused";
          document.querySelector(".status-dot").style.background = running ? "var(--success)" : "var(--warning)";
          renderPortfolio(engineState);
          window.MarketOverviewUI?.sync(currentUser, engineState, $("trading").classList.contains("active"));
          if (engineState.error) notify(engineState.error, true);
        } catch (error) {
          notify(`State refresh failed: ${error.message}`, true);
        }
      }

      function updateDisplayedBalance(target = $("executionTarget").value) {
        if (!engineState) return;
        const realTarget = target !== "paper";
        const balance = realTarget
          ? engineState.live_portfolio_value
          : engineState.paper_portfolio_value;
        const available = balance !== null && balance !== undefined;
        if (available) animateMetric("totalBalance", balance, 2);
        else {
          if ($("totalBalance").metricAnimation) cancelAnimationFrame($("totalBalance").metricAnimation);
          $("totalBalance").textContent = "--";
          delete $("totalBalance").dataset.metricValue;
        }
        $("totalBalanceSource").innerHTML = realTarget
          ? (available
              ? '<i class="fas fa-building-columns"></i> Connected exchange valuation'
              : '<i class="fas fa-plug-circle-xmark"></i> Test exchange access to load balance')
          : (engineState.config?.mode === "live"
              ? '<i class="fas fa-satellite-dish"></i> Demo wallet valued from live markets'
              : '<i class="fas fa-flask"></i> Synthetic tutorial wallet');
        $("totalBalanceSource").className = `metric-change balance-source ${realTarget && available ? "text-success" : ""}`;
        const performance = engineState.paper_performance;
        $("paperPerformance").hidden = realTarget || !performance;
        if (!realTarget && performance) {
          $("paperPerformance").textContent = `This wallet: trades $${money(performance.trade_profit)} · market movement $${money(performance.inventory_change)} · net $${money(performance.net_change)}`;
          $("paperPerformance").className = `metric-change ${performance.net_change < 0 ? "text-danger" : "text-success"}`;
        }
      }

      function exchangeLabel(exchange) {
        return exchange === "okx"
          ? "OKX"
          : String(exchange || "").replace(/^./, (letter) => letter.toUpperCase());
      }

      function renderExchangeChoices(available, selected) {
        const selectedSet = new Set(selected || []);
        $("tradingExchanges").innerHTML = (available || []).map((exchange) => `
          <label class="badge badge-info" style="display:inline-flex;align-items:center;gap:.45rem;cursor:pointer;padding:.55rem .7rem">
            <input type="checkbox" name="tradingExchange" value="${escapeHtml(exchange)}"
              ${selectedSet.has(exchange) ? "checked" : ""} style="width:auto" />
            ${escapeHtml(exchangeLabel(exchange))}
          </label>`).join("");
      }

      async function toggleEngine() {
        const button = $("startEngineButton");
        button.disabled = true;
        try {
          if (engineState?.running) {
            await api("/api/pause", { method: "POST", body: "{}" });
            notify("Engine paused safely.");
          } else {
            const target = $("executionTarget").value;
            const config = stagedTradingConfig();
            if (target === "production") {
              if (!window.confirm(`REAL FUNDS WILL BE USED. Start ${config.strategy} trading with ${config.trade_size} USDT per trade?`)) return;
            }
            await api("/api/config", { method: "POST", body: JSON.stringify(config) });
            if (config.execution_mode === "real") {
              await testExchangeConnection({ throwOnError: true });
            }
            await api("/api/start", { method: "POST", body: "{}" });
            tradingConfigDirty = false;
            notify(`Engine started in ${target} execution mode.`);
          }
          await refreshState();
        } catch (error) {
          notify(error.message, true);
        } finally {
          button.disabled = false;
        }
      }

      function stagedTradingConfig() {
        const mode = $("tradingMode").value;
        const target = $("executionTarget").value;
        const executionMode = target === "paper" ? "paper" : "real";
        const strategy = $("tradingStrategy").value;
        if (strategy === "signal_trend" && (target !== "paper" || mode !== "live")) {
          throw new Error("Automatic trend signals require Live data and Paper execution.");
        }
        const tradeSize = Number($("tradeSize").value);
        const maxPosition = Number($("maxPosition").value);
        const maxDailyLoss = Number($("maxDailyLoss").value);
        const exchanges = [...document.querySelectorAll('input[name="tradingExchange"]:checked')]
          .map((input) => input.value);
        if (!Number.isFinite(tradeSize) || tradeSize < 10) throw new Error("Trade size must be at least 10 USDT.");
        if (!Number.isFinite(maxPosition) || maxPosition < tradeSize) throw new Error("Maximum position must be at least the trade size.");
        if (!Number.isFinite(maxDailyLoss) || maxDailyLoss <= 0) throw new Error("Maximum daily loss must be greater than zero.");
        if (!exchanges.length) throw new Error("Select at least one trading venue.");
        if (strategy === "cross_exchange" && exchanges.length < 2) throw new Error("Cross-exchange trading needs at least two venues.");
        if (strategy === "triangular" && exchanges.length !== 1) throw new Error("Triangular trading needs exactly one venue.");
        if (strategy === "signal_trend" && exchanges.length !== 1) throw new Error("Automatic trend signals need exactly one venue.");
        const config = {
          mode: executionMode === "real" ? "live" : mode,
          execution_mode: executionMode,
          strategy,
          trade_size: tradeSize,
          max_position_notional: maxPosition,
          max_daily_loss: maxDailyLoss,
          exchanges,
        };
        if (executionMode === "real") {
          config.sandbox_mode = target === "testnet";
          config.real_trading_enabled = true;
        }
        if (target === "production") {
          config.real_trading_ack = $("realAcknowledgement").value.trim();
          if (config.real_trading_ack !== "I ACCEPT REAL LOSSES") {
            throw new Error('Type "I ACCEPT REAL LOSSES" exactly before testing production access.');
          }
        }
        return config;
      }

      async function testExchangeConnection({ throwOnError = false } = {}) {
        if (connectionTestInFlight) {
          if (throwOnError) throw new Error("An exchange access test is already in progress.");
          return null;
        }
        connectionTestInFlight = true;
        const button = $("testConnectionButton");
        button.disabled = true;
        const readiness = $("realReadiness");
        const originalLabel = button.innerHTML;
        let finalMessage = "";
        let failed = false;
        button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Testing securely...';
        readiness.textContent = "Testing authenticated exchange access; no live order will be placed...";
        try {
          const config = stagedTradingConfig();
          if (config.execution_mode !== "real") {
            throw new Error("Select Testnet or Production real execution before testing exchange access.");
          }
          await api("/api/config", { method: "POST", body: JSON.stringify(config) });
          tradingConfigDirty = false;
          const result = await api("/api/test-connection", { method: "POST", body: "{}" });
          const summary = (result.exchanges || []).map((item) =>
            `${item.exchange}: ${item.ok ? "connected" : item.error || "failed"}`).join(" · ");
          const message = summary || "Authenticated exchange access verified.";
          finalMessage = `Access test completed — ${message}. Trading readiness is checked separately before starting.`;
          lastConnectionTestResult = { message: finalMessage, failed: false };
          readiness.textContent = finalMessage;
          notify(message);
          return result;
        } catch (error) {
          failed = true;
          finalMessage = `Not ready — ${error.message}`;
          lastConnectionTestResult = { message: finalMessage, failed: true };
          readiness.textContent = finalMessage;
          notify(error.message, true);
          if (throwOnError) throw error;
          return null;
        } finally {
          connectionTestInFlight = false;
          button.disabled = false;
          button.innerHTML = originalLabel;
          await refreshState();
          if (finalMessage) {
            readiness.textContent = finalMessage;
            readiness.classList.toggle("text-danger", failed);
            readiness.classList.toggle("text-success", !failed);
          }
        }
      }

      $("executionTarget").addEventListener("change", (event) => {
        tradingConfigDirty = true;
        lastConnectionTestResult = null;
        $("realAcknowledgementGroup").style.display = event.target.value === "production" ? "block" : "none";
        if (event.target.value !== "paper") $("tradingMode").value = "live";
        updateDisplayedBalance(event.target.value);
      });
      $("tradingMode").addEventListener("change", () => { tradingConfigDirty = true; lastConnectionTestResult = null; });
      $("tradingStrategy").addEventListener("change", () => {
        tradingConfigDirty = true;
        lastConnectionTestResult = null;
        if ($("tradingStrategy").value === "signal_trend") {
          $("tradingMode").value = "live";
          $("executionTarget").value = "paper";
          $("realAcknowledgementGroup").style.display = "none";
          const venues = [...document.querySelectorAll('input[name="tradingExchange"]')];
          const selected = venues.find((input) => input.checked) || venues[0];
          venues.forEach((input) => { input.checked = input === selected; });
        }
      });
      $("tradingExchanges").addEventListener("change", () => { tradingConfigDirty = true; lastConnectionTestResult = null; });
      $("tradeSize").addEventListener("input", () => { tradingConfigDirty = true; lastConnectionTestResult = null; });
      $("maxPosition").addEventListener("input", () => { tradingConfigDirty = true; lastConnectionTestResult = null; });
      $("maxDailyLoss").addEventListener("input", () => { tradingConfigDirty = true; lastConnectionTestResult = null; });

      async function refreshHistory() {
        try {
          const result = await api("/api/history");
          allTrades = result.trades || [];
          renderTradeTables(allTrades);
          renderCharts(allTrades);
        } catch (error) {
          notify(`Trade history failed: ${error.message}`, true);
        }
      }

      function tradeRow(trade, includeUser = false) {
        const profit = Number(trade.profit_usdt || 0);
        const strategy = trade.strategy === "signal_trend" ? "Trend" : trade.strategy === "triangular" ? "Triangle" : "Cross";
        const pair = trade.sell_exchange ? `${trade.buy_exchange || "--"} → ${trade.sell_exchange}` : trade.symbol;
        const successful = ["completed", "filled", "closed"].includes(
          String(trade.status || "").toLowerCase());
        return `<tr>${includeUser ? `<td>${escapeHtml(currentUser?.username || "operator")}</td>` : ""}<td>${escapeHtml(pair || trade.symbol || "--")}</td><td><span class="badge badge-info">${strategy}</span></td><td class="${profit >= 0 ? "text-success" : "text-danger"}">${profit >= 0 ? "+" : ""}$${money(profit)}</td><td><span class="badge badge-${successful ? "success" : "warning"}">${escapeHtml(trade.status || "completed")}</span></td><td>${escapeHtml(formatDateTime(trade.time))}</td></tr>`;
      }

      function renderTradeTables(trades) {
        const empty = '<tr><td colspan="6" class="text-center text-muted">No trades recorded yet. Start the paper engine to collect data.</td></tr>';
        const query = $("tradeFilter")?.value.trim().toLowerCase() || "";
        const filtered = trades.filter((trade) => JSON.stringify(trade).toLowerCase().includes(query));
        $("recentTradesTable").innerHTML = trades.length ? trades.slice(0, 5).map((t) => tradeRow(t)).join("") : empty;
        $("modalTradesTable").innerHTML = trades.length ? trades.map((t) => tradeRow(t, true)).join("") : empty;
        $("allTradesTable").innerHTML = filtered.length ? filtered.map((t) => tradeRow(t, true)).join("")
          : '<tr><td colspan="7" class="text-center text-muted">No matching trades.</td></tr>';
        $("historyTable").innerHTML = trades.length ? trades.map((t) => tradeRow(t)).join("") : empty;
      }

      function renderPortfolio(state) {
        const rows = [];
        Object.entries(state.balances || {}).forEach(([exchange, balances]) => {
          Object.entries(balances || {}).forEach(([market, balance]) => {
            if (market === "error" || typeof balance !== "object") return;
            if (balance.source === "paper") {
              rows.push(`<tr><td>${escapeHtml(exchange)}</td><td>${escapeHtml(market)}</td><td>$${money(balance.cash_usdt)}</td><td>${Number(balance.coin || 0).toFixed(8)} ${escapeHtml(balance.asset)}</td><td><span class="badge badge-info">Paper</span></td></tr>`);
            } else {
              rows.push(`<tr><td>${escapeHtml(exchange)}</td><td>${escapeHtml(market)}</td><td>${money(balance.free)}</td><td>${money(balance.used)}</td><td><span class="badge badge-success">Live</span></td></tr>`);
            }
          });
        });
        $("portfolioTable").innerHTML = rows.length ? rows.join("") : '<tr><td colspan="5" class="text-center text-muted">No balance snapshot is available.</td></tr>';
      }

      function chartColors() {
        const css = getComputedStyle(document.body);
        return { text: css.getPropertyValue("--text-muted").trim(), border: css.getPropertyValue("--border").trim(), card: css.getPropertyValue("--bg-card").trim() };
      }

      function drawCanvasFallback(canvas, values, kind = "line") {
        const ratio = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        canvas.width = Math.max(320, rect.width * ratio);
        canvas.height = Math.max(240, rect.height * ratio);
        const ctx = canvas.getContext("2d");
        ctx.scale(ratio, ratio);
        const width = canvas.width / ratio;
        const height = canvas.height / ratio;
        const colors = chartColors();
        ctx.clearRect(0, 0, width, height);
        if (!values.length) return;
        if (kind === "doughnut") {
          const total = values.reduce((sum, value) => sum + value, 0) || 1;
          let angle = -Math.PI / 2;
          ["#6366f1", "#10b981"].forEach((color, index) => {
            const next = angle + Math.PI * 2 * ((values[index] || 0) / total);
            ctx.beginPath(); ctx.arc(width / 2, height / 2, 88, angle, next); ctx.arc(width / 2, height / 2, 52, next, angle, true); ctx.closePath();
            ctx.fillStyle = color; ctx.fill(); angle = next;
          });
          return;
        }
        const pad = 32;
        const min = Math.min(0, ...values), max = Math.max(1, ...values);
        ctx.strokeStyle = colors.border; ctx.lineWidth = 1;
        for (let row = 0; row < 4; row++) { const y = pad + row * (height - pad * 2) / 3; ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(width - pad, y); ctx.stroke(); }
        ctx.strokeStyle = "#10b981"; ctx.lineWidth = 3; ctx.beginPath();
        values.forEach((value, index) => {
          const x = pad + index * (width - pad * 2) / Math.max(1, values.length - 1);
          const y = height - pad - (value - min) / Math.max(1, max - min) * (height - pad * 2);
          index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        });
        ctx.stroke();
      }

      function upsertChart(key, canvas, config) {
        if (charts[key]) {
          charts[key].data = config.data;
          charts[key].options = config.options;
          charts[key].update("none");
          return charts[key];
        }
        charts[key] = new Chart(canvas, config);
        return charts[key];
      }

      function renderCharts(trades) {
        $("profitChartEmpty").classList.toggle("hidden", trades.length > 0);
        $("distributionChartEmpty").classList.toggle("hidden", trades.length > 0);
        const nextSignature = JSON.stringify(trades.map((trade) => [trade.time, trade.profit_usdt, trade.status, trade.strategy]));
        if (nextSignature === chartSignature && charts.profit && charts.distribution) return;
        chartSignature = nextSignature;
        const chronological = [...trades].reverse();
        let cumulative = 0;
        const fullLabels = chronological.map((trade, index) => {
          const date = new Date(trade.time);
          return Number.isNaN(date.getTime()) ? `Trade ${index + 1}` : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
        });
        const labels = chronological.map((trade, index) => {
          const date = new Date(trade.time);
          return Number.isNaN(date.getTime()) ? `#${index + 1}` : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        });
        const labelStep = Math.max(1, Math.ceil(labels.length / 6));
        const profits = chronological.map((trade) => Number((cumulative += Number(trade.profit_usdt || 0)).toFixed(8)));
        const outcomes = {
          Profitable: trades.filter((trade) => Number(trade.profit_usdt || 0) > 0).length,
          Loss: trades.filter((trade) => Number(trade.profit_usdt || 0) < 0).length,
          "Break-even": trades.filter((trade) => Number(trade.profit_usdt || 0) === 0).length,
        };
        const outcomeEntries = Object.entries(outcomes).filter(([, count]) => count > 0);
        if (typeof Chart === "undefined") {
          drawCanvasFallback($("profitChart"), profits, "line");
          drawCanvasFallback($("distributionChart"), outcomeEntries.map(([, count]) => count), "doughnut");
          return;
        }
        const colors = chartColors();
        const profitCanvas = $("profitChart");
        const gradient = profitCanvas.getContext("2d").createLinearGradient(0, 0, 0, 300);
        gradient.addColorStop(0, "rgba(16,185,129,.38)");
        gradient.addColorStop(1, "rgba(16,185,129,0)");
        const moneyTooltip = (context) => `${context.dataset.label}: $${Number(context.parsed.y || 0).toFixed(4)}`;
        const lineOptions = {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { intersect: false, mode: "index" },
          animation: { duration: 550 },
          plugins: {
            legend: { display: false },
            tooltip: {
              displayColors: false,
              callbacks: {
                title: (items) => fullLabels[items[0]?.dataIndex] || "",
                label: moneyTooltip,
              },
            },
          },
          scales: {
            x: {
              ticks: {
                color: colors.text,
                autoSkip: false,
                maxRotation: 0,
                callback: (value, index) => (index % labelStep === 0 || index === labels.length - 1) ? labels[index] : "",
              },
              grid: { display: false },
            },
            y: {
              ticks: { color: colors.text, callback: (value) => `$${Number(value).toFixed(2)}` },
              grid: { color: (context) => context.tick.value === 0 ? "rgba(239,68,68,.55)" : colors.border },
            },
          },
        };
        upsertChart("profit", profitCanvas, {
          type: "line",
          data: {
            labels: labels.length ? labels : ["No trades"],
            datasets: [{
              label: "Cumulative profit",
              data: profits.length ? profits : [0],
              borderColor: "#10b981",
              backgroundColor: gradient,
              pointBackgroundColor: chronological.map((trade) => Number(trade.profit_usdt || 0) >= 0 ? "#10b981" : "#ef4444"),
              pointBorderColor: "rgba(255,255,255,.85)",
              pointRadius: profits.length <= 30 ? 3 : 0,
              pointHoverRadius: 6,
              borderWidth: 2.5,
              fill: true,
              tension: .28,
            }],
          },
          options: lineOptions,
        });
        upsertChart("distribution", $("distributionChart"), {
          type: "doughnut",
          data: {
            labels: outcomeEntries.length ? outcomeEntries.map(([label]) => label) : ["No trades"],
            datasets: [{
              data: outcomeEntries.length ? outcomeEntries.map(([, count]) => count) : [1],
              backgroundColor: outcomeEntries.length ? outcomeEntries.map(([label]) => ({ Profitable: "#10b981", Loss: "#ef4444", "Break-even": "#f59e0b" })[label]) : [colors.border],
              borderColor: "rgba(255,255,255,.14)",
              borderWidth: 2,
              hoverOffset: 8,
            }],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: "68%",
            plugins: {
              legend: { position: "bottom", labels: { color: colors.text, usePointStyle: true, padding: 18 } },
              tooltip: { callbacks: { label: (context) => {
                const total = context.dataset.data.reduce((sum, value) => sum + Number(value), 0);
                return `${context.label}: ${context.raw} (${total ? (Number(context.raw) / total * 100).toFixed(1) : 0}%)`;
              } } },
            },
          },
        });
        if ($("dailyProfitChart")) upsertChart("daily", $("dailyProfitChart"), { type: "bar", data: { labels: labels.slice(-20), datasets: [{ data: chronological.slice(-20).map((t) => Number(t.profit_usdt || 0)), backgroundColor: "#6366f1" }] }, options: lineOptions });
        if ($("userGrowthChart")) upsertChart("users", $("userGrowthChart"), { type: "line", data: { labels: ["Current"], datasets: [{ data: [Number($("totalUsers").textContent) || 1], borderColor: "#3b82f6" }] }, options: lineOptions });
      }

      async function refreshAdmin() {
        try {
          const [stats, users] = await Promise.all([api("/api/admin/stats"), api("/api/admin/users")]);
          $("totalUsers").textContent = stats.stats.total_users;
          $("adminTotalProfit").textContent = money(stats.stats.total_profit);
          $("platformVolume").textContent = (Number(stats.stats.platform_volume) / 1e6).toFixed(2);
          $("avgWinRate").textContent = Number(stats.stats.avg_win_rate).toFixed(1);
          $("apiHealth").innerHTML = '<i class="fas fa-circle-check"></i> Local';
          $("feedLatency").textContent = engineState?.feed_health?.fetch_seconds != null ? `${Number(engineState.feed_health.fetch_seconds).toFixed(3)}s` : "--";
          $("activeSessions").textContent = stats.stats.platform_health.active_sessions;
          $("errorCount").textContent = engineState?.error ? 1 : 0;
          $("activeTraders").textContent = stats.stats.active_traders;
          $("adminTradeCount").textContent = allTrades.length.toLocaleString();
          $("averageTradeSize").textContent = `$${money(allTrades.length ? allTrades.reduce((sum, trade) => sum + Number(trade.trade_size_usdt || 0), 0) / allTrades.length : 0)}`;
          $("networkStatus").textContent = engineState?.error ? "Attention" : "Healthy";
          $("networkStatus").className = `badge badge-${engineState?.error ? "warning" : "success"}`;
          $("usersTable").innerHTML = (users.users || []).map((user) => `<tr><td><strong>${escapeHtml(user.username)}</strong></td><td><span class="badge badge-${user.status === "Active" ? "success" : "warning"}">${escapeHtml(user.status)}</span></td><td>${Number(user.trades).toLocaleString()}</td><td class="${Number(user.profit) >= 0 ? "text-success" : "text-danger"}">$${money(user.profit)}</td><td>${Number(user.win_rate || 0).toFixed(1)}%</td><td>${escapeHtml(user.join_date)}</td></tr>`).join("");
          $("topPerformersTable").innerHTML = (users.users || []).slice(0, 3).map((user, index) => `<tr><td>${index + 1}</td><td>${escapeHtml(user.username)}</td><td class="${Number(user.profit) >= 0 ? "text-success" : "text-danger"}">$${money(user.profit)}</td><td>${Number(user.trades).toLocaleString()}</td><td><span class="badge badge-info">Session</span></td></tr>`).join("") || '<tr><td colspan="5" class="text-center text-muted">No operator sessions yet.</td></tr>';
        } catch (error) {
          notify(`Admin data failed: ${error.message}`, true);
        }
      }

      function openAllTradesModal() {
        renderTradeTables(allTrades);
        $("allTradesModal").classList.add("active");
      }
      function closeAllTradesModal() { $("allTradesModal").classList.remove("active"); }
      function exportTrades() { window.location.assign("/api/trades.csv"); }

      async function logout() {
        clearInterval(refreshTimer);
        window.MarketOverviewUI?.stop();
        // Let an already-started dashboard poll finish while the session is
        // still valid. Otherwise logout revokes its cookie halfway through and
        // the remaining requests flash avoidable 401/403 errors in the UI.
        const refreshDeadline = performance.now() + 5000;
        while (refreshInFlight && performance.now() < refreshDeadline) {
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
        if (engineState?.running) {
          const pauseFirst = window.confirm(
            "Your trading engine is running. Select OK to pause it before signing out, " +
            "or Cancel to leave it running unattended."
          );
          if (pauseFirst) {
            try { await api("/api/pause", { method: "POST", body: "{}" }); }
            catch (error) { notify(`Could not pause safely: ${error.message}`, true); return; }
          }
        }
        try {
          const result = await api("/api/auth/logout", { method: "POST", body: "{}" });
          if (result.engine_continues) window.alert(result.message);
        } catch (_) {}
        currentUser = null;
        $("loginForm").reset();
        window.MarketOverviewUI?.stop();
        showLogin();
        $("appContainer").style.display = "none";
        $("adminSection").style.display = "none";
      }

      $("tradeFilter")?.addEventListener("input", (event) => {
        renderTradeTables(allTrades);
      });

      $("mobileMenu").addEventListener("click", () => {
        const sidebar = document.querySelector("aside");
        const open = sidebar.classList.toggle("mobile-open");
        $("mobileMenu").setAttribute("aria-expanded", String(open));
        $("mobileMenu").innerHTML = `<i class="fas fa-${open ? "xmark" : "bars"}"></i>`;
      });

      $("saveProfileButton").addEventListener("click", () => {
        const email = $("profileEmail").value.trim();
        if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
          notify("Enter a valid email address.", true);
          return;
        }
        localStorage.setItem(`arbicore-email-${currentUser.username}`, email);
        notify("Profile preferences saved on this device.");
      });

      $("notificationPreference").addEventListener("change", async (event) => {
        try {
          await api("/api/account/preferences", {method: "POST",
            body: JSON.stringify({notifications: event.target.value})});
          notify("Notification preference saved.");
        } catch (error) { notify(error.message, true); }
      });

      $("runtimeSettingsForm").addEventListener("input", () => { runtimeSettingsDirty = true; });
      $("runtimeSettingsForm").addEventListener("change", () => { runtimeSettingsDirty = true; });
      $("runtimeSettingsForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = event.submitter;
        const config = {
          interval: Number($("settingsScanInterval").value),
          min_profit: Number($("settingsMinProfit").value),
          max_slippage: Number($("settingsMaxSlippage").value),
          max_orders_per_minute: Number($("settingsOrdersMinute").value),
          max_trades_per_hour: Number($("settingsTradesHour").value),
          intelligence_enabled: $("settingsIntelligence").value === "enabled",
          min_model_confidence: Number($("settingsModelConfidence").value) / 100,
        };
        if (![config.interval, config.min_profit, config.max_slippage,
              config.max_orders_per_minute, config.max_trades_per_hour,
              config.min_model_confidence].every(Number.isFinite)) {
          notify("Every trading control must be a valid number.", true);
          return;
        }
        button.disabled = true;
        try {
          await api("/api/config", { method: "POST", body: JSON.stringify(config) });
          runtimeSettingsDirty = false;
          notify("Trading controls saved. They apply from the next scan.");
          await refreshState();
        } catch (error) {
          notify(error.message, true);
        } finally {
          button.disabled = false;
        }
      });

      $("reviewSafetySetup").addEventListener("click", async () => {
        await checkOnboarding();
        $("onboardingModal").classList.add("active");
      });

      $("passwordChangeForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = event.submitter;
        button.disabled = true;
        try {
          const result = await api("/api/account/password", {
            method: "POST",
            body: JSON.stringify({
              current_password: $("currentPassword").value,
              new_password: $("newPassword").value,
            }),
          });
          event.target.reset();
          notify(result.message);
          currentUser = null;
          clearInterval(refreshTimer);
          $("appContainer").style.display = "none";
          showLogin();
        } catch (error) {
          notify(error.message, true);
        } finally {
          button.disabled = false;
        }
      });

      $("enableMfa").addEventListener("click", async () => {
        try {
          const setup = await api("/api/account/mfa/setup", { method: "POST", body: "{}" });
          const code = window.prompt(
            `Add this secret to your authenticator app:\n\n${setup.secret}\n\nThen enter the 6-digit code:`
          );
          if (!code) return;
          const confirmed = await api("/api/account/mfa/confirm", {
            method: "POST", body: JSON.stringify({ code: code.trim() }),
          });
          window.alert(`MFA enabled. Save these recovery codes securely:\n\n${confirmed.recovery_codes.join("\n")}`);
          notify("Authenticator MFA enabled.");
          await refreshAccountSecurity();
        } catch (error) { notify(error.message, true); }
      });

      async function refreshAccountSecurity() {
        try {
          const status = await api("/api/account/security");
          $("enableMfa").style.display = status.mfa_enabled ? "none" : "block";
          $("disableMfa").style.display = status.mfa_enabled ? "block" : "none";
          $("accountSecurityStatus").textContent = status.mfa_enabled
            ? `MFA enabled · ${Number(status.recovery_codes_remaining || 0)} recovery codes remaining`
            : "Authenticator MFA is not enabled.";
        } catch (error) {
          $("accountSecurityStatus").textContent = `Security status unavailable: ${error.message}`;
        }
      }

      $("disableMfa").addEventListener("click", async () => {
        const password = window.prompt("Enter your current ArbiCore password:");
        if (!password) return;
        const code = window.prompt("Enter the current authenticator code or a recovery code:");
        if (!code) return;
        if (!window.confirm("Disable MFA and sign out your other devices?")) return;
        try {
          const result = await api("/api/account/mfa/disable", {
            method: "POST", body: JSON.stringify({ password, code: code.trim() }),
          });
          notify(result.message);
          await refreshAccountSecurity();
        } catch (error) { notify(error.message, true); }
      });

      $("revokeOtherSessions").addEventListener("click", async () => {
        try {
          const result = await api("/api/account/sessions/revoke-others", { method: "POST", body: "{}" });
          notify(`${Number(result.revoked || 0)} other session(s) signed out.`);
        } catch (error) { notify(error.message, true); }
      });

      async function refreshCredentialStatus() {
        try {
          const result = await api("/api/user/api-keys");
          credentialStatuses = result.exchanges || [];
          const select = $("credentialExchange");
          const selected = select.value;
          select.innerHTML = credentialStatuses.map((item) =>
            `<option value="${escapeHtml(item.exchange)}">${escapeHtml(item.label || exchangeLabel(item.exchange))}</option>`).join("");
          if (credentialStatuses.some((item) => item.exchange === selected)) select.value = selected;
          $("exchangeCredentialList").innerHTML = credentialStatuses.map((item) =>
            `<div><i class="fas fa-${item.configured ? "circle-check" : "circle-xmark"}"></i> `
            + `${escapeHtml(item.label || exchangeLabel(item.exchange))}: `
            + `<span class="${item.configured ? "text-success" : "text-muted"}">${item.configured ? `connected ${escapeHtml(item.api_key || "")}` : "not connected"}</span>`
            + `${item.active ? " · selected for trading" : ""}</div>`).join("");
          syncCredentialForm();
        } catch (error) {
          $("exchangeCredentialStatus").textContent = error.message;
        }
      }

      function syncCredentialForm() {
        const exchange = $("credentialExchange").value;
        const status = credentialStatuses.find((item) => item.exchange === exchange) || {
          exchange,
          label: exchangeLabel(exchange),
          configured: false,
          requires_password: ["kucoin", "okx"].includes(exchange),
          available: true,
        };
        const label = status.label || exchangeLabel(exchange);
        $("credentialApiKeyLabel").textContent = `${label} API key`;
        $("credentialApiSecretLabel").textContent = `${label} secret key`;
        $("credentialPassphraseGroup").style.display = status.requires_password ? "block" : "none";
        $("credentialPassphrase").required = Boolean(status.requires_password);
        if (!status.requires_password) $("credentialPassphrase").value = "";
        $("exchangeCredentialStatus").innerHTML = status.configured
          ? `<span class="text-success"><i class="fas fa-circle-check"></i> Connected ${escapeHtml(status.api_key || "")}</span> · ${escapeHtml(status.source || "server")}`
          : `<span class="text-muted"><i class="fas fa-circle-xmark"></i> ${escapeHtml(label)} is not connected.</span>`;
        $("saveExchangeCredentials").innerHTML = `<i class="fas fa-link"></i> Connect ${escapeHtml(label)}`;
        $("saveExchangeCredentials").disabled = status.available === false;
        $("disconnectExchange").disabled = !status.configured || status.source === "env" || status.available === false;
      }

      $("credentialExchange").addEventListener("change", () => {
        $("credentialApiKey").value = "";
        $("credentialApiSecret").value = "";
        $("credentialPassphrase").value = "";
        syncCredentialForm();
      });

      $("exchangeCredentialForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = $("saveExchangeCredentials");
        button.disabled = true;
        try {
          const exchange = $("credentialExchange").value;
          const result = await api("/api/user/api-keys", {
            method: "POST",
            body: JSON.stringify({
              exchange,
              api_key: $("credentialApiKey").value.trim(),
              api_secret: $("credentialApiSecret").value.trim(),
              password: $("credentialPassphrase").value.trim(),
            }),
          });
          $("credentialApiKey").value = "";
          $("credentialApiSecret").value = "";
          $("credentialPassphrase").value = "";
          notify(result.message);
          await Promise.all([refreshCredentialStatus(), refreshState()]);
        } catch (error) {
          notify(error.message, true);
        } finally {
          syncCredentialForm();
        }
      });

      $("disconnectExchange").addEventListener("click", async () => {
        const exchange = $("credentialExchange").value;
        const label = exchangeLabel(exchange);
        if (!window.confirm(`Erase the ${label} credentials currently held by this server?`)) return;
        try {
          const result = await api(`/api/user/api-keys/${encodeURIComponent(exchange)}`, { method: "DELETE" });
          notify(result.message);
          await Promise.all([refreshCredentialStatus(), refreshState()]);
        } catch (error) { notify(error.message, true); }
      });

      async function restoreSession() {
        try {
          const savedState = await api("/api/state");
          if (savedState.current_user) {
            currentUser = savedState.current_user;
            engineState = savedState;
            showApp();
            await refreshAll();
            await checkOnboarding();
            clearInterval(refreshTimer);
            refreshTimer = setInterval(refreshAll, 2500);
            return;
          }
        } catch (error) {
          console.warn("Session restoration failed", error);
        }
        $("appContainer").style.display = "none";
        showRegister();
      }

      async function handlePasswordReset() {
        const url = new URL(window.location.href);
        const token = url.searchParams.get("reset_token");
        if (!token) return;
        history.replaceState({}, document.title, url.pathname);
        const password = window.prompt("Enter a new ArbiCore password (at least 12 characters):");
        if (!password) return;
        try {
          const result = await api("/api/auth/password-reset/confirm", {
            method: "POST", body: JSON.stringify({ token, new_password: password }),
          });
          window.alert(result.message);
        } catch (error) { window.alert(`Password reset failed: ${error.message}`); }
      }

      handlePasswordReset().then(restoreSession);
