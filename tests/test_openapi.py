"""The OpenAPI spec and the Postman collection stay true to the server.

* docs/openapi.yaml is exactly what lowtides/openapi.py generates, and /openapi.json serves the same spec.
* Every $ref resolves, operation IDs are unique, path parameters are declared.
* Every documented route is really routed by the server (no "Unknown endpoint").
* The Postman collection only calls documented routes, and replaying it against a live server
  passes the same decision checks its test scripts make.
"""
import json
import os
import re
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from test_http import ServerCase, http

from lowtides import openapi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTMAN = os.path.join(ROOT, "docs", "postman", "Lowtides.postman_collection.json")
POSTMAN_ENV = os.path.join(ROOT, "docs", "postman", "Lowtides.postman_environment.json")
METHODS = ("get", "post", "put", "patch", "delete")


def _walk(v):
    if isinstance(v, dict):
        yield v
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)


def _resolve(spec, ref):
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _items(coll):
    for folder in coll["item"]:
        for item in folder.get("item", [folder]):
            yield folder["name"], item


def _spec_path_for(method, raw):
    """Match a Postman URL (with {{vars}}) to a spec path template."""
    path = raw.replace("{{baseUrl}}", "").split("?")[0]
    path = re.sub(r"\{\{\w+\}\}", "X", path)
    for template, ops in openapi.SPEC["paths"].items():
        if method.lower() in ops and re.fullmatch(re.sub(r"\{\w+\}", "[^/]+", template), path):
            return template
    return None


class TestSpec(unittest.TestCase):
    def test_yaml_matches_source(self):
        with open(openapi.YAML_PATH, encoding="utf-8") as f:
            on_disk = f.read()
        self.assertEqual(on_disk, openapi.yaml_text(),
                         "docs/openapi.yaml is out of date. Run: python3 -m lowtides.openapi --write")

    def test_yaml_parses_back_to_the_same_spec(self):
        try:
            import yaml  # optional; not a Lowtides dependency
        except ImportError:
            self.skipTest("PyYAML not installed")
        with open(openapi.YAML_PATH, encoding="utf-8") as f:
            self.assertEqual(yaml.safe_load(f), json.loads(json.dumps(openapi.SPEC)))

    def test_shape(self):
        s = openapi.SPEC
        self.assertEqual(s["openapi"], "3.1.0")
        for key in ("info", "paths", "components", "webhooks", "servers"):
            self.assertIn(key, s)
        for name in ("requestSpend", "getSpend", "getBudget", "createChatCompletion", "createMessage", "listModels",
                     "approveSpend", "denySpend", "killAgent", "stopAll", "addWebhook"):
            self.assertIn(name, {op["operationId"] for p in s["paths"].values() for m, op in p.items() if m in METHODS})
        self.assertEqual(set(s["webhooks"]), {"spend.needs_approval", "spend.paid", "spend.denied", "agent.killed",
                                              "stop_all", "loop_guard.paused"})

    def test_refs_resolve_and_ids_unique(self):
        s = openapi.SPEC
        for node in _walk(s):
            if "$ref" in node:
                self.assertIsInstance(_resolve(s, node["$ref"]), dict, node["$ref"])
        ids = [op["operationId"] for p in list(s["paths"].values()) + list(s["webhooks"].values())
               for m, op in p.items() if m in METHODS]
        self.assertEqual(len(ids), len(set(ids)))
        schemes = set(s["components"]["securitySchemes"])
        for p in s["paths"].values():
            for m, op in p.items():
                self.assertTrue(op.get("responses"), op["operationId"])
                for code in op["responses"]:
                    self.assertRegex(code, r"^[1-5]\d\d$")
                for req in op.get("security", []):
                    self.assertTrue(set(req) <= schemes)

    def test_path_params_declared(self):
        for template, ops in openapi.SPEC["paths"].items():
            names = set(re.findall(r"\{(\w+)\}", template))
            for m, op in ops.items():
                declared = {p["name"] for p in op.get("parameters", []) if p["in"] == "path"}
                self.assertEqual(names, declared, "%s %s" % (m, template))

    def test_json_safe(self):
        json.dumps(openapi.SPEC, allow_nan=False)


