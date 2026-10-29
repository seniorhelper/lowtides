"""Local fake servers that speak the real PayPal, Anthropic and OpenAI wire formats.

They let the test suite exercise Lowtides's real HTTP clients end to end with no
internet and no real money. They implement only what Lowtides uses.
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except ValueError:
            return {"_raw": raw.decode()}

    def _send(self, code, body):
        data = b"" if code == 204 else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(handler_cls, state):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.state = state
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# --------------------------------------------------------------------------
class FakePayPalHandler(_Base):
    def do_POST(self):
        st = self.server.state
        body = self._json()
        st["requests"].append((self.path, dict(self.headers.items()), body))
        rid = self.headers.get("PayPal-Request-Id")
        if self.path == "/v1/oauth2/token":
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(401, {"error": "invalid_client"})
            scope = "https://uri.paypal.com/services/invoicing openid"
            if not st.get("no_payouts_scope"):
                scope += " https://uri.paypal.com/payments/payouts"
            return self._send(200, {"scope": scope, "access_token": "A21-fake", "token_type": "Bearer",
                                    "app_id": "APP-80W284485P519543T", "expires_in": 32400})
        if not self.headers.get("Authorization", "") == "Bearer A21-fake":
            return self._send(401, {"name": "AUTHENTICATION_FAILURE"})
        if rid and rid in st["idem"]:
            code, resp = st["idem"][rid]
            return self._send(code, resp)

        def remember(code, resp):
            if rid:
                st["idem"][rid] = (code, resp)
            return self._send(code, resp)
        p = self.path
        if p == "/v3/vault/setup-tokens":
            pp = (body.get("payment_source") or {}).get("paypal") or {}
            ctx = pp.get("experience_context") or {}
            if pp.get("usage_type") not in ("MERCHANT", "PLATFORM") or not ctx.get("return_url") or not ctx.get("cancel_url"):
                return self._send(400, {"name": "INVALID_REQUEST", "message": "Request is not well-formed",
                                        "details": [{"issue": "MISSING_REQUIRED_PARAMETER"}]})
            sid = "SETUP" + uuid.uuid4().hex[:12].upper()
            st["setup_tokens"][sid] = {"approved": False, "usage_type": pp["usage_type"], "return_url": ctx["return_url"]}
            return remember(201, {"id": sid, "customer": {"id": "CUST" + uuid.uuid4().hex[:8].upper()},
                                  "status": "PAYER_ACTION_REQUIRED", "payment_source": {"paypal": {}},
                                  "links": [{"rel": "self", "href": "x"},
                                            {"rel": "approve", "href": "https://www.sandbox.paypal.com/agreements/approve?approval_session_id=" + sid}]})
        if p == "/v3/vault/payment-tokens":
            src = (body.get("payment_source") or {}).get("token") or {}
            stok = st["setup_tokens"].get(src.get("id"))
            if src.get("type") != "SETUP_TOKEN" or not stok:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})
            if not stok["approved"]:
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                                        "details": [{"issue": "SETUP_TOKEN_NOT_APPROVED"}], "debug_id": "dbg-vault"})
            tid = "PTOK" + uuid.uuid4().hex[:10].upper()
            st["payment_tokens"][tid] = {"setup": src["id"], "deleted": False, "email": st.get("payer_email", "buyer@example.com")}
            return remember(200, {"id": tid, "customer": {"id": "CUSTFAKE"},
                                  "payment_source": {"paypal": {"email_address": st["payment_tokens"][tid]["email"],
                                                                "payer_id": "PAYERFAKE", "usage_type": stok["usage_type"]}}})
        if p == "/v2/checkout/orders" and ((body.get("payment_source") or {}).get("paypal") or {}).get("vault_id"):
            vid = body["payment_source"]["paypal"]["vault_id"]
            t = st["payment_tokens"].get(vid)
            if not t or t["deleted"]:
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYMENT_SOURCE_NOT_FOUND"}]})
            oid = "ORD" + uuid.uuid4().hex[:12].upper()
            amount = body["purchase_units"][0]["amount"]
            st["orders"][oid] = {"id": oid, "intent": body["intent"], "amount": amount, "status": "COMPLETED", "vault_id": vid}
            return remember(201, {"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
                {"id": "CAP" + uuid.uuid4().hex[:10].upper(), "status": "COMPLETED", "amount": amount}]}}]})
        if p == "/v2/checkout/orders":
            oid = "ORD" + uuid.uuid4().hex[:12].upper()
            amount = body["purchase_units"][0]["amount"]
            st["orders"][oid] = {"id": oid, "intent": body["intent"], "amount": amount, "status": "PAYER_ACTION_REQUIRED",
                                 "custom_id": body["purchase_units"][0].get("custom_id")}
            return remember(200, {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                                  "links": [{"rel": "self", "href": "x"},
                                            {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=" + oid}]})
        if p.startswith("/v2/checkout/orders/") and p.endswith("/capture"):
            oid = p.split("/")[4]
            o = st["orders"].get(oid)
            if not o:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            if not o.get("approved"):
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
            if o["status"] == "COMPLETED":
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            o["status"] = "COMPLETED"
            cid = "CAP" + uuid.uuid4().hex[:10].upper()
            return remember(201, {"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
                {"id": cid, "status": st.get("capture_status", "COMPLETED"), "amount": o["amount"]}]}}]})
        if p.startswith("/v2/checkout/orders/") and p.endswith("/authorize"):
            oid = p.split("/")[4]
            o = st["orders"].get(oid)
            if not o or not o.get("approved"):
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
            aid = "AUTH" + uuid.uuid4().hex[:10].upper()
            st["auths"][aid] = {"amount": float(o["amount"]["value"]), "status": "CREATED", "captured": 0.0}
            o["status"] = "COMPLETED"
            return remember(201, {"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {"authorizations": [
                {"id": aid, "status": "CREATED", "amount": o["amount"]}]}}]})
        if p.startswith("/v2/payments/authorizations/") and p.endswith("/capture"):
            aid = p.split("/")[4]
            a = st["auths"].get(aid)
            if not a or a["status"] in ("VOIDED", "CAPTURED"):
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "AUTHORIZATION_ALREADY_CAPTURED"}]})
            amt = float(body["amount"]["value"])
            if amt < a["amount"] and not st.get("partial_capture", True):
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PARTIAL_CAPTURE_NOT_ENABLED"}]})
            if amt > a["amount"] * 1.15:
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "AMOUNT_EXCEEDS"}]})
            a["captured"] += amt
            a["status"] = "CAPTURED" if body.get("final_capture") else "PARTIALLY_CAPTURED"
            cid = "CAP" + uuid.uuid4().hex[:10].upper()
            st["captures"][cid] = {"amount": amt, "refunded": 0.0}
            return remember(201, {"id": cid, "status": "COMPLETED", "amount": body["amount"]})
        if p.startswith("/v2/payments/authorizations/") and p.endswith("/void"):
            aid = p.split("/")[4]
            a = st["auths"].get(aid)
            if not a:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            a["status"] = "VOIDED"
            return remember(204, {})
        if p.startswith("/v2/payments/captures/") and p.endswith("/refund"):
            cid = p.split("/")[4]
            c = st["captures"].get(cid)
            if not c:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            if st.get("refund_fails"):
                return self._send(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "REFUND_FAILED_INSUFFICIENT_FUNDS"}]})
            amt = float(body["amount"]["value"])
            c["refunded"] += amt
            return remember(201, {"id": "REF" + uuid.uuid4().hex[:8].upper(), "status": "COMPLETED", "amount": body["amount"]})
        if p == "/v2/invoicing/invoices":
            iid = "INV2-" + uuid.uuid4().hex[:16].upper()
            st["invoices"][iid] = body
            return remember(201, dict(body, id=iid, status="DRAFT"))
        if p.startswith("/v2/invoicing/invoices/") and p.endswith("/send"):
            iid = p.split("/")[4]
            if iid not in st["invoices"]:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            st["sent"].append(iid)
            return remember(200, {"href": "https://www.sandbox.paypal.com/invoice/p/#" + iid})
        if p == "/v1/payments/payouts":
            # Shape checks the real Payouts v1 API enforces (it answers 400 VALIDATION_ERROR).
            hdr = body.get("sender_batch_header") or {}
            it = (body.get("items") or [{}])[0]
            amt = (it.get("amount") or {})
            bad = []
            if not hdr.get("sender_batch_id") or len(hdr["sender_batch_id"]) > 256:
                bad.append("sender_batch_id")
            if it.get("recipient_type") != "EMAIL" or not it.get("receiver") or len(it["receiver"]) > 127:
                bad.append("receiver")
            if not isinstance(amt.get("value"), str) or "." not in amt["value"] or len(amt["value"].split(".")[1]) != 2 \
                    or len(amt.get("currency", "")) != 3:
                bad.append("amount")
            if len(it.get("sender_item_id", "")) > 63:
                bad.append("sender_item_id")
            if bad:
                return self._send(400, {"name": "VALIDATION_ERROR", "message": "Invalid request - see details",
                                        "debug_id": "dbg-validate", "details": [{"field": f, "issue": "INVALID"} for f in bad]})
            if st.get("payout_error"):
                code, err = st["payout_error"]
                if st.get("payout_error_times", 1000) > 0:
                    st["payout_error_times"] = st.get("payout_error_times", 1000) - 1
                    return self._send(code, dict(err, debug_id="dbg-" + uuid.uuid4().hex[:8]))
            if hdr["sender_batch_id"] in st["batch_ids"]:
                return self._send(400, {"name": "USER_BUSINESS_ERROR", "message": "Batch with given sender_batch_id already exists",
                                        "debug_id": "dbg-dup"})
            st["batch_ids"].add(hdr["sender_batch_id"])
            bid = "BATCH" + uuid.uuid4().hex[:10].upper()
            item_id = "ITEM" + uuid.uuid4().hex[:10].upper()
            st["payouts"][bid] = {"item_id": item_id, "receiver": it["receiver"], "amount": float(amt["value"]),
                                  "status": st.get("payout_status", "UNCLAIMED"), "pending_polls": st.get("pending_polls", 0)}
            return remember(201, {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING",
                                                   "sender_batch_header": hdr}})
        if p.startswith("/v1/payments/payouts-item/") and p.endswith("/cancel"):
            if body:
                return self._send(400, {"name": "VALIDATION_ERROR", "message": "cancel takes no body"})
            item_id = p.split("/")[4]
            for po in st["payouts"].values():
                if po["item_id"] == item_id:
                    if po["status"] != "UNCLAIMED":
                        return self._send(400, {"name": "ITEM_INCORRECT_STATUS",
                                                "message": "Item cannot be cancelled since it is not unclaimed."})
                    po["status"] = "RETURNED"
                    return remember(200, {"payout_item_id": item_id, "transaction_status": "RETURNED"})
            return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
        if p == "/v1/notifications/verify-webhook-signature":
            ok = body.get("transmission_sig") == "good-signature"
            return self._send(200, {"verification_status": "SUCCESS" if ok else "FAILURE"})
        return self._send(404, {"name": "NOT_FOUND", "path": p})

    def do_DELETE(self):
        st = self.server.state
        st["requests"].append(("DELETE " + self.path, dict(self.headers.items()), {}))
        if not self.headers.get("Authorization", "") == "Bearer A21-fake":
            return self._send(401, {"name": "AUTHENTICATION_FAILURE"})
        if self.path.startswith("/v3/vault/payment-tokens/"):
            only = st.get("vault_delete_error_ids")  # fail only these token IDs, if set
            if st.get("vault_delete_error") and st.get("vault_delete_error_times", 1000) > 0 \
                    and (not only or self.path.split("/")[4] in only):
                st["vault_delete_error_times"] = st.get("vault_delete_error_times", 1000) - 1
                code, err = st["vault_delete_error"]
                return self._send(code, dict(err, debug_id="dbg-" + uuid.uuid4().hex[:8]))
            t = st["payment_tokens"].get(self.path.split("/")[4])
            if not t or t["deleted"]:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})
            t["deleted"] = True
            return self._send(204, {})
        return self._send(404, {"name": "NOT_FOUND"})

    def do_GET(self):
        st = self.server.state
        if self.path.startswith("/v3/vault/payment-tokens/"):
            t = st["payment_tokens"].get(self.path.split("/")[4])
            if not t or t["deleted"]:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            return self._send(200, {"id": self.path.split("/")[4], "payment_source": {"paypal": {"email_address": t["email"]}}})
        if self.path.startswith("/v1/payments/payouts/"):
            bid = self.path.split("/")[4]
            po = st["payouts"].get(bid)
            if not po:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            if po["pending_polls"] > 0:  # PayPal still processing: no items yet
                po["pending_polls"] -= 1
                return self._send(200, {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING"}, "items": []})
            item = {"payout_item_id": po["item_id"], "transaction_status": po["status"]}
            if po["status"] == "UNCLAIMED":
                item["errors"] = {"name": "RECEIVER_UNREGISTERED", "message": "Receiver is unregistered", "debug_id": "x"}
            elif po["status"] == "FAILED":
                item["errors"] = {"name": "RECEIVER_ACCOUNT_LOCKED", "message": "Receiver account is locked", "debug_id": "y"}
            bstatus = "DENIED" if po["status"] in ("FAILED", "BLOCKED") else "SUCCESS"
            return self._send(200, {"batch_header": {"payout_batch_id": bid, "batch_status": bstatus}, "items": [item]})
        if self.path.startswith("/v2/checkout/orders/"):
            oid = self.path.split("/")[4]
            o = st["orders"].get(oid)
            if not o:
                return self._send(404, {"name": "RESOURCE_NOT_FOUND"})
            return self._send(200, {"id": oid, "status": o["status"]})
        return self._send(404, {"name": "NOT_FOUND"})


def paypal_state():
    return {"requests": [], "idem": {}, "orders": {}, "auths": {}, "captures": {}, "invoices": {}, "sent": [], "partial_capture": True,
            "payouts": {}, "payout_status": "UNCLAIMED", "batch_ids": set(), "pending_polls": 0, "payout_error": None,
            "setup_tokens": {}, "payment_tokens": {}, "vault_delete_error": None}


def approve(state, order_id):
    """What the buyer does on PayPal's page."""
    state["orders"][order_id]["approved"] = True


