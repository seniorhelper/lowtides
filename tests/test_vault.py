"""PayPal on file (Vault v3 payment tokens): connect, revoke from the kill switch and
STOP ALL, purchases refused after revocation, simulated mode, and honest failure reports."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.parse

import fakes
from helpers import agent
from test_http import ServerCase, http

from lowtides.paypal import PayPal
from lowtides.spend import SpendGovernor
from lowtides.store import Store
from lowtides.vault import REVOKED_LINE, Vault, VaultError


def buy(amount, merchant="Acme Data", **kw):
    d = {"amount": amount, "merchant": merchant, "item": "credits", "category": "data", "reason": "finish the lead list"}
    d.update(kw)
    return d


def deletes(state):
    return [r for r in state["requests"] if r[0].startswith("DELETE /v3/vault/payment-tokens/")]


class TestVaultReal(unittest.TestCase):
    """The real PayPal client against the fake PayPal server."""

    def setUp(self):
        self.pp, url = fakes.start(fakes.FakePayPalHandler, fakes.paypal_state())
        self.addCleanup(self.pp.shutdown)
        self.s = Store(":memory:")
        self.paypal = PayPal("client", "secret", base_url=url)
        self.v = Vault(self.s, self.paypal, base_url="http://lt.example")
        self.v.retry_delay = 0
        self.g = SpendGovernor(self.s, self.paypal, vault=self.v)
        self.g.poll_delays, self.g.background_poll = (0,), ()
        self.a = agent(self.s, "Buyer")
        self.s.add_merchant("Acme Data", "merchant-demo@example.com", "data")

    def connect(self):
        start = self.v.connect_start(self.a["id"])
        fakes.approve_vault(self.pp.state, start["setup_token_id"])
        return start, self.v.connect_finish(start["setup_token_id"])

    def test_connect_sends_a_merchant_setup_token_and_saves_the_payment_token(self):
        start = self.v.connect_start(self.a["id"])
        self.assertFalse(start["simulated"])
        self.assertIn("agreements/approve", start["approve_url"])
        sent = [r for r in self.pp.state["requests"] if r[0] == "/v3/vault/setup-tokens"][0]
        pp = sent[2]["payment_source"]["paypal"]
        self.assertEqual(pp["usage_type"], "MERCHANT")
        self.assertEqual(pp["experience_context"]["return_url"], "http://lt.example/paypal/return?kind=vault")
        self.assertTrue({k.lower(): v for k, v in sent[1].items()}.get("paypal-request-id"))
        self.assertEqual(self.v.status(self.a["id"])["status"], "waiting_for_paypal")
        fakes.approve_vault(self.pp.state, start["setup_token_id"])
        done = self.v.connect_finish(start["setup_token_id"])
        self.assertEqual((done["status"], done["payer_email"]), ("active", "buyer@example.com"))
        st = self.v.status(self.a["id"])
        self.assertEqual((st["status"], st["label"], st["simulated"], st["env"]), ("active", "connected", False, "sandbox"))
        self.assertTrue(st["token_id"].startswith("PTOK"))
        # A second return from PayPal is harmless.
        self.assertTrue(self.v.connect_finish(start["setup_token_id"])["already"])
        self.assertEqual(len([r for r in self.pp.state["requests"] if r[0] == "/v3/vault/payment-tokens"]), 1)
        self.assertIn("vault.connected", [e["kind"] for e in self.s.audit_entries()])

    def test_unapproved_setup_token_fails_honestly(self):
        start = self.v.connect_start(self.a["id"])
        with self.assertRaises(VaultError) as cm:
            self.v.connect_finish(start["setup_token_id"])
        self.assertIn("SETUP_TOKEN_NOT_APPROVED", str(cm.exception))
        self.assertEqual(self.v.status(self.a["id"])["status"], "none")
        self.assertIsNone(self.v.purchase_block(self.a["id"]))  # never connected: not required

    def test_kill_deletes_the_token_at_paypal(self):
        _, done = self.connect()
        self.assertEqual(self.g.request(self.a, buy(5))["status"], "paid")
        out = self.g.kill(self.a["id"])
        self.assertEqual(out["tokens_revoked"], 1)
        self.assertEqual(out["paypal_on_file"]["status"], "revoked")
        self.assertEqual(out["paypal_on_file"]["message"], REVOKED_LINE)
        self.assertTrue(self.pp.state["payment_tokens"][done["token_id"]]["deleted"])
        self.assertEqual(deletes(self.pp.state)[0][0], "DELETE /v3/vault/payment-tokens/" + done["token_id"])
        self.assertEqual(self.v.status(self.a["id"])["status"], "revoked")
        msgs = [e["message"] for e in self.s.events(20)]
        self.assertTrue(any(REVOKED_LINE in m for m in msgs), msgs)
        self.assertTrue(any("Saved PayPal payment method revoked" in m for m in msgs), msgs)
        kinds = [e["kind"] for e in self.s.audit_entries()]
        self.assertIn("vault.revoked", kinds)
        killed = [json.loads(e["data"]) for e in self.s.audit_entries() if e["kind"] == "agent.killed"][0]
        self.assertEqual(killed["paypal_on_file"]["status"], "revoked")
        self.assertTrue(self.s.audit_verify()["ok"])

    def test_purchase_after_revoke_is_denied_even_when_restarted(self):
        self.connect()
        self.g.kill(self.a["id"])
        self.s.update_agent(self.a["id"], status="on")  # owner presses Start but doesn't reconnect
        r = self.g.request(self.s.agent(self.a["id"]), buy(5))
        self.assertEqual(r["decision"], "denied")
        self.assertIn("revoked", r["reasons"][0])
        self.assertIn("no_paypal_on_file", r["flags"])
        # Reconnecting makes it usable again.
        self.connect()
        self.assertEqual(self.g.request(self.s.agent(self.a["id"]), buy(6))["status"], "paid")

    def test_approve_refused_after_revoke(self):
        self.connect()
        held = self.g.request(self.a, buy(40))
        self.assertEqual(held["status"], "pending")
        self.v.revoke(self.a["id"])
        from lowtides.spend import SpendError
        with self.assertRaises(SpendError) as cm:
            self.g.approve(held["id"])
        self.assertIn("revoked", str(cm.exception))

    def test_revoke_failure_is_reported_and_retried(self):
        _, done = self.connect()
        self.pp.state["vault_delete_error"] = (500, {"name": "INTERNAL_SERVER_ERROR", "message": "An internal server error occurred."})
        out = self.g.kill(self.a["id"])
        self.assertEqual(out["tokens_revoked"], 0)
        self.assertEqual(out["paypal_on_file"]["status"], "revoke_failed")
        self.assertTrue(any("Couldn't revoke the saved PayPal payment method" in p for p in out["problems"]), out)
        self.assertIn("INTERNAL_SERVER_ERROR", out["paypal_on_file"]["error"])
        self.assertEqual(len(deletes(self.pp.state)), 2)  # a 5xx is retried once
        self.assertFalse(self.pp.state["payment_tokens"][done["token_id"]]["deleted"])
        self.assertEqual(self.v.status(self.a["id"])["status"], "revoke_failed")
        self.assertIn("vault.revoke_failed", [e["kind"] for e in self.s.audit_entries()])
        # Purchases stay refused while the token may still be live.
        self.s.update_agent(self.a["id"], status="on")
        r = self.g.request(self.s.agent(self.a["id"]), buy(5))
        self.assertEqual(r["decision"], "denied")
        self.assertIn("didn't go through at PayPal", r["reasons"][0])
        # The next try (a second KILL or the Revoke button) succeeds once PayPal recovers.
        self.pp.state["vault_delete_error"] = None
        again = self.v.revoke(self.a["id"], by="owner")
        self.assertEqual(again["status"], "revoked")
        self.assertTrue(self.pp.state["payment_tokens"][done["token_id"]]["deleted"])

    def test_client_error_is_not_retried(self):
        self.connect()
        self.pp.state["vault_delete_error"] = (403, {"name": "NOT_AUTHORIZED", "message": "Authorization failed due to insufficient permissions."})
        out = self.v.revoke(self.a["id"])
        self.assertEqual(out["status"], "revoke_failed")
        self.assertEqual(len(deletes(self.pp.state)), 1)

    def test_token_already_gone_at_paypal_counts_as_revoked(self):
        _, done = self.connect()
        self.pp.state["payment_tokens"][done["token_id"]]["deleted"] = True  # payer removed it in PayPal
        out = self.v.revoke(self.a["id"])
        self.assertEqual(out["status"], "revoked")
        self.assertEqual(self.s.vault_current(self.a["id"])["note"], "PayPal had already removed it.")

    def test_kill_without_token_still_works(self):
        out = self.g.kill(self.a["id"])
        self.assertEqual((out["tokens_revoked"], out["paypal_on_file"]["status"]), (0, "none"))
        self.assertEqual(deletes(self.pp.state), [])

    def test_reconnect_deletes_the_old_token(self):
        _, first = self.connect()
        _, second = self.connect()
        self.assertTrue(self.pp.state["payment_tokens"][first["token_id"]]["deleted"])
        self.assertFalse(self.pp.state["payment_tokens"][second["token_id"]]["deleted"])
        self.assertEqual(self.v.status(self.a["id"])["token_id"], second["token_id"])

    def test_topup_charges_the_vaulted_token(self):
        _, done = self.connect()
        out = self.v.topup(self.a["id"], 10)
        self.assertEqual((out["status"], out["balance"]), ("COMPLETED", 10.0))
        sent = [r for r in self.pp.state["requests"] if r[0] == "/v2/checkout/orders"][-1][2]
        self.assertEqual(sent["payment_source"], {"paypal": {"vault_id": done["token_id"]}})
        self.assertEqual(sent["intent"], "CAPTURE")
        self.v.revoke(self.a["id"])
        with self.assertRaises(VaultError):
            self.v.topup(self.a["id"], 10)


class TestVaultSimulated(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.g = SpendGovernor(self.s, PayPal("", ""))
        self.a = agent(self.s, "Sim")
        self.s.add_merchant("Acme Data", "", "data")

    def test_simulated_connect_kill_and_deny(self):
        out = self.g.vault.connect_start(self.a["id"])
        self.assertTrue(out["simulated"])
        self.assertIsNone(out["approve_url"])
        st = self.g.vault.status(self.a["id"])
        self.assertEqual((st["status"], st["simulated"]), ("active", True))
        self.assertTrue(st["token_id"].startswith("SIM-VAULT-"))
        self.assertTrue(any("simulated" in e["message"] for e in self.s.events(5)))
        k = self.g.kill(self.a["id"])
        self.assertEqual((k["tokens_revoked"], k["paypal_on_file"]["simulated"]), (1, True))
        self.assertIn("Simulated", k["paypal_on_file"]["message"])
        self.s.update_agent(self.a["id"], status="on")
        self.assertEqual(self.g.request(self.s.agent(self.a["id"]), buy(3))["decision"], "denied")

    def test_simulated_topup_is_labeled(self):
        self.g.vault.connect_start(self.a["id"])
        out = self.g.vault.topup(self.a["id"], 5)
        self.assertTrue(out["simulated"])
        self.assertEqual(self.s.balance(self.a["id"]), 5)

    def test_existing_database_gets_the_table(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE, "
                   "daily_budget REAL NOT NULL, task_budget REAL NOT NULL, status TEXT NOT NULL DEFAULT 'on', client_id INTEGER, "
                   "use_wallet INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)")
        db.execute("INSERT INTO agents(name,key,daily_budget,task_budget,created) VALUES('Old','lt-old',5,1,?)", (time.time(),))
        db.commit()
        db.close()
        s = Store(path)
        v = Vault(s, PayPal("", ""))
        self.assertEqual(v.status(1)["status"], "none")
        v.connect_start(1)
        self.assertEqual(v.status(1)["status"], "active")
        Store(path)  # opening again is safe
        self.assertEqual(Vault(Store(path), PayPal("", "")).status(1)["status"], "active")


class TestVaultHTTP(ServerCase):
    def setUp(self):
        self.pp.state.update(fakes.paypal_state())
        self.app.vault.retry_delay = 0
        self.app.spend.poll_delays, self.app.spend.background_poll = (0,), ()
        s, r, _ = self.api("POST", "/api/agents", {"name": "Vaulted %f" % time.time(), "daily_budget": 5})
        self.agent = r["agent"]
        self.h = {"Authorization": "Bearer " + self.agent["key"]}
        self.api("POST", "/api/merchants", {"name": "Acme Data", "paypal_email": "acme@example.com", "category": "data"})

    def connect(self):
        aid = self.agent["id"]
        s, start, _ = self.api("POST", "/api/agents/%d/vault/connect" % aid, {})
        self.assertEqual(s, 200, start)
        sid = urllib.parse.parse_qs(urllib.parse.urlparse(start["approve_url"]).query)["approval_session_id"][0]
        fakes.approve_vault(self.pp.state, sid)
        # PayPal sends the payer back to the return URL with the approved setup token.
        s, _, _ = http("GET", self.url + "/paypal/return?kind=vault&approval_token_id=%s&approval_session_id=%s" % (sid, sid))
        self.assertEqual(s, 200)
        s, st, _ = self.api("GET", "/api/agents/%d/vault" % aid)
        self.assertEqual(st["status"], "active", st)
        return st

    def test_connect_through_the_return_url_and_state_shows_it(self):
        st = self.connect()
        s, state, _ = self.api("GET", "/api/state")
        mine = [a for a in state["agents"] if a["id"] == self.agent["id"]][0]
        self.assertEqual(mine["paypal_on_file"]["label"], "connected")
        self.assertEqual(mine["paypal_on_file"]["token_id"], st["token_id"])
        self.assertFalse(state["vault_simulated"])

    def test_return_with_token_param_and_unknown_token(self):
        aid = self.agent["id"]
        s, start, _ = self.api("POST", "/api/agents/%d/vault/connect" % aid, {})
        fakes.approve_vault(self.pp.state, start["setup_token_id"])
        http("GET", self.url + "/paypal/return?kind=vault&token=" + start["setup_token_id"])
        self.assertEqual(self.app.vault.status(aid)["status"], "active")
        req = urllib.request.Request(self.url + "/paypal/return?kind=vault&token=NOPE")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(req)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            self.assertIn("#error?msg=", e.headers["Location"])

    def test_kill_route_revokes_and_purchase_is_denied(self):
        st = self.connect()
        s, out, _ = self.api("POST", "/api/agents/%d/kill" % self.agent["id"], {})
        self.assertEqual((out["tokens_revoked"], out["paypal_on_file"]["status"]), (1, "revoked"))
        self.assertTrue(self.pp.state["payment_tokens"][st["token_id"]]["deleted"])
        self.api("POST", "/api/agents/%d/start" % self.agent["id"], {})
        s, r, _ = http("POST", self.url + "/v1/spend", buy(5), self.h)
        self.assertEqual(r["decision"], "denied")
        self.assertIn("Reconnect PayPal", r["reasons"][0])

    def test_stop_all_revokes_every_agent_and_reports_failures(self):
        st = self.connect()
        s, r, _ = self.api("POST", "/api/agents", {"name": "Second %f" % time.time(), "daily_budget": 5})
        second = r["agent"]
        s, start, _ = self.api("POST", "/api/agents/%d/vault/connect" % second["id"], {})
        fakes.approve_vault(self.pp.state, start["setup_token_id"])
        self.app.vault.connect_finish(start["setup_token_id"])
        second_tok = self.app.vault.status(second["id"])["token_id"]
        # PayPal refuses to delete the first agent's token: that one fails, the other is still revoked.
        self.pp.state["vault_delete_error"] = (403, {"name": "NOT_AUTHORIZED", "message": "Authorization failed."})
        self.pp.state["vault_delete_error_ids"] = {st["token_id"]}
        s, out, _ = self.api("POST", "/api/stop-all", {})
        self.assertEqual(s, 200)
        self.assertGreaterEqual(out["tokens_revoked"], 1, out)  # other tests' agents share this server
        self.assertEqual(out["revoke_failed"], 1, out)
        self.assertEqual(self.app.vault.status(self.agent["id"])["status"], "revoke_failed")
        self.assertEqual(self.app.vault.status(second["id"])["status"], "revoked")
        self.assertTrue(any("Couldn't revoke" in p for p in out["problems"]), out)
        self.assertFalse(self.pp.state["payment_tokens"][st["token_id"]]["deleted"])
        self.assertTrue(self.pp.state["payment_tokens"][second_tok]["deleted"])
        s, state, _ = self.api("GET", "/api/state")
        self.assertTrue(any("revoked (1 could not be revoked)" in e["message"] for e in state["events"]))

    def test_revoke_button_and_topup_routes(self):
        aid = self.agent["id"]
        s, r, _ = self.api("POST", "/api/agents/%d/vault/revoke" % aid, {})
        self.assertEqual(s, 409)
        self.connect()
        s, r, _ = self.api("POST", "/api/agents/%d/vault/topup" % aid, {"amount": 7})
        self.assertEqual((s, r["status"], r["balance"]), (200, "COMPLETED", 7.0))
        s, r, _ = self.api("POST", "/api/agents/%d/vault/revoke" % aid, {})
        self.assertEqual((s, r["status"], r["paypal_on_file"]["label"]), (200, "revoked", "revoked"))
        s, r, _ = self.api("POST", "/api/agents/%d/vault/topup" % aid, {"amount": 7})
        self.assertEqual(s, 409)
        s, r, _ = self.api("POST", "/api/agents/999999/vault/connect", {})
        self.assertEqual(s, 404)

    def test_vault_routes_refuse_cross_site(self):
        s, r, _ = http("POST", self.url + "/api/agents/%d/vault/revoke" % self.agent["id"], {}, {"Origin": "https://evil.example"})
        self.assertEqual(s, 403)


import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


if __name__ == "__main__":
    unittest.main()
