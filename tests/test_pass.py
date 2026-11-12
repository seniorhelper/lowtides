"""Lowtides Pass: Ed25519 (RFC 8032 vectors), issuing, verifying, tampering, expiry,
revocation (owner, Stop all, STOP, loop guard, rules change), would_allow vs the
real spend decision, and the HTTP endpoints and /pass/ page."""
import json
import os
import time
import unittest

from helpers import agent
from test_http import ServerCase, http

from lowtides import ed25519, mcp
from lowtides.passport import ALG, TYP, PassBook, PassError, b64d, b64e
from lowtides.paypal import PayPal
from lowtides.spend import SpendGovernor
from lowtides.store import Store

# RFC 8032, section 7.1, TEST 1-3: (secret key, public key, message, signature)
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]

ISS = "https://lowtides.test"


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for sk, pk, msg, sig in RFC8032:
            sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
            self.assertEqual(ed25519.public_key(sk), pk)
            self.assertEqual(ed25519.sign(sk, msg), sig)
            self.assertTrue(ed25519.verify(pk, msg, sig))

    def test_rejects_wrong_message_key_or_signature(self):
        sk, pk, msg, sig = (bytes.fromhex(x) for x in RFC8032[2])
        self.assertFalse(ed25519.verify(pk, msg + b"\x00", sig))
        self.assertFalse(ed25519.verify(bytes.fromhex(RFC8032[0][1]), msg, sig))
        bad = bytearray(sig)
        bad[5] ^= 1
        self.assertFalse(ed25519.verify(pk, msg, bytes(bad)))
        # S >= L must be refused (signature malleability).
        s = int.from_bytes(sig[32:], "little") + ed25519.L
        self.assertFalse(ed25519.verify(pk, msg, sig[:32] + s.to_bytes(32, "little")))
        self.assertFalse(ed25519.verify(pk, msg, b"short"))
        self.assertFalse(ed25519.verify(b"\xff" * 32, msg, sig))

    def test_random_roundtrip(self):
        for n in (0, 1, 33, 500):
            seed = ed25519.new_seed()
            msg = os.urandom(n)
            self.assertTrue(ed25519.verify(ed25519.public_key(seed), msg, ed25519.sign(seed, msg)))


def setup(paypal=None):
    store = Store(":memory:")
    paypal = paypal or PayPal("", "")
    return store, PassBook(store, paypal), SpendGovernor(store, paypal)


def resign_parts(token, header=None, payload=None, sig=None):
    h, p, s = token.split(".")
    if header is not None:
        h = b64e(json.dumps(header).encode())
    if payload is not None:
        p = b64e(json.dumps(payload).encode())
    if sig is not None:
        s = sig
    return ".".join((h, p, s))


