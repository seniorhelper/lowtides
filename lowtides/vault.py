"""PayPal on file: a saved PayPal payment method (Vault v3 payment token) per agent.

Why per agent: the kill switch is per agent. Revoking one agent's token cuts off that
agent's money source without touching the others; STOP ALL revokes every agent's.

Flow:
  connect_start  POST /v3/vault/setup-tokens       -> approve URL (payer agrees on PayPal)
  connect_finish POST /v3/vault/payment-tokens     <- /paypal/return?kind=vault&token=...
  revoke         DELETE /v3/vault/payment-tokens/{id}  (the kill switch calls this)
  topup          POST /v2/checkout/orders with payment_source.paypal.vault_id (no payer present)

Without PayPal keys, or in the public demo, every step is simulated and labeled
"simulated": nothing is sent to PayPal.
"""
import secrets
import time

from . import pricing
from .paypal import PayPalError

REVOKED_LINE = "Revoked the saved PayPal payment method, so this agent can't be charged again until you reconnect it."
USABLE = "active"


class VaultError(Exception):
    def __init__(self, message, http=400):
        super().__init__(message)
        self.http = http


class Vault:
    def __init__(self, store, paypal, base_url="http://localhost:8787", simulate=False):
        self.store = store
        self.paypal = paypal
        self._base = base_url  # a string, or a function returning one (read on every use)
        self.force_simulate = bool(simulate)
        self.retry_delay = 1.0

    @property
    def base_url(self):
        return (self._base() if callable(self._base) else self._base).rstrip("/")

    @property
    def simulated(self):
        return self.force_simulate or not self.paypal.configured

    # ---- what the dashboard shows -------------------------------------------------------
    def status(self, agent_id):
        r = self.store.vault_current(agent_id)
        pending = self.store.vault_pending(agent_id=agent_id)
        if pending and r and pending["id"] < r["id"]:
            pending = None
        if not r:
            return {"status": "waiting_for_paypal" if pending else "none", "label": "waiting for you on PayPal" if pending else "not connected",
                    "simulated": self.simulated}
        label = {"active": "connected", "revoked": "revoked", "revoke_failed": "revoke failed"}.get(r["status"], r["status"])
        return {"status": r["status"], "label": label, "token_id": r["token_id"], "payer_email": r["payer_email"],
                "simulated": r["simulated"], "env": r["env"], "connected_ts": r["connected_ts"], "revoked_ts": r["revoked_ts"],
                "revoked_by": r["revoked_by"], "note": r["note"], "waiting_for_paypal": bool(pending)}

    def purchase_block(self, agent_id):
        """Why a purchase must be refused because of the money source, or None. An agent
        that never connected PayPal on file isn't affected; once one was connected, its
        purchases need it to be live."""
        r = self.store.vault_current(agent_id)
        if not r or r["status"] == USABLE:
            return None
        if r["status"] == "revoked":
            return "This agent's saved PayPal payment method was revoked, so it can't be charged. Reconnect PayPal for this agent first."
        if r["status"] == "revoke_failed":
            return ("Revoking this agent's saved PayPal payment method didn't go through at PayPal, so Lowtides refuses its "
                    "purchases until it is revoked or reconnected.")
        return None

    # ---- connect -----------------------------------------------------------------------------
    def connect_start(self, agent_id):
        agent = self.store.agent(agent_id)
        if not agent:
            raise VaultError("No such agent.", 404)
        if self.simulated:
            tid = "SIM-VAULT-" + secrets.token_hex(5).upper()
            vid = self.store.add_vault(agent_id, setup_token_id="SIM-SETUP-" + secrets.token_hex(5).upper(), token_id=tid,
                                       status=USABLE, simulated=1, env="simulated", connected_ts=time.time(),
                                       note="Simulated: nothing was sent to PayPal.")
            self._replace_older(agent_id, vid)
            self.store.audit("vault.connected", str(agent_id), {"agent": agent["name"], "token_id": tid, "simulated": True})
            self.store.event(agent_id, "wallet", "PayPal on file connected for %s (simulated: nothing was sent to PayPal)."
                             % agent["name"])
            return {"status": USABLE, "simulated": True, "approve_url": None, "paypal_on_file": self.status(agent_id)}
        st = self.paypal.create_setup_token(
            self.base_url + "/paypal/return?kind=vault", self.base_url + "/paypal/cancel?kind=vault",
            "Lowtides: let AI agent %s pay from this PayPal" % agent["name"],
            request_id="setup-%d-%s" % (agent_id, secrets.token_hex(6)))
        sid = st.get("id")
        if not sid:
            raise VaultError("PayPal didn't return a setup token.", 502)
        self.store.add_vault(agent_id, setup_token_id=sid, status="pending", simulated=0, env=getattr(self.paypal, "env", ""),
                             customer_id=(st.get("customer") or {}).get("id", ""))
        self.store.audit("vault.setup_started", str(agent_id), {"agent": agent["name"], "setup_token_id": sid})
        return {"status": "waiting_for_paypal", "simulated": False, "setup_token_id": sid, "approve_url": self.paypal.approve_link(st)}

    def connect_finish(self, setup_token_id):
        row = self.store.vault_pending(setup_token_id=setup_token_id) if setup_token_id else None
        if not row:
            raise VaultError("Unknown PayPal connection. Start it again from the Wallet tab.", 404)
        if row["status"] == USABLE:
            return {"status": USABLE, "already": True, "agent_id": row["agent_id"]}
        if row["status"] != "pending":
            raise VaultError("This PayPal connection is already %s. Start a new one from the Wallet tab." % row["status"], 409)
        agent = self.store.agent(row["agent_id"]) or {"name": "agent %s" % row["agent_id"]}
        try:
            tok = self.paypal.create_payment_token(setup_token_id)
        except PayPalError as e:
            self.store.update_vault(row["id"], status="failed", note=e.summary()[:500])
            self.store.audit("vault.connect_failed", str(row["agent_id"]), {"setup_token_id": setup_token_id, "error": e.summary()[:500]})
            self.store.event(row["agent_id"], "wallet", "PayPal on file was not saved for %s: %s" % (agent["name"], e.summary()))
            raise VaultError("PayPal didn't save the payment method: %s" % e.summary(), 502)
        tid = tok.get("id")
        if not tid:
            self.store.update_vault(row["id"], status="failed", note="PayPal returned no payment token.")
            raise VaultError("PayPal returned no payment token.", 502)
        email = self.paypal.payer_email(tok)
        self.store.update_vault(row["id"], token_id=tid, status=USABLE, payer_email=email, connected_ts=time.time(),
                                customer_id=(tok.get("customer") or {}).get("id", "") or row["customer_id"], note="")
        self._replace_older(row["agent_id"], row["id"])
        self.store.audit("vault.connected", str(row["agent_id"]), {"agent": agent["name"], "token_id": tid, "payer_email": email,
                                                                   "env": row["env"], "simulated": False})
        self.store.event(row["agent_id"], "wallet", "PayPal on file connected for %s%s (PayPal %s payment token %s)."
                         % (agent["name"], " (" + email + ")" if email else "", row["env"] or "", tid))
        return {"status": USABLE, "agent_id": row["agent_id"], "token_id": tid, "payer_email": email}

    def _replace_older(self, agent_id, keep_id):
        """One live token per agent: a reconnect deletes the one it replaces."""
        for old in self.store.q("SELECT id FROM vault_tokens WHERE agent_id=? AND id<>? AND status IN ('active','revoke_failed')",
                                (agent_id, keep_id)):
            r = self.store.vault_row(old["id"])
            if not r["simulated"] and r["token_id"]:
                try:
                    self.paypal.delete_payment_token(r["token_id"])
                except PayPalError as e:
                    if e.status != 404:
                        self.store.event(agent_id, "wallet", "Couldn't delete the old saved PayPal token %s at PayPal (%s). "
                                                             "Remove it in PayPal if it is still listed." % (r["token_id"], e.summary()))
            self.store.update_vault(r["id"], status="revoked", revoked_ts=time.time(), revoked_by="replaced",
                                    note="Replaced by a new connection.")

    # ---- revoke ----------------------------------------------------------------------------------
    def revoke(self, agent_id, by="owner"):
        """Delete the agent's saved PayPal at PayPal. Returns None when there was nothing to
        revoke, else {"status": "revoked"|"revoke_failed", ...}. Never raises for a PayPal
        failure: the kill switch must keep going, and the failure is reported honestly."""
        r = self.store.vault_current(agent_id)
        if not r or r["status"] not in (USABLE, "revoke_failed"):
            return None
        agent = self.store.agent(agent_id) or {"name": "agent %s" % agent_id}
        note = ""
        if not r["simulated"]:
            err = None
            for attempt in (1, 2):
                try:
                    self.paypal.delete_payment_token(r["token_id"])
                    err = None
                    break
                except PayPalError as e:
                    err = e
                    if e.status == 404:  # already gone at PayPal: it can't be charged, which is the point
                        note = "PayPal had already removed it."
                        err = None
                        break
                    if e.retryable and attempt == 1:
                        time.sleep(self.retry_delay)
                        continue
                    break
            if err is not None:
                why = err.summary()[:400]
                self.store.update_vault(r["id"], status="revoke_failed", note=why)
                self.store.audit("vault.revoke_failed", str(agent_id), {"agent": agent["name"], "token_id": r["token_id"], "by": by,
                                                                        "error": why, "http": err.status, "debug_id": err.debug_id})
                msg = ("Couldn't revoke the saved PayPal payment method for %s: %s. Lowtides refuses its purchases anyway; "
                       "press Revoke on the Wallet tab to try again, or remove Lowtides under PayPal > Settings > Payments." % (agent["name"], why))
                self.store.event(agent_id, "kill", msg)
                return {"status": "revoke_failed", "token_id": r["token_id"], "simulated": False, "error": why, "message": msg}
        self.store.update_vault(r["id"], status="revoked", revoked_ts=time.time(), revoked_by=by,
                                note=note or ("Simulated: nothing was sent to PayPal." if r["simulated"] else ""))
        self.store.audit("vault.revoked", str(agent_id), {"agent": agent["name"], "token_id": r["token_id"], "by": by,
                                                          "simulated": r["simulated"], "env": r["env"], "note": note})
        line = REVOKED_LINE + (" (Simulated: nothing was sent to PayPal.)" if r["simulated"] else "")
        self.store.event(agent_id, "kill", "%s: %s" % (agent["name"], line))
        return {"status": "revoked", "token_id": r["token_id"], "simulated": r["simulated"], "message": line}

    # ---- charge the saved PayPal --------------------------------------------------------------------
    def topup(self, agent_id, amount):
        """Load the agent's wallet straight from its PayPal on file, no checkout page."""
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            raise VaultError("The top-up must be a dollar amount.")
        if amount < 1 or amount > 1000:
            raise VaultError("Top-ups must be between $1 and $1,000.")
        agent = self.store.agent(agent_id)
        if not agent:
            raise VaultError("No such agent.", 404)
        r = self.store.vault_current(agent_id)
        if not r or r["status"] != USABLE:
            raise VaultError("%s has no PayPal on file. Connect PayPal for this agent first." % agent["name"], 409)
        if r["simulated"]:
            ref = "vaulttopup:SIM-" + secrets.token_hex(6)
            self.store.wallet_move(agent_id, amount, "paypal on file (simulated)", ref=ref)
            self.store.event(agent_id, "wallet", "Wallet topped up with %s from PayPal on file (simulated: nothing was charged)."
                             % pricing.money(amount))
            return {"status": "COMPLETED", "simulated": True, "balance": self.store.balance(agent_id)}
        res = self.paypal.charge_vaulted(r["token_id"], amount, "AI allowance for %s" % agent["name"], "topup:%d" % agent_id,
                                         request_id="vtop-%d-%s" % (agent_id, secrets.token_hex(6)))
        oid = res.get("id", "")
        status, cap = res.get("status", ""), self.paypal.capture_status(res)
        if oid:
            self.store.add_topup(oid, agent_id, amount)
        if status == "COMPLETED" and cap in ("", "COMPLETED"):
            self.store.wallet_move(agent_id, amount, "paypal on file", ref="topup:" + oid)
            if oid:
                self.store.set_topup_status(oid, "COMPLETED")
            self.store.event(agent_id, "wallet", "Wallet topped up with %s from PayPal on file (order %s)." % (pricing.money(amount), oid))
        elif oid:
            status = "CAPTURE_" + cap if cap else (status or "UNKNOWN")
            self.store.set_topup_status(oid, status)
        return {"status": status, "order_id": oid, "simulated": False, "balance": self.store.balance(agent_id)}
