import json
import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import helpers  # noqa: F401  (sets sys.path)

from lowtides import mcp

GOOD_KEY = "lt-goodkey"


class Fake(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self):
        if self.headers.get("Authorization") != "Bearer " + GOOD_KEY:
            self._send(401, {"error": "invalid agent key"})
            return False
        return True

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Fake.seen.append(("POST", self.path, body))
        if not self._authed():
            return
        if self.path != "/v1/spend":
            return self._send(404, {"error": "not found"})
        m = body["merchant"]
        base = {"id": "sp_1", "amount": body["amount"], "merchant": m, "flags": [], "remaining_today": 37.5}
        if m == "Acme Data":
            base.update(decision="approved", status="paid", reasons=["Within the $20.00 auto-approve limit."],
                        payout={"id": "p1", "status": "SIMULATED", "simulated": True})
        elif m == "New Shop":
            base.update(decision="needs_approval", status="pending", reasons=["New merchant."], flags=["new_merchant"])
        else:
            base.update(decision="denied", status="denied", reasons=["Category is blocked."])
        self._send(200, base)

    def do_GET(self):
        Fake.seen.append(("GET", self.path, None))
        if not self._authed():
            return
        if self.path == "/v1/spend/sp_1":
            return self._send(200, {"id": "sp_1", "decision": "needs_approval", "status": "approved",
                                    "amount": 30, "merchant": "New Shop", "reasons": ["Approved by a person."]})
        if self.path == "/v1/budget":
            return self._send(200, {
                "agent": "Research bot", "status": "on",
                "tokens": {"daily_budget": 5.0, "spent_today": 0.12},
                "purchases": {"daily_limit": 200, "spent_today": 12.5, "remaining_today": 187.5,
                              "auto_approve_up_to": 20, "max_per_purchase": 50},
                "rules": {"max_per_purchase": 50}})
        self._send(404, {"error": "not found"})


def call(name, args, mid=1):
    return mcp.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                       "params": {"name": name, "arguments": args}})


def text(resp):
    return resp["result"]["content"][0]["text"]


SPEND = {"amount": 12.5, "reason": "Needed for leads", "item": "credits"}


class TestMcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["LOWTIDES_URL"] = "http://127.0.0.1:%d" % self.port
        os.environ["LOWTIDES_AGENT_KEY"] = GOOD_KEY
        Fake.seen.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_initialize(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual(r["id"], 1)
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "lowtides")

    def test_notification_and_ping(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "ping"})["result"], {})

    def test_tools_list(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertEqual(set(tools), {"request_spend", "check_spend", "get_budget"})
        for t in tools.values():
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
        self.assertEqual(set(tools["request_spend"]["inputSchema"]["required"]), {"amount", "merchant", "item", "reason"})

    def test_request_spend_approved(self):
        r = call("request_spend", dict(SPEND, merchant="Acme Data", category="data", task_id="t1"))
        self.assertFalse(r["result"]["isError"])
        t = text(r)
        self.assertIn("Decision: approved", t)
        self.assertIn("auto-approve limit", t)
        self.assertIn("simulated", t)
        method, path, body = Fake.seen[0]
        self.assertEqual((method, path), ("POST", "/v1/spend"))
        self.assertEqual(body["category"], "data")
        self.assertEqual(body["task_id"], "t1")

    def test_request_spend_needs_approval(self):
        r = call("request_spend", dict(SPEND, merchant="New Shop"))
        self.assertFalse(r["result"]["isError"])
        t = text(r)
        self.assertIn("Decision: needs_approval", t)
        self.assertIn("New merchant.", t)
        self.assertIn("Do NOT proceed", t)
        self.assertIn("check_spend", t)

    def test_request_spend_denied(self):
        r = call("request_spend", dict(SPEND, merchant="Gift Co"))
        self.assertFalse(r["result"]["isError"])
        t = text(r)
        self.assertIn("Decision: denied", t)
        self.assertIn("Category is blocked.", t)
        self.assertIn("Do not retry the same purchase", t)

    def test_request_spend_bad_args(self):
        r = call("request_spend", {"merchant": "x"})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(Fake.seen, [])

    def test_check_spend(self):
        r = call("check_spend", {"id": "sp_1"})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("status: approved", text(r))
        self.assertIn("may continue", text(r))
        self.assertEqual(Fake.seen[0][:2], ("GET", "/v1/spend/sp_1"))

    def test_check_spend_unknown_id(self):
        r = call("check_spend", {"id": "nope"})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("404", text(r))

    def test_get_budget(self):
        r = call("get_budget", {})
        self.assertFalse(r["result"]["isError"])
        t = text(r)
        self.assertIn("Research bot", t)
        self.assertIn("$187.50", t)

    def test_bad_key(self):
        os.environ["LOWTIDES_AGENT_KEY"] = "lt-wrong"
        r = call("get_budget", {})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("401", text(r))

    def test_missing_key(self):
        del os.environ["LOWTIDES_AGENT_KEY"]
        r = call("get_budget", {})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("LOWTIDES_AGENT_KEY", text(r))

    def test_server_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        os.environ["LOWTIDES_URL"] = "http://127.0.0.1:%d" % port
        r = call("request_spend", dict(SPEND, merchant="Acme Data"))
        self.assertTrue(r["result"]["isError"])
        self.assertIn("Could not reach Lowtides", text(r))

    def test_unknown_tool(self):
        r = call("nope", {})
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_method(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(r["error"]["code"], -32601)
        self.assertEqual(r["id"], 9)

    def test_invalid_request(self):
        r = mcp.handle({"id": 3, "method": "ping"})
        self.assertEqual(r["error"]["code"], -32600)

    def test_serve_loop(self):
        import io
        inp = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n'
                          '{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        out = io.StringIO()
        mcp.serve(inp, out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