def approve_vault(state, setup_token_id):
    """What the payer does on PayPal's 'save PayPal for later' page."""
    state["setup_tokens"][setup_token_id]["approved"] = True


# --------------------------------------------------------------------------
class FakeLLMHandler(_Base):
    """Speaks both /v1/messages (Anthropic) and /chat/completions (OpenAI)."""

    def do_POST(self):
        st = self.server.state
        body = self._json()
        st["requests"].append((self.path, dict(self.headers.items()), body))
        model = body.get("model", "")
        prompt = json.dumps(body.get("messages"))
        if st.get("fail_models") and model in st["fail_models"]:
            return self._send(503, {"error": {"message": "overloaded"}})
        if st.get("reject_models") and model in st["reject_models"]:
            return self._send(400, {"error": {"message": "this model does not support that parameter"}})
        weak = st.get("weak_models", set())
        text = "I'm not sure." if model in weak else "Answer from %s." % model
        if st.get("reply"):
            text = st["reply"]
        if self.path == "/v1/messages":
            if not self.headers.get("x-api-key"):
                return self._send(401, {"type": "error", "error": {"type": "authentication_error"}})
            usage = {"input_tokens": max(1, len(prompt) // 4), "output_tokens": 7}
            if st.get("report_cache"):
                usage["cache_read_input_tokens"] = st["report_cache"]
            content = [{"type": "text", "text": text}]
            if st.get("tool_use"):
                content = [{"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}}]
            return self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                    "content": content, "stop_reason": "end_turn", "usage": usage})
        if self.path.endswith("/chat/completions"):
            resp = {"id": "cmpl_1", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
            if not st.get("no_usage"):
                resp["usage"] = {"prompt_tokens": max(1, len(prompt) // 4), "completion_tokens": 5,
                                 "prompt_tokens_details": {"cached_tokens": st.get("report_cache", 0)}}
            return self._send(200, resp)
        return self._send(404, {"error": "nope"})


def llm_state():
    return {"requests": [], "weak_models": set(), "fail_models": set()}