class TestPass(unittest.TestCase):
    def setUp(self):
        self.store, self.book, self.gov = setup()
        self.a = agent(self.store, "Research bot")

    def test_issue_and_verify_roundtrip(self):
        token, p = self.book.issue(self.a, ISS)
        h, body, sig = token.split(".")
        header = json.loads(b64d(h))
        self.assertEqual(header["alg"], ALG)
        self.assertEqual(header["typ"], TYP)
        self.assertEqual(header["kid"], self.book.jwks()["keys"][0]["kid"])
        payload = json.loads(b64d(body))
        self.assertEqual(payload["iss"], ISS)
        self.assertEqual(payload["sub"], "agent:%d" % self.a["id"])
        self.assertEqual(payload["agent"]["name"], "Research bot")
        self.assertAlmostEqual(payload["exp"] - payload["iat"], 24 * 3600, delta=1)
        self.assertTrue(payload["jti"].startswith("pass_"))
        self.assertEqual(payload["limits"]["max_per_purchase"], 50.0)
        self.assertIn("AP2-aligned", payload["ap2"]["aligned_with"])
        self.assertIn("Up to $50.00 per purchase.", payload["ap2"]["natural_language_description"])
        self.assertEqual(payload["ap2"]["constraints"]["max_amount_per_day"], {"currency": "USD", "value": "200.00"})
        self.assertEqual(payload["status_url"], ISS + "/v1/pass/verify")
        v = self.book.check(token)
        self.assertTrue(v["valid"] and v["signature_ok"])
        self.assertFalse(v["revoked"] or v["expired"])
        self.assertEqual(v["agent"]["name"], "Research bot")
        self.assertIsNone(v["would_allow"])  # nothing to check without a purchase
        self.assertIn("Up to $200.00 of purchases per day.", v["limits_plain"])
        # Offline check with only the public key from the JWKS.
        x = b64d(self.book.jwks()["keys"][0]["x"])
        self.assertTrue(ed25519.verify(x, (h + "." + body).encode(), b64d(sig)))

    def test_same_key_across_restarts(self):
        kid = self.book.jwks()["keys"][0]["kid"]
        token, _ = self.book.issue(self.a, ISS)
        again = PassBook(self.store, PayPal("", ""))
        self.assertEqual(again.jwks()["keys"][0]["kid"], kid)
        self.assertTrue(again.check(token)["valid"])

    def test_tampered_payload_is_rejected(self):
        token, p = self.book.issue(self.a, ISS)
        p["limits"]["max_per_purchase"] = 100000
        v = self.book.check(resign_parts(token, payload=p), amount=5000, merchant="Acme")
        self.assertFalse(v["valid"])
        self.assertFalse(v["signature_ok"])
        self.assertEqual(v["would_allow"], "denied")
        self.assertIsNone(v["limits"])
        self.assertIn("signature doesn't match", v["reasons"][0])

    def test_tampered_signature_is_rejected(self):
        token, _ = self.book.issue(self.a, ISS)
        sig = bytearray(b64d(token.split(".")[2]))
        sig[10] ^= 0x40
        self.assertFalse(self.book.check(resign_parts(token, sig=b64e(bytes(sig))))["signature_ok"])

    def test_alg_none_and_unknown_kid_are_rejected(self):
        token, _ = self.book.issue(self.a, ISS)
        header = json.loads(b64d(token.split(".")[0]))
        v = self.book.check(resign_parts(token, header=dict(header, alg="none"), sig=""))
        self.assertFalse(v["signature_ok"] or v["valid"])
        self.assertIn("EdDSA", v["reasons"][0])
        v = self.book.check(resign_parts(token, header=dict(header, alg="HS256")))
        self.assertFalse(v["valid"])
        v = self.book.check(resign_parts(token, header=dict(header, kid="lt-pass-0000000000000000")))
        self.assertFalse(v["valid"])
        self.assertIn("Unknown signing key", v["reasons"][0])
        # A pass signed by a different Lowtides server (its own key) doesn't verify here.
        other_store, other_book, _ = setup()
        other_token, _ = other_book.issue(agent(other_store, "Research bot"), ISS)
        self.assertFalse(self.book.check(other_token)["valid"])

    def test_garbage_is_not_a_pass(self):
        for junk in ("", "abc", "a.b", "a.b.c.d", "!!.??.**", None, "x" * 20000):
            with self.assertRaises(PassError):
                self.book.check(junk)

    def test_expiry(self):
        token, p = self.book.issue(self.a, ISS)
        v = self.book.check(token, now=p["exp"] + 1)
        self.assertTrue(v["expired"])
        self.assertFalse(v["valid"])
        self.assertEqual(v["would_allow"], "denied")
        self.assertIn("EXPIRED", v["reasons"][0])
        short = PassBook(self.store, ttl_hours=1)
        _, p2 = short.issue(self.a, ISS)
        self.assertAlmostEqual(p2["exp"] - p2["iat"], 3600, delta=1)

    def test_kill_revokes_and_restart_does_not_bring_it_back(self):
        token, _ = self.book.issue(self.a, ISS)
        self.gov.kill(self.a["id"])
        v = self.book.check(token, amount=5, merchant="Acme")
        self.assertTrue(v["revoked"])
        self.assertFalse(v["valid"])
        self.assertEqual(v["would_allow"], "denied")
        self.assertIn("REVOKED", v["reasons"][0])
        self.assertFalse(self.book.current(self.store.agent(self.a["id"]), ISS)["valid"])  # no new pass while stopped
        self.store.update_agent(self.a["id"], status="on")
        self.assertTrue(self.book.check(token)["revoked"])  # the old pass stays dead
        fresh = self.book.current(self.store.agent(self.a["id"]), ISS)
        self.assertTrue(fresh["valid"])
        self.assertNotEqual(fresh["token"], token)

    def test_kill_revokes_even_if_restarted_before_anyone_checks(self):
        token, _ = self.book.issue(self.a, ISS)
        time.sleep(0.01)
        self.gov.kill(self.a["id"])
        self.store.update_agent(self.a["id"], status="on")
        v = self.book.check(token)
        self.assertTrue(v["revoked"])
        self.assertIn("Stop all", v["revoked_reason"])

    def test_stop_and_loop_pause_revoke(self):
        token, _ = self.book.issue(self.a, ISS)
        self.store.update_agent(self.a["id"], status="paused")
        self.assertIn("loop guard", self.book.check(token)["revoked_reason"])
        b = agent(self.store, "Second bot")
        t2, _ = self.book.issue(b, ISS)
        time.sleep(0.01)
        self.store.update_agent(b["id"], status="stopped")
        self.store.event(b["id"], "control", "STOP pressed STOP.")
        self.store.update_agent(b["id"], status="on")
        self.assertTrue(self.book.check(t2)["revoked"])

    def test_owner_revoke_holds_until_reissued(self):
        token, _ = self.book.issue(self.a, ISS)
        self.assertEqual(self.book.revoke(self.a["id"]), 1)
        self.assertEqual(self.book.check(token)["revoked_reason"], "The owner revoked this pass.")
        cur = self.book.current(self.a, ISS)
        self.assertFalse(cur["valid"])  # a running agent can't just fetch a new one
        self.assertTrue(cur["owner_revoked"])
        self.book.issue(self.a, ISS)
        self.assertTrue(self.book.current(self.a, ISS)["valid"])

    def test_rules_change_replaces_the_pass(self):
        token, _ = self.book.issue(self.a, ISS)
        self.assertEqual(self.book.current(self.a, ISS)["token"], token)  # unchanged: same pass
        self.store.update_agent(self.a["id"], rules=json.dumps({"max_per_purchase": 500}))
        a = self.store.agent(self.a["id"])
        self.assertIn("limits changed", self.book.check(token)["revoked_reason"])
        fresh = self.book.current(a, ISS)
        self.assertTrue(fresh["valid"])
        self.assertIn("Up to $500.00 per purchase.", fresh["limits_plain"])

    def test_deleted_agent_and_unknown_pass(self):
        token, _ = self.book.issue(self.a, ISS)
        self.store.run("DELETE FROM passes")
        self.assertIn("no record", self.book.check(token)["revoked_reason"])
        self.store.delete_agent(self.a["id"])
        self.assertIn("no longer exists", self.book.check(token)["revoked_reason"])


