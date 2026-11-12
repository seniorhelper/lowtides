"""Optional sign-in accounts: off by default, and with a stand-in sign-in service: sign up, sign in,
the dashboard persisting across browsers and across a dropped sandbox, the signing key never leaving
the server, sign out, rate limits, and one account never seeing another's data."""
import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from test_demo import Browser, demo_server, http

from lowtides import accounts


class FakeSupabase:
    """Just enough of Supabase Auth and one PostgREST table for these tests. Nothing leaves this machine."""

    def __init__(self):
        self.users, self.rows, self.calls, self.down = {}, {}, [], False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _in(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_POST(self):
                d = self._in()
                fake.calls.append((self.path.split("?")[0], self.headers.get("apikey")))
                if fake.down:
                    return self._out(500, {"msg": "down"})
                if self.path.startswith("/auth/v1/signup"):
                    if d["email"] in fake.users:
                        return self._out(422, {"msg": "User already registered"})
                    u = {"id": str(uuid.uuid4()), "email": d["email"], "password": d["password"], "user_metadata": d.get("data") or {}}
                    fake.users[d["email"]] = u
                    return self._out(200, {"access_token": "t", "user": {k: u[k] for k in ("id", "email", "user_metadata")}})
                if self.path.startswith("/auth/v1/token"):
                    u = fake.users.get(d.get("email"))
                    if not u or u["password"] != d.get("password"):
                        return self._out(400, {"error_description": "Invalid login credentials"})
                    return self._out(200, {"access_token": "t", "user": {k: u[k] for k in ("id", "email", "user_metadata")}})
                if self.path.startswith("/rest/v1/lowtides_accounts"):
                    if self.headers.get("apikey") != "service-key":
                        return self._out(401, {"message": "no"})
                    fake.rows[d["user_id"]] = d
                    return self._out(201, None)
                self._out(404, {})

            def do_GET(self):
                if fake.down or self.headers.get("apikey") != "service-key":
                    return self._out(500 if fake.down else 401, {})
                uid = self.path.split("user_id=eq.")[1].split("&")[0]
                self._out(200, [fake.rows[uid]] if uid in fake.rows else [])

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]