class Receiver:
    """A local stand-in for a Zapier Catch Hook."""

    def __init__(self):
        self.got = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.got.append((dict(self.headers.items()), body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hooks/catch/1/abc/" % self.srv.server_address[1]


class TestServedSpecAndPostman(ServerCase):
    def test_served_json_equals_source(self):
        s, body, h = http("GET", self.url + "/openapi.json")
        self.assertEqual(s, 200)
        self.assertEqual(body, json.loads(json.dumps(openapi.SPEC)))
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")

    def test_every_documented_route_is_routed(self):
        _, r, _ = self.api("POST", "/api/agents", {"name": "Route check %f" % time.time()})
        key, aid = r["agent"]["key"], r["agent"]["id"]
        fill = {"id": "sp_0000000000000000", "agent_id": str(aid), "webhook_id": "999999"}
        for template, ops in openapi.SPEC["paths"].items():
            if template == "/api/stop-all":
                continue  # covered by the Postman replay; would stop every agent here
            for method, op in ops.items():
                path = re.sub(r"\{(\w+)\}", lambda m: fill[m.group(1)], template)
                body = None
                if "requestBody" in op:
                    body = op["requestBody"]["content"]["application/json"].get("example", {})
                headers = {"Authorization": "Bearer " + key}
                s, resp, _ = http(method.upper(), self.url + path, body, headers)
                self.assertNotEqual(resp, {"error": "Unknown endpoint"}, "%s %s is documented but not routed" % (method, template))
                self.assertIn(str(s), op["responses"], "%s %s returned undocumented %s: %s" % (method, template, s, resp))

    def test_postman_only_calls_documented_routes(self):
        with open(POSTMAN, encoding="utf-8") as f:
            coll = json.load(f)
        self.assertEqual(coll["info"]["schema"], "https://schema.getpostman.com/json/collection/v2.1.0/collection.json")
        with open(POSTMAN_ENV, encoding="utf-8") as f:
            env = json.load(f)
        self.assertIn("baseUrl", {v["key"] for v in env["values"]})
        for folder, item in _items(coll):
            r = item["request"]
            self.assertIsNotNone(_spec_path_for(r["method"], r["url"]["raw"]),
                                 "%s / %s calls an undocumented route" % (folder, item["name"]))
            self.assertTrue(any(e["listen"] == "test" for e in item["event"]), item["name"])

    def test_postman_collection_replays_against_a_live_server(self):
        """Runs the collection in order, like Postman's Runner, checking the same expectations its scripts check."""
        with open(POSTMAN, encoding="utf-8") as f:
            coll = json.load(f)
        hook = Receiver()
        self.addCleanup(hook.srv.shutdown)
        self.app.notify.allow_private = True  # the receiver lives on 127.0.0.1
        var = {v["key"]: v["value"] for v in coll["variable"]}
        var.update(baseUrl=self.url, zapierHookUrl=hook.url)
        self.api("POST", "/api/merchants", {"name": "Acme Data", "paypal_email": "merchant-demo@example.com", "category": "data"})

        def sub(text):
            text = text.replace("{{$timestamp}}", str(int(time.time() * 1000)))
            return re.sub(r"\{\{(\w+)\}\}", lambda m: str(var[m.group(1)]), text)

        def get(obj, dotted):
            for part in dotted.split("."):
                obj = obj[int(part)] if isinstance(obj, list) else obj[part]
            return obj

        checked = 0
        for folder, item in _items(coll):
            r = item["request"]
            headers = {h["key"]: sub(h["value"]) for h in r["header"]}
            headers = {k: v for k, v in headers.items() if v}
            raw = r.get("body", {}).get("raw")
            data = json.loads(sub(raw)) if raw else None
            s, j, h = http(r["method"], sub(r["url"]["raw"]), data, headers)
            name = "%s / %s" % (folder, item["name"])
            self.assertEqual(s, 200, "%s -> %s %s" % (name, s, j))
            script = "\n".join(next(e for e in item["event"] if e["listen"] == "test")["script"]["exec"])
            for path, value in re.findall(r"pm\.expect\(j\.([\w.]+)\)\.to\.eql\((.+?)\);", script):
                self.assertEqual(get(j, path), json.loads(value), "%s: j.%s" % (name, path))
                checked += 1
            for path, value in re.findall(r"pm\.expect\(j\.([\w.]+)\)\.to\.include\((\".*?\")\);", script):
                self.assertIn(json.loads(value), get(j, path), "%s: j.%s" % (name, path))
                checked += 1
            for path, pattern in re.findall(r"pm\.expect\(j\.([\w.]+)\)\.to\.match\(/(.+?)/\);", script):
                self.assertRegex(get(j, path), pattern, name)
                checked += 1
            if "X-Lowtides-Cost" in script:
                self.assertIn("X-Lowtides-Cost", h)
            for k, path in re.findall(r"pm\.collectionVariables\.set\(\"(\w+)\", j\.([\w.]+)\);", script):
                var[k] = get(j, path)
            for k, path in re.findall(r"pm\.collectionVariables\.set\(\"(\w+)\", JSON\.stringify\(j\.([\w.]+)\)\);", script):
                var[k] = json.dumps(get(j, path))
            if "heldSpendId" in script and "map(r => r.id)" in script:
                self.assertIn(var["heldSpendId"], [x["id"] for x in j["requests"]])
        self.assertGreater(checked, 25)
        self.assertTrue(any(json.loads(b)["type"] == "test.ping" for _, b in hook.got), "Zapier test event not received")


if __name__ == "__main__":
    unittest.main()