class TestWouldAllowMatchesSpend(unittest.TestCase):
    """would_allow must say what POST /v1/spend actually decides, and record nothing."""

    MAP = {"approved": "allowed", "needs_approval": "needs_owner_approval", "denied": "denied"}

    def run_cases(self, gov, book, store, a, token, cases):
        for amount, merchant, category in cases:
            before = store.one("SELECT COUNT(*) AS n FROM spend_requests")["n"]
            v = book.check(token, amount=amount, merchant=merchant, category=category)
            self.assertEqual(store.one("SELECT COUNT(*) AS n FROM spend_requests")["n"], before, "verify must not record")
            row = gov.request(store.agent(a["id"]), {"amount": amount, "merchant": merchant, "category": category,
                                                     "item": "thing", "reason": "for the task"})
            self.assertEqual(v["would_allow"], self.MAP[row["decision"]], (amount, merchant, category, v["reasons"], row["reasons"]))
            self.assertEqual(v["reasons"][0], row["reasons"][0], (amount, merchant))

    def test_matches_for_allowed_approval_and_denied(self):
        store, book, gov = setup()
        a = agent(store, "Shopper")
        store.update_agent(a["id"], rules=json.dumps({"max_per_purchase": 60, "daily_purchase_limit": 100,
                                                       "approval_over": 25, "blocked_categories": ["gift cards"],
                                                       "blocked_merchants": ["shady llc"]}))
        store.add_merchant("Acme Data", "acme@example.com", "data")
        store.add_merchant("CloudGPU Inc", "gpu@example.com", "compute")
        token, _ = book.issue(store.agent(a["id"]), ISS)
        self.run_cases(gov, book, store, a, token, [
            (12.0, "Acme Data", "data"),          # allowed
            (12.0, "Acme Data", "data"),          # duplicate: needs approval
            (45.0, "Acme Data", ""),              # over the approval line: needs approval
            (480.0, "CloudGPU Inc", "compute"),   # over per-purchase: denied
            (10.0, "Gift Hub", "gift cards"),     # blocked category: denied
            (5.0, "Shady LLC", "data"),           # blocked merchant: denied
            (8.0, "Brand New Shop", "data"),      # new merchant: needs approval
            (50.0, "CloudGPU Inc", "compute"),    # would pass the daily limit: denied
        ])

    def test_matches_with_paypal_configured_and_allowed_categories(self):
        store, book, gov = setup(PayPal("client", "secret", base_url="http://127.0.0.1:9"))
        a = agent(store, "Picky")
        store.update_agent(a["id"], rules=json.dumps({"allowed_categories": ["data"]}))
        store.add_merchant("No Email Co", "", "data")
        token, _ = book.issue(store.agent(a["id"]), ISS)
        self.run_cases(gov, book, store, a, token, [
            (5.0, "No Email Co", "data"),    # known merchant without a PayPal payee: needs approval
            (5.0, "No Email Co", "compute"),  # not an allowed category: denied
        ])


