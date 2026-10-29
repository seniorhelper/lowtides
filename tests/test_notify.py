"""Outbound webhooks (Zapier, Make, any URL): signing, retries, filtering, and the rule that
a slow or dead webhook never slows or blocks a spend decision."""
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import agent, chat, config, engine

from lowtides import notify
from lowtides.paypal import PayPal
from lowtides.spend import SpendGovernor
from lowtides.store import Store
from test_http import ServerCase, http


class Hook:
    """A local webhook receiver. `fail_first` answers 500 that many times; `delay` stalls each answer."""

    def __init__(self, fail_first=0, delay=0.0):
        self.got, self.fail_first, self.delay = [], fail_first, delay
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if outer.delay:
                    time.sleep(outer.delay)
                outer.got.append((dict(self.headers.items()), body))
                code = 500 if len(outer.got) <= outer.fail_first else 200
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hook" % self.srv.server_address[1]

    def events(self):
        return [json.loads(b) for _, b in self.got]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    return "http://127.0.0.1:%d/hook" % port


def silent_url(test):
    """A local socket that accepts connections and never answers, so a request just hangs.
    Stays on this machine (no unroutable outside address that a networked CI host might try)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    test.addCleanup(s.close)
    return "http://127.0.0.1:%d/hook" % s.getsockname()[1]


class Redirect:
    """A local server that answers every POST with a redirect to `target`."""

    def __init__(self, target, code=302):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(code)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hook" % self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def buy(amount, merchant="Acme Data", **kw):
    return dict({"amount": amount, "merchant": merchant, "item": "credits", "category": "data", "reason": "work"}, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.n = notify.attach(self.s)
        self.n.allow_private = True  # the receivers in these tests live on 127.0.0.1
        self.n.retry_delays = (0.05, 0.05, 0.05)
        self.n.timeout = 2
        self.g = SpendGovernor(self.s, PayPal("", ""))
        self.a = agent(self.s, "Buyer")
        self.s.add_merchant("Acme Data", "acme@example.com", "data")

    def hook(self, **kw):
        h = Hook(**kw)
        self.addCleanup(h.close)
        return h


class TestSigning(unittest.TestCase):
    def test_sign_and_verify(self):
        body = b'{"type":"spend.paid"}'
        header = notify.sign("whsec_x", body, ts=1700000000)
        self.assertRegex(header, r"^t=1700000000,v1=[0-9a-f]{64}$")
        self.assertTrue(notify.verify("whsec_x", body, header, now=1700000010))
        self.assertFalse(notify.verify("whsec_y", body, header, now=1700000010))       # wrong secret
        self.assertFalse(notify.verify("whsec_x", body + b" ", header, now=1700000010))  # tampered body
        self.assertFalse(notify.verify("whsec_x", body, header, now=1700009999))       # replayed later
        self.assertFalse(notify.verify("whsec_x", body, "garbage"))

    def test_emit_without_notifier_is_a_no_op(self):
        self.assertEqual(notify.emit(Store(":memory:"), "spend.paid", {"id": "sp_1"}), 0)


class TestDelivery(Base):
    def test_needs_approval_is_delivered_signed(self):
        h = self.hook()
        added = self.n.add(h.url, label="phone")
        self.assertTrue(added["secret"].startswith("whsec_"))
        self.assertNotIn("secret", added["webhook"])  # only shown once
        r = self.g.request(self.a, buy(45))
        self.assertEqual(r["decision"], "needs_approval")
        self.assertTrue(self.n.drain())
        (headers, body), = h.got
        ev = json.loads(body)
        self.assertEqual(ev["type"], "spend.needs_approval")
        self.assertEqual(headers["X-Lowtides-Event"], "spend.needs_approval")
        self.assertEqual((ev["data"]["id"], ev["data"]["amount"], ev["data"]["agent_name"]), (r["id"], 45.0, "Buyer"))
        self.assertTrue(ev["data"]["reasons"])
        self.assertTrue(notify.verify(added["secret"], body, headers[notify.SIGNATURE_HEADER]))

    def test_paid_and_denied_events(self):
        h = self.hook()
        self.n.add(h.url)
        self.g.request(self.a, buy(5))                       # paid (simulated)
        self.g.request(self.a, buy(5, category="gift cards"))  # denied by rules
        held = self.g.request(self.a, buy(30, item="x"))
        self.g.deny(held["id"], note="no")                   # denied by a person
        self.assertTrue(self.n.drain())
        kinds = [e["type"] for e in h.events()]
        self.assertEqual(sorted(kinds), sorted(["spend.paid", "spend.denied", "spend.needs_approval", "spend.denied"]))
        paid = next(e for e in h.events() if e["type"] == "spend.paid")
        self.assertEqual(paid["data"]["status"], "paid")

    def test_event_filter(self):
        only_paid, everything = self.hook(), self.hook()
        self.n.add(only_paid.url, events=["spend.paid"])
        self.n.add(everything.url)
        self.g.request(self.a, buy(45))
        self.g.request(self.a, buy(5))
        self.assertTrue(self.n.drain())
        self.assertEqual([e["type"] for e in only_paid.events()], ["spend.paid"])
        self.assertEqual(len(everything.got), 2)

    def test_bad_settings_are_refused(self):
        for url in ("ftp://x.com/h", "not a url", ""):
            with self.assertRaises(notify.NotifyError):
                self.n.add(url)
        with self.assertRaises(notify.NotifyError):
            self.n.add("https://hooks.zapier.com/x", events=["spend.everything"])

    def test_public_demo_only_allows_known_hook_services_and_masks_urls(self):
        n = notify.Notifier(Store(":memory:"), public_demo=True)
        for url in ("http://hooks.zapier.com/hooks/catch/1/a/", "https://169.254.169.254/latest/", "https://evil.example.com/h",
                    "https://hooks.zapier.com.evil.com/x"):
            with self.assertRaises(notify.NotifyError, msg=url):
                n.add(url)
        n.add("https://hooks.zapier.com/hooks/catch/123/secretpart/")
        self.assertNotIn("secretpart", n.hooks()[0]["url"][:-6])
        self.assertTrue(n.hooks()[0]["url"].startswith("https://hooks.zapier.com/..."))


class TestRetries(Base):
    def test_retries_with_backoff_then_succeeds(self):
        h = self.hook(fail_first=2)
        self.n.add(h.url)
        self.g.request(self.a, buy(45))
        self.assertTrue(self.n.drain())
        self.assertEqual(len(h.got), 3)
        log = sorted(self.n.deliveries(), key=lambda d: d["attempt"])
        self.assertEqual([(d["attempt"], d["ok"], d["status"]) for d in log], [(1, 0, 500), (2, 0, 500), (3, 1, 200)])
        ids = {json.loads(b)["id"] for _, b in h.got}
        self.assertEqual(len(ids), 1)  # the same event each time, so a receiver can de-duplicate
        self.assertEqual(len({hd["X-Lowtides-Delivery"] for hd, _ in h.got}), 3)

    def test_gives_up_after_the_last_retry_and_logs_it(self):
        self.n.add(dead_url())
        self.g.request(self.a, buy(45))
        self.assertTrue(self.n.drain())
        log = self.n.deliveries()
        self.assertEqual(len(log), 1 + len(self.n.retry_delays))
        self.assertTrue(all(not d["ok"] and d["error"] for d in log))

    def test_deleted_hook_stops_retrying(self):
        self.n.retry_delays = (0.3, 0.3)
        hid = self.n.add(dead_url())["webhook"]["id"]
        self.g.request(self.a, buy(45))
        time.sleep(0.1)
        self.n.delete(hid)
        self.assertTrue(self.n.drain())
        self.assertEqual(len(self.n.deliveries()), 1)


class TestNoSSRF(Base):
    def test_redirects_are_not_followed(self):
        inner = self.hook()  # stands in for http://169.254.169.254/... behind an allowed host
        for code in (301, 302, 303, 307, 308):
            r = Redirect(inner.url, code)
            self.addCleanup(r.close)
            hid = self.n.add(r.url)["webhook"]["id"]
            out = self.n.test(hid)
            self.assertFalse(out["ok"], code)
            self.assertEqual(out["status"], code)
            self.assertIn("never follows redirects", out["error"])
        self.assertEqual(inner.got, [])

    def test_private_addresses_are_refused_at_send_time_in_every_mode(self):
        h = self.hook()
        port = h.srv.server_address[1]
        self.n.allow_private = False  # a normal private install, not just the public demo
        for url in ("http://localhost:%d/hook" % port, "http://127.0.0.1:%d/hook" % port, "http://[::1]:%d/hook" % port):
            hid = self.n.add(url)["webhook"]["id"]
            out = self.n.test(hid)
            self.assertFalse(out["ok"], url)
            self.assertIn("Refused", out["error"], url)
        self.assertEqual(h.got, [])

    def test_a_name_that_rebinds_to_loopback_is_refused(self):
        h = self.hook()
        self.n.allow_private = False
        real = socket.getaddrinfo

        def fake(host, *a, **kw):  # a public-looking name whose DNS answer is now 127.0.0.1
            if host == "hooks.example-rebind.test":
                return real("127.0.0.1", *a, **kw)
            return real(host, *a, **kw)
        from unittest import mock
        with mock.patch.object(notify.socket, "getaddrinfo", fake):
            hid = self.n.add("http://hooks.example-rebind.test:%d/hook" % h.srv.server_address[1])["webhook"]["id"]
            out = self.n.test(hid)
        self.assertFalse(out["ok"])
        self.assertIn("Refused", out["error"])
        self.assertEqual(h.got, [])

    def test_blocked_address_classes(self):
        import ipaddress
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                   "224.0.0.1", "240.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1", "ff02::1",
                   "::ffff:127.0.0.1", "::ffff:169.254.169.254", "2002:7f00:1::1", "64:ff9b::a9fe:a9fe"):
            self.assertTrue(notify._blocked_ip(ipaddress.ip_address(ip)), ip)
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertFalse(notify._blocked_ip(ipaddress.ip_address(ip)), ip)

    def test_public_demo_ignores_the_private_override(self):
        n = notify.Notifier(Store(":memory:"), public_demo=True)
        n.allow_private = True
        with self.assertRaises(notify.NotifyError):
            n._post("http://127.0.0.1:9/hook", b"{}", {})

    def test_public_demo_allows_three_hooks(self):
        n = notify.Notifier(Store(":memory:"), public_demo=True)
        for i in range(notify.DEMO_MAX_HOOKS):
            n.add("https://hooks.zapier.com/hooks/catch/1/%d/" % i)
        with self.assertRaises(notify.NotifyError) as cm:
            n.add("https://hooks.zapier.com/hooks/catch/1/x/")
        self.assertIn("3 webhooks", str(cm.exception))


class TestNeverBlocks(Base):
    def test_dead_and_slow_webhooks_never_slow_a_spend(self):
        slow = self.hook(delay=1.5)
        self.n.add(slow.url)
        self.n.add(dead_url())
        self.n.add(silent_url(self))  # accepts, never answers: the connection just hangs
        start = time.time()
        results = [self.g.request(self.a, buy(45, item="item %d" % i)) for i in range(4)]
        results.append(self.g.request(self.a, buy(5)))
        took = time.time() - start
        self.assertLess(took, 0.5, "spend decisions waited on webhooks (%.2fs)" % took)
        self.assertEqual([r["decision"] for r in results], ["needs_approval"] * 4 + ["approved"])
        self.assertEqual(results[-1]["status"], "paid")

    def test_full_queue_drops_instead_of_waiting(self):
        n = notify.Notifier(Store(":memory:"))
        n.queue_size = 2
        n.q = __import__("queue").Queue(maxsize=2)
        n.workers = 0  # nobody drains it
        n.add(dead_url())
        start = time.time()
        for i in range(10):
            n.emit("spend.paid", {"i": i})
        self.assertLess(time.time() - start, 0.5)
        self.assertEqual(n.dropped, 8)

    def test_broken_store_never_raises_into_a_decision(self):
        class Broken:
            def q(self, *a):
                raise RuntimeError("disk gone")
        n = notify.Notifier.__new__(notify.Notifier)
        n.store = Broken()
        self.assertEqual(n.emit("spend.paid", {}), 0)


class TestLoopGuardEvent(unittest.TestCase):
    def test_loop_guard_pause_is_sent(self):
        eng, store = engine(config(loop_max_repeats=3, answer_memory=False))
        n = notify.attach(store)
        n.allow_private = True
        h = Hook()
        self.addCleanup(h.close)
        n.add(h.url, events=["loop_guard.paused"])
        a = agent(store)
        for _ in range(6):
            eng.handle("openai", chat("Say the same thing again"), a["key"])
        self.assertEqual(store.agent(a["id"])["status"], "paused")
        self.assertTrue(n.drain())
        self.assertEqual([e["type"] for e in h.events()], ["loop_guard.paused"])
        self.assertEqual(h.events()[0]["data"]["agent_id"], a["id"])


class TestWebhooksHTTP(ServerCase):
    def setUp(self):
        self.app.notify.retry_delays = (0.05,)
        self.app.notify.allow_private = True
        self.h = Hook()
        self.addCleanup(self.h.close)

    def tearDown(self):
        for w in self.app.notify.hooks():
            self.app.notify.delete(w["id"])

    def test_settings_api_add_list_test_delete(self):
        s, r, _ = self.api("POST", "/api/webhooks", {"url": self.h.url, "label": "Zap", "events": ["agent.killed"]})
        self.assertEqual(s, 200)
        hid = r["webhook"]["id"]
        s, lst, _ = self.api("GET", "/api/webhooks")
        self.assertEqual([w["id"] for w in lst["webhooks"]], [hid])
        self.assertIn("spend.needs_approval", lst["events"])
        s, t, _ = self.api("POST", "/api/webhooks/%d/test" % hid, {})
        self.assertEqual((s, t["ok"], t["status"]), (200, True, 200))
        self.assertEqual(self.h.events()[0]["type"], "test.ping")
        s, r, _ = self.api("DELETE", "/api/webhooks/%d" % hid)
        self.assertEqual((s, r["ok"]), (200, True))
        s, r, _ = self.api("DELETE", "/api/webhooks/%d" % hid)
        self.assertEqual(s, 404)
        s, r, _ = self.api("POST", "/api/webhooks", {"url": "nope"})
        self.assertEqual(s, 400)

    def test_cross_site_is_refused(self):
        s, r, _ = http("POST", self.url + "/api/webhooks", {"url": self.h.url}, {"Origin": "https://evil.example"})
        self.assertEqual(s, 403)
        self.assertEqual(self.app.notify.hooks(), [])

    def test_kill_and_stop_all_are_sent(self):
        self.api("POST", "/api/webhooks", {"url": self.h.url, "events": ["agent.killed", "stop_all"]})
        _, r, _ = self.api("POST", "/api/agents", {"name": "Doomed %f" % time.time()})
        self.api("POST", "/api/agents/%d/kill" % r["agent"]["id"], {})
        self.api("POST", "/api/stop-all", {})
        self.assertTrue(self.app.notify.drain())
        kinds = [e["type"] for e in self.h.events()]
        self.assertEqual(kinds, ["agent.killed", "stop_all"])
        self.assertEqual(self.h.events()[0]["data"]["agent_name"], r["agent"]["name"])
        self.assertIn("payouts_cancelled", self.h.events()[1]["data"])


if __name__ == "__main__":
    unittest.main()