def call(b, method, path, body=None):
    """(status, json, headers) without raising on 4xx."""
    try:
        return b(method, path, body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def follow(b, headers):
    c = headers.get("Set-Cookie")
    if c:
        b.cookie = c.split(";")[0]


class AccountsOff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.srv, cls.url = demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_off_by_default_and_the_demo_is_untouched(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("ACCOUNTS_ENABLED", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
                os.environ.pop(k, None)
            b = Browser(self.url)
            s, r, _ = call(b, "GET", "/api/account")
            self.assertEqual((s, r["enabled"], r["signed_in"]), (200, False, False))
            s, r, h = call(b, "POST", "/api/account/signin", {"email": "a@example.com", "password": "longenough"})
            self.assertEqual(s, 404)
            self.assertIsNone(h.get("Set-Cookie"))
            s, r, _ = call(b, "GET", "/api/state")
            self.assertEqual(s, 200)
            self.assertTrue(r["agents"])

    def test_keys_without_the_switch_stay_off(self):
        with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_ANON_KEY": "a", "SUPABASE_SERVICE_ROLE_KEY": "s"}):
            os.environ.pop("ACCOUNTS_ENABLED", None)
            self.assertFalse(accounts.enabled())
        with mock.patch.dict(os.environ, {"ACCOUNTS_ENABLED": "1", "SUPABASE_URL": "http://evil.example", "SUPABASE_ANON_KEY": "a", "SUPABASE_SERVICE_ROLE_KEY": "s"}):
            self.assertFalse(accounts.enabled(), "plain http to another host is refused")


class AccountsOn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeSupabase()
        cls.env = mock.patch.dict(os.environ, {"ACCOUNTS_ENABLED": "1", "SUPABASE_URL": cls.fake.url,
                                               "SUPABASE_ANON_KEY": "anon-key", "SUPABASE_SERVICE_ROLE_KEY": "service-key"})
        cls.env.start()
        cls.app, cls.srv, cls.url = demo_server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.fake.srv.shutdown()
        cls.env.stop()

    def setUp(self):
        accounts._tries.clear()
        self.fake.down = False

    def signup(self, email, role="shopper", pw="correct horse 9"):
        b = Browser(self.url)
        s, r, h = call(b, "POST", "/api/account/signup", {"email": email, "password": pw, "role": role})
        self.assertEqual(s, 200, r)
        follow(b, h)
        return b, r, h

    def signin(self, email, pw="correct horse 9"):
        b = Browser(self.url)
        s, r, h = call(b, "POST", "/api/account/signin", {"email": email, "password": pw})
        follow(b, h)
        return b, s, r, h

    def test_sign_up_then_the_dashboard_follows_the_account(self):
        b, r, h = self.signup("maya@example.com")
        self.assertEqual((r["signed_in"], r["role"], r["go"]), (True, "shopper", "/shop/"))
        self.assertNotIn("sid", r)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("lt_demo=acct-", h["Set-Cookie"])
        s, me, _ = call(b, "GET", "/api/account")
        self.assertEqual((me["signed_in"], me["email"], me["role"]), (True, "maya@example.com", "shopper"))
        s, r, _ = call(b, "POST", "/api/receipts", {"store": "Corner Bakery", "title": "Sourdough loaf", "amount": 7.5})
        self.assertEqual(s, 200, r)
        sid = b.cookie.split("=", 1)[1].split(".")[0]
        self.assertEqual(accounts.flush(self.app.sandboxes), 1)
        # another browser signs in: same dashboard, straight from memory
        b2, s, r, _ = self.signin("maya@example.com")
        self.assertEqual(s, 200)
        s, r, _ = call(b2, "GET", "/api/receipts?q=sourdough")
        self.assertEqual(len(r["receipts"]), 1)
        # the server forgets the sandbox (restart, idle): the next sign-in loads it from storage
        with self.app.sandboxes.lock:
            self.app.sandboxes._drop(sid)
        b3, s, r, _ = self.signin("maya@example.com")
        s, r, _ = call(b3, "GET", "/api/receipts?q=sourdough")
        self.assertEqual([x["title"] for x in r["receipts"]], ["Sourdough loaf"])
        s, st, _ = call(b3, "GET", "/api/state")
        self.assertTrue(st["agents"], "the restored account still has its AI shoppers")

    def test_snapshot_never_holds_the_signing_key_or_a_password(self):
        b, r, _ = self.signup("keys@example.com")
        call(b, "POST", "/api/receipts", {"store": "A", "title": "B", "amount": 1})
        accounts.flush(self.app.sandboxes)
        uid = self.fake.users["keys@example.com"]["id"]
        row = self.fake.rows[uid]
        self.assertEqual((row["email"], row["role"]), ("keys@example.com", "shopper"))
        doc = json.loads(row["snapshot"])
        raw = zlib.decompress(base64.b64decode(doc["db"]))
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(raw)
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT count(*) FROM pass_keys").fetchone()[0], 0)
            db.close()
        finally:
            os.remove(path)
        self.assertNotIn("correct horse", row["snapshot"])
        self.assertNotIn(b"correct horse", raw)
        # the browser-facing key is used for sign-in, the service key only for storage
        self.assertTrue(all(k == "anon-key" for p, k in self.fake.calls if p.startswith("/auth/")))
        self.assertTrue(all(k == "service-key" for p, k in self.fake.calls if p.startswith("/rest/")))

    def test_two_accounts_never_see_each_other(self):
        a, _, _ = self.signup("a@example.com")
        b, r, _ = self.signup("b@example.com", role="store")
        self.assertEqual(r["go"], "/merchants/")
        call(a, "POST", "/api/receipts", {"store": "Only A", "title": "Secret thing", "amount": 3})
        s, r, _ = call(b, "GET", "/api/receipts?q=secret")
        self.assertEqual(r["receipts"], [])
        visitor = Browser(self.url)
        s, r, _ = call(visitor, "GET", "/api/receipts?q=secret")
        self.assertEqual(r["receipts"], [])

    def test_wrong_password_forged_cookie_and_sign_out(self):
        self.signup("out@example.com")
        b, s, r, h = self.signin("out@example.com", pw="wrong password")
        self.assertEqual(s, 401)
        self.assertIsNone(h.get("Set-Cookie"))
        forged = Browser(self.url)
        uid = self.fake.users["out@example.com"]["id"].replace("-", "")
        forged.cookie = "lt_demo=acct-%s.%s" % (uid, "0" * 32)
        s, me, _ = call(forged, "GET", "/api/account")
        self.assertFalse(me["signed_in"], "a cookie this server did not sign is not an account")
        b, s, r, _ = self.signin("out@example.com")
        self.assertEqual(s, 200)
        s, r, h = call(b, "POST", "/api/account/signout", {})
        follow(b, h)
        self.assertNotIn("acct-", b.cookie)
        s, me, _ = call(b, "GET", "/api/account")
        self.assertFalse(me["signed_in"])

    def test_bad_input_rate_limit_and_cross_site(self):
        b = Browser(self.url)
        for body in ({"email": "nope", "password": "longenough"}, {"email": "a@example.com", "password": "short"}, {"email": "a@example.com"}):
            s, r, _ = call(b, "POST", "/api/account/signup", body)
            self.assertEqual(s, 400, body)
        try:
            s, _, _ = http("POST", self.url + "/api/account/signin", {"email": "a@example.com", "password": "longenough"},
                           {"Cookie": b.cookie, "Origin": "https://evil.example"})
        except urllib.error.HTTPError as e:
            s = e.code
        self.assertEqual(s, 403)
        accounts._tries.clear()
        codes = [call(b, "POST", "/api/account/signin", {"email": "x@example.com", "password": "wrong password"})[0] for _ in range(10)]
        self.assertEqual(codes[:8], [401] * 8)
        self.assertEqual(codes[8:], [429, 429])

    def test_storage_down_falls_back_to_a_working_demo(self):
        b, _, _ = self.signup("down@example.com")
        sid = b.cookie.split("=", 1)[1].split(".")[0]
        call(b, "POST", "/api/receipts", {"store": "Keep", "title": "Keep me", "amount": 2})
        accounts.flush(self.app.sandboxes)
        before = dict(self.fake.rows[self.fake.users["down@example.com"]["id"]])
        self.fake.down = True
        with self.app.sandboxes.lock:
            self.app.sandboxes._drop(sid)
        s, st, _ = call(b, "GET", "/api/state")          # storage unreachable: a fresh working dashboard, not an error
        self.assertEqual(s, 200)
        self.assertTrue(st["agents"])
        s, r, _ = call(Browser(self.url), "POST", "/api/account/signin", {"email": "down@example.com", "password": "correct horse 9"})
        self.assertIn(s, (401, 503))
        # storage comes back: the temporary copy made meanwhile must not have replaced the saved dashboard
        call(b, "POST", "/api/receipts", {"store": "Temp", "title": "Typed while storage was down", "amount": 1})
        self.fake.down = False
        accounts.flush(self.app.sandboxes)
        self.assertEqual(self.fake.rows[self.fake.users["down@example.com"]["id"]]["snapshot"], before["snapshot"])
        with self.app.sandboxes.lock:
            self.app.sandboxes._drop(sid)
        s, r, _ = call(b, "GET", "/api/receipts?q=keep")
        self.assertEqual([x["title"] for x in r["receipts"]], ["Keep me"])

    def test_a_broken_snapshot_is_refused(self):
        from lowtides.store import Store
        for bad in ("", "not json", json.dumps({"v": 1, "db": "AAAA"}), json.dumps({"v": 2, "db": ""}),
                    json.dumps({"v": 1, "db": base64.b64encode(zlib.compress(b"SQLite format 3\x00garbage")).decode()})):
            self.assertIsNone(accounts.load_into(Store(":memory:"), bad))


if __name__ == "__main__":
    unittest.main()