class TestPassHttp(ServerCase):
    def new_agent(self, name):
        s, r, _ = self.api("POST", "/api/agents", {"name": name, "daily_budget": 3})
        return r["agent"]

    def test_agent_gets_and_merchant_verifies(self):
        a = self.new_agent("Pass bot")
        auth = {"Authorization": "Bearer " + a["key"]}
        s, r, _ = http("GET", self.url + "/v1/pass")
        self.assertEqual(s, 401)
        s, r, _ = http("GET", self.url + "/v1/pass", headers=auth)
        self.assertEqual(s, 200, r)
        token = r["pass"]
        self.assertTrue(r["valid"])
        self.assertEqual(r["issuer"], self.url)
        s, r2, _ = http("GET", self.url + "/v1/pass", headers=auth)
        self.assertEqual(r2["pass"], token)  # stable until something changes
        s, keys, h = http("GET", self.url + "/.well-known/lowtides-pass-keys.json")
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")
        k = keys["keys"][0]
        self.assertEqual((k["kty"], k["crv"], k["alg"]), ("OKP", "Ed25519", "EdDSA"))
        hp, bp, sp = token.split(".")
        self.assertTrue(ed25519.verify(b64d(k["x"]), (hp + "." + bp).encode(), b64d(sp)))
        s, v, h = http("POST", self.url + "/v1/pass/verify", {"pass": token, "amount": 5, "merchant": "Nobody Yet"})
        self.assertEqual(s, 200)
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")
        self.assertTrue(v["valid"])
        self.assertEqual(v["would_allow"], "needs_owner_approval")
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": token, "amount": 5000, "merchant": "Nobody Yet"})
        self.assertEqual(v["would_allow"], "denied")
        payload = json.loads(b64d(bp))
        payload["limits"]["max_per_purchase"] = 99999
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": resign_parts(token, payload=payload)})
        self.assertEqual(s, 200)
        self.assertFalse(v["valid"])
        self.assertFalse(v["signature_ok"])
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": "not-a-pass"})
        self.assertEqual(s, 400)
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": token, "amount": "lots"})
        self.assertEqual(s, 400)

    def test_dashboard_view_revoke_and_reissue(self):
        a = self.new_agent("Dash bot")
        s, r, _ = self.api("GET", "/api/agents/%d/pass" % a["id"])
        self.assertEqual(s, 200)
        token = r["pass"]["token"]
        self.assertTrue(r["pass"]["valid"])
        s, r, _ = self.api("POST", "/api/agents/%d/pass/revoke" % a["id"], {})
        self.assertEqual((s, r["revoked"], r["agent_status"]), (200, 1, "on"))
        self.assertTrue(r["pass"]["owner_revoked"])  # the dialog offers "Issue a new pass"
        self.assertTrue(r["pass"]["revoked"])
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": token})
        self.assertTrue(v["revoked"])
        s, r, _ = http("GET", self.url + "/v1/pass", headers={"Authorization": "Bearer " + a["key"]})
        self.assertEqual(s, 409)
        self.assertTrue(r["revoked"])
        s, r, _ = self.api("POST", "/api/agents/%d/pass" % a["id"], {})
        self.assertEqual(s, 200)
        self.assertTrue(r["pass"]["valid"])
        self.assertNotEqual(r["pass"]["token"], token)
        s, r, _ = self.api("GET", "/api/agents/999999/pass")
        self.assertEqual(s, 404)

    def test_kill_over_http_revokes(self):
        a = self.new_agent("Kill bot")
        s, r, _ = http("GET", self.url + "/v1/pass", headers={"Authorization": "Bearer " + a["key"]})
        token = r["pass"]
        self.api("POST", "/api/agents/%d/kill" % a["id"], {})
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": token, "amount": 1, "merchant": "Acme"})
        self.assertTrue(v["revoked"])
        self.assertEqual(v["would_allow"], "denied")
        s, r, _ = self.api("GET", "/api/agents/%d/pass" % a["id"])
        self.assertTrue(r["pass"]["revoked"])
        self.assertEqual(r["agent_status"], "stopped")

    def test_mcp_get_pass(self):
        a = self.new_agent("MCP bot")
        env = dict(os.environ)
        try:
            os.environ["LOWTIDES_URL"] = self.url
            os.environ["LOWTIDES_AGENT_KEY"] = a["key"]
            r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_pass", "arguments": {}}})
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertFalse(r["result"]["isError"])
        text = r["result"]["content"][0]["text"]
        self.assertIn("Status: valid", text)
        self.assertIn("/v1/pass/verify", text)

    def test_verifier_page_and_crawler_files(self):
        s, raw, _ = http("GET", self.url + "/pass/")
        self.assertEqual(s, 200)
        for needle in ("Check a Lowtides Pass", "/v1/pass/verify", 'rel="canonical"', "AP2-aligned", "Need a website?"):
            self.assertIn(needle, raw)
        s, raw, _ = http("GET", self.url + "/sitemap.xml")
        self.assertIn("/pass/", raw)
        s, raw, _ = http("GET", self.url + "/llms.txt")
        self.assertIn("get_pass", raw)
        s, raw, _ = http("GET", self.url + "/app/")  # the dashboard moved from / to /app/
        self.assertIn("/pass.js", raw)
        s, raw, _ = http("GET", self.url + "/pass.js")
        self.assertEqual(s, 200)


