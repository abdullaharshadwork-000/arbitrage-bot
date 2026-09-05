"""Role and dashboard API regressions for the professional UI."""

import unittest
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet

import server
import arbitrage_bot as bot
from arbicore import auth
from arbicore.vault import CredentialVault


class TestDashboardRoles(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.saved_db_file = server.DB_FILE
        server.DB_FILE = Path(self.tempdir.name) / "fresh.db"
        server.initialize_database()
        self.client = server.app.test_client()
        response = self.client.get("/")  # obtains the local mutation-guard cookie
        response.close()
        with server.state_lock:
            server.state["current_user"] = None
        server.session_users.clear()
        server.request_windows.clear()
        self.saved_wallet = server.wallet
        self.saved_vault = server.credential_vault
        with server.state_lock:
            self.saved_state = {
                key: server.state[key]
                for key in ("trades", "trades_count", "total_profit", "owner_user_id")
            }
            server.state["owner_user_id"] = None
            self.saved_execution_mode = server.state["config"]["execution_mode"]
            self.saved_interval = server.state["config"]["interval"]

    def tearDown(self):
        server.wallet = self.saved_wallet
        server.credential_vault = self.saved_vault
        with server.state_lock:
            server.state.update(self.saved_state)
            server.state["config"]["execution_mode"] = self.saved_execution_mode
            server.state["config"]["interval"] = self.saved_interval
        server.DB_FILE = self.saved_db_file
        self.tempdir.cleanup()

    def login(self, role="trader", username="operator"):
        password = server.DEFAULT_ADMIN_PASSWORD if username == server.DEFAULT_ADMIN_USERNAME else "local-demo-password"
        if username != server.DEFAULT_ADMIN_USERNAME:
            self.client.post("/api/auth/register", json={
                "username": username,
                "email": f"{username}@localhost",
                "password": password,
            })
        return self.client.post("/api/auth/login", json={
            "username": username,
            "password": password,
        })

    def test_root_serves_professional_dashboard(self):
        response = self.client.get("/")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="appContainer"', response.data)
            self.assertIn(b'id="themeToggle"', response.data)
            self.assertNotIn(b"Admin@12345", response.data)
            self.assertIn(b'src="/dashboard-pro.js"', response.data)
            self.assertIn("script-src 'self';", response.headers["Content-Security-Policy"])
            self.assertNotIn("script-src 'self' 'unsafe-inline'", response.headers["Content-Security-Policy"])
        finally:
            response.close()

    def test_dashboard_javascript_is_served_as_a_non_inline_asset(self):
        response = self.client.get("/dashboard-pro.js")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"function refreshAll", response.data)
        finally:
            response.close()

    def test_backend_source_files_are_not_public(self):
        self.assertEqual(self.client.get("/server.py").status_code, 404)
        self.assertEqual(self.client.get("/arbicore.db").status_code, 404)

    def test_obsolete_dashboard_redirects_to_canonical_ui(self):
        response = self.client.get("/dashboard.html")
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers["Location"], "/")

    def test_unknown_credentials_are_rejected(self):
        response = self.client.post("/api/auth/login", json={
            "username": "missing", "password": "incorrect-password"})
        self.assertEqual(response.status_code, 401)

    def test_sustained_failed_logins_are_temporarily_locked(self):
        with server.db() as connection:
            for _ in range(10):
                connection.execute(
                    "INSERT INTO auth_login_attempts "
                    "(attempted_at, username, ip_address, successful) VALUES (?, ?, ?, 0)",
                    (time.time(), "operator", "127.0.0.1"),
                )
        response = self.client.post("/api/auth/login", json={
            "username": "operator", "password": "incorrect-password"})
        self.assertEqual(response.status_code, 429)

    def test_mfa_can_only_be_disabled_with_password_and_current_code(self):
        self.assertEqual(self.login().status_code, 200)
        with self.client.session_transaction() as browser_session:
            user_id = browser_session["user_id"]
        server.credential_vault = CredentialVault(Fernet.generate_key().decode("ascii"))
        secret = "JBSWY3DPEHPK3PXP"
        with server.db() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO user_security "
                "(user_id, totp_secret, totp_enabled, recovery_hashes, updated_at) "
                "VALUES (?, ?, 1, '[]', '2026-01-01T00:00:00')",
                (user_id, server.credential_vault.encrypt_text(secret)),
            )
        rejected = self.client.post("/api/account/mfa/disable", json={
            "password": "wrong-password", "code": auth.totp(secret)})
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post("/api/account/mfa/disable", json={
            "password": "local-demo-password", "code": auth.totp(secret)})
        self.assertEqual(accepted.status_code, 200)
        self.assertFalse(self.client.get("/api/account/security").get_json()["mfa_enabled"])

    def test_password_reset_token_is_one_time_and_revokes_old_password(self):
        self.login()
        with self.client.session_transaction() as browser_session:
            user_id = browser_session["user_id"]
        token = "one-time-reset-token"
        with server.db() as connection:
            connection.execute(
                "INSERT INTO password_reset_tokens "
                "(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (auth.hash_token(token), user_id, "2026-01-01T00:00:00", time.time() + 300),
            )
        reset = self.client.post("/api/auth/password-reset/confirm", json={
            "token": token, "new_password": "replacement-password"})
        self.assertEqual(reset.status_code, 200)
        reused = self.client.post("/api/auth/password-reset/confirm", json={
            "token": token, "new_password": "another-password"})
        self.assertEqual(reused.status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "operator", "password": "replacement-password"}).status_code, 200)

    def test_trader_cannot_read_admin_endpoints(self):
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/stats").status_code, 403)

    def test_trader_can_change_scan_interval(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.post("/api/config", json={"interval": 2.5})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(server.state["config"]["interval"], 2.5)

    def test_unsafe_scan_interval_is_clamped_to_workload_floor(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.post("/api/config", json={"interval": 0.01})
        self.assertEqual(response.status_code, 200, response.get_json())
        minimum = server.recommended_scan_interval()
        self.assertEqual(server.state["config"]["interval"], minimum)
        self.assertEqual(response.get_json()["safe_adjustments"]["interval"], minimum)

    def test_execution_quality_is_scoped_and_backend_driven(self):
        self.login()
        with self.client.session_transaction() as browser_session:
            user_id = browser_session["user_id"]
        with server.db() as connection:
            connection.execute(
                "INSERT INTO trades (time, symbol, profit_usdt, expected_profit_usdt, "
                "realized_slippage_usdt, status, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-01-01T00:00:00", "BTC/USDT", 0.8, 1.0, 0.2, "filled", user_id),
            )
            connection.execute(
                "INSERT INTO order_intents (client_order_id, user_id, exchange, symbol, side, "
                "quantity, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("quality-1", user_id, "binance", "BTC/USDT", "buy", "0.001",
                 "rejected", "2026-01-01T00:00:00", "2026-01-01T00:00:01"),
            )
        quality = self.client.get("/api/execution-quality").get_json()["quality"]
        self.assertEqual(quality["trades"], 1)
        self.assertEqual(quality["realized_profit_usdt"], 0.8)
        self.assertEqual(quality["rejected_orders"], 1)

    def test_revoke_other_sessions_preserves_current_session(self):
        self.login()
        with self.client.session_transaction() as browser_session:
            user_id = browser_session["user_id"]
            current = browser_session["session_id"]
        with server.db() as connection:
            connection.execute(
                "INSERT INTO auth_sessions (session_id, user_id, created_at, expires_at) "
                "VALUES ('other-session', ?, '2026-01-01T00:00:00', 9999999999)",
                (user_id,),
            )
        result = self.client.post("/api/account/sessions/revoke-others").get_json()
        self.assertEqual(result["revoked"], 1)
        with server.db() as connection:
            rows = connection.execute(
                "SELECT session_id, revoked_at FROM auth_sessions WHERE user_id = ?", (user_id,)
            ).fetchall()
        self.assertIsNone(dict(rows)[current])
        self.assertIsNotNone(dict(rows)["other-session"])

    def test_onboarding_requires_both_consents_and_persists_mode(self):
        self.assertEqual(self.login().status_code, 200)
        initial = self.client.get("/api/onboarding").get_json()
        self.assertFalse(initial["completed"])
        rejected = self.client.post("/api/onboarding", json={
            "experience_mode": "beginner", "risk_accepted": True,
            "terms_accepted": False,
        })
        self.assertEqual(rejected.status_code, 400)
        accepted = self.client.post("/api/onboarding", json={
            "experience_mode": "beginner", "risk_accepted": True,
            "terms_accepted": True,
        })
        self.assertEqual(accepted.status_code, 200)
        restored = self.client.get("/api/onboarding").get_json()
        self.assertTrue(restored["completed"])
        self.assertEqual(restored["experience_mode"], "beginner")

    def test_admin_sees_real_session_count(self):
        self.assertEqual(self.login(username="admin").status_code, 200)
        stats = self.client.get("/api/admin/stats").get_json()["stats"]
        users = self.client.get("/api/admin/users").get_json()["users"]
        self.assertEqual(stats["total_users"], 1)
        self.assertEqual([user["username"] for user in users], ["admin"])

    def test_fresh_database_always_seeds_default_admin(self):
        with server.db() as connection:
            user = connection.execute(
                "SELECT username, role, password_hash FROM users WHERE username = 'admin'"
            ).fetchone()
        self.assertEqual(user[:2], ("admin", "admin"))
        self.assertNotIn(server.DEFAULT_ADMIN_PASSWORD, user[2])
        self.assertEqual(self.login(username="admin").status_code, 200)

    def test_user_stats_are_calculated_from_backend_trades(self):
        self.login()
        with self.client.session_transaction() as browser_session:
            user_id = browser_session["user_id"]
        with server.db() as connection:
            for index, profit in enumerate((2.0, -1.0), start=1):
                connection.execute(
                    "INSERT INTO trades (time, symbol, profit_usdt, status, user_id) "
                    "VALUES (?, 'BTC/USDT', ?, 'filled', ?)",
                    (f"2026-01-01T00:00:0{index}", profit, user_id),
                )
        stats = self.client.get("/api/user/stats").get_json()["stats"]
        self.assertEqual(stats["total_trades"], 2)
        self.assertEqual(stats["total_profit"], 1.0)
        self.assertEqual(stats["win_rate"], 50.0)

    def test_state_publishes_real_paper_wallet_rows(self):
        self.login()
        server.wallet = bot.PaperWallet(
            ["binance"], ["BTC/USDT"], 1000.0, {"BTC/USDT": 100000.0})
        with server.state_lock:
            server.state["config"]["execution_mode"] = "paper"
        balances = self.client.get("/api/state").get_json()["balances"]
        row = balances["binance"]["BTC/USDT"]
        self.assertEqual(row["source"], "paper")
        self.assertEqual(row["cash_usdt"], 500.0)
        state = self.client.get("/api/state").get_json()
        self.assertIn("BTC/USDT", state["available_symbols"])
        self.assertIn("binance", state["available_exchanges"])


if __name__ == "__main__":
    unittest.main()