class TestIssuer(ServerCase):
    """The issuer on a pass can't be chosen by whoever sends the request."""

    def new_pass(self, headers):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Iss bot %f" % time.time()})
        s, r, _ = http("GET", self.url + "/v1/pass", headers=dict(headers, Authorization="Bearer " + r["agent"]["key"]))
        self.assertEqual(s, 200, r)
        return json.loads(b64d(r["pass"].split(".")[1]))["iss"]

    def test_spoofed_forwarded_host_does_not_change_iss(self):
        iss = self.new_pass({"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"})
        self.assertEqual(iss, self.url)
        self.assertNotIn("evil", iss)

    def test_env_wins(self):
        os.environ["LOWTIDES_PUBLIC_URL"] = "https://lowtides.ai/"
        try:
            self.assertEqual(self.new_pass({"X-Forwarded-Host": "evil.example"}), "https://lowtides.ai")
        finally:
            os.environ.pop("LOWTIDES_PUBLIC_URL", None)

    def test_host_header_allow_list(self):
        from lowtides.passport import issuer_url

        class H:
            def __init__(self, **hd):
                self.headers = hd
        base = self.app.config.paypal["public_base_url"]
        self.assertEqual(issuer_url(H(Host="evil.example"), self.app), base)
        self.assertEqual(issuer_url(H(Host="evil.example", **{"X-Forwarded-Host": "evil.example"}), self.app), base)
        self.assertEqual(issuer_url(H(Host="localhost:9999"), self.app), "http://localhost:9999")
        self.assertEqual(issuer_url(H(Host="127.0.0.1:1234"), self.app), "http://127.0.0.1:1234")
        self.assertEqual(issuer_url(H(Host="bad host/x"), self.app), base)
        self.assertEqual(issuer_url(H(), self.app), base)
        old = self.app.config.data["paypal"]["public_base_url"]
        self.app.config.data["paypal"]["public_base_url"] = "https://pay.example.org"
        try:
            self.assertEqual(issuer_url(H(Host="pay.example.org"), self.app), "https://pay.example.org")
            self.assertEqual(issuer_url(H(Host="other.example.org"), self.app), "https://pay.example.org")
        finally:
            self.app.config.data["paypal"]["public_base_url"] = old


class TestPassStopAll(ServerCase):
    def test_stop_all_revokes_every_pass(self):
        tokens = []
        for name in ("One", "Two"):
            s, r, _ = self.api("POST", "/api/agents", {"name": name, "daily_budget": 3})
            s, p, _ = http("GET", self.url + "/v1/pass", headers={"Authorization": "Bearer " + r["agent"]["key"]})
            tokens.append(p["pass"])
        self.api("POST", "/api/stop-all", {})
        for t in tokens:
            s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": t})
            self.assertTrue(v["revoked"])
            self.assertFalse(v["valid"])


class TestPassLocked(ServerCase):
    admin = "pw-pass"

    def test_dashboard_pass_needs_password_but_verify_does_not(self):
        s, r, _ = self.api("POST", "/api/agents", {"name": "Locked", "daily_budget": 3})
        aid = r["agent"]["id"]
        s, _, _ = http("GET", self.url + "/api/agents/%d/pass" % aid)
        self.assertEqual(s, 401)
        s, _, _ = http("POST", self.url + "/api/agents/%d/pass/revoke" % aid, {})
        self.assertEqual(s, 401)
        s, r, _ = self.api("GET", "/api/agents/%d/pass" % aid)
        self.assertEqual(s, 200)
        s, v, _ = http("POST", self.url + "/v1/pass/verify", {"pass": r["pass"]["token"]})
        self.assertEqual(s, 200)
        self.assertTrue(v["valid"])
        s, _, _ = http("GET", self.url + "/.well-known/lowtides-pass-keys.json")
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()
