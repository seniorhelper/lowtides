"""Report this store: a signed fraud report the person can keep.

When a purchase (Handshake) or an action (Action Muzzle) is refused because the watchdog says the store or
business looks fake (a lookalike name, or no signed policy), the person can press "Report this store".

  * POST /api/reports {"handshake_id": "hs_..."} or {"action_id": "act_..."}, optional {"note": "..."}.
  * GET /api/reports lists the person's reports, newest first.
  * GET /api/reports/{id} returns one report signed with the same key the receipts use (handshake.sign_doc).

Honest limits: the demo stores are fictional, nothing is sent anywhere, and this module makes no network calls.
The report only points the person at the official places where real fraud is reported.
"""
import json
import re
import secrets
import time

from . import muzzle
from .handshake import HandshakeError, _iso, service, sign_doc

SCHEMA = """
CREATE TABLE IF NOT EXISTS rp_reports (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, decision_id TEXT NOT NULL, store_id TEXT NOT NULL, data TEXT NOT NULL,
  UNIQUE (kind, decision_id)
);
CREATE INDEX IF NOT EXISTS rp_reports_ts ON rp_reports(ts);
"""
TABLES = ("rp_reports",)
MAX_REPORTS = 50            # reports kept per sandbox
MAX_NOTE = 280
OFFICIAL = (
    {"name": "FTC", "label": "Report fraud to the FTC", "url": "https://reportfraud.ftc.gov/"},
    {"name": "IC3", "label": "Report internet crime to the FBI's IC3", "url": "https://www.ic3.gov/"},
)
DEMO_NOTE = ("Demo stores are fictional. Do not file a real report about them. "
             "Lowtides does not send anything to the authorities for you.")
NOT_REPORTABLE = "Only a store that GuardPooch refused can be reported."
USAGE = "Send JSON like {\"handshake_id\": \"hs_...\"} or {\"action_id\": \"act_...\"}, with an optional \"note\"."


def _ensure(app):
    if not getattr(app, "_reports_ready", False):
        with app.store.lock:
            app.store.db.executescript(SCHEMA)
            app.store.db.commit()
        app._reports_ready = True


def reset(app):
    _ensure(app)
    with app.store.lock:
        for t in TABLES:
            app.store.db.execute("DELETE FROM " + t)
        app.store.db.commit()


def _view(r):
    d = json.loads(r["data"])
    return {"id": r["id"], "ts": r["ts"], "store": d["store"], "reasons": d["reasons"],
            "decision": {"kind": r["kind"], "id": r["decision_id"]}, "note": d.get("note", ""),
            "evidence_url": d["evidence_url"], "report_url": "/api/reports/" + r["id"],
            "official": [dict(o) for o in OFFICIAL], "demo_note": DEMO_NOTE}


def _from_handshake(app, hid):
    svc = service(app)
    h = svc.public(svc.get(hid))
    gp = h.get("guardpooch") or {}
    checks = [c for c in (h.get("shopper_checks") or []) + (h.get("store_checks") or [])
              if c.get("id") == "guardpooch" and c.get("level") == "fail"]
    if h.get("signal") != "no_match" or not (checks or gp.get("verdict") == "danger"):
        raise HandshakeError(NOT_REPORTABLE, 409)
    reasons = [str(x) for x in (gp.get("reasons") or []) if x] or [c["detail"] for c in checks]
    st = h["store"]
    return st["id"], {"store": {"id": st["id"], "name": st["name"], "domain": st.get("domain") or ""}, "reasons": reasons,
                      "evidence_url": "/api/handshakes/%s/proof" % hid}


def _from_action(app, aid):
    a = muzzle._row(app, muzzle.get(app, aid))
    reasons = [c["detail"] for c in a["checks"] if c.get("side") == "guard" and c.get("level") == "fail"]
    if a["signal"] != "no_match" or not reasons:
        raise HandshakeError(NOT_REPORTABLE, 409)
    cid = a["counterparty"]["id"]
    store = {"id": cid, "name": a["counterparty"]["name"]}
    domain = (muzzle.COUNTERPARTIES.get(cid) or {}).get("domain")
    if domain:
        store["domain"] = domain
    return cid, {"store": store, "reasons": reasons, "evidence_url": a["receipt_url"]}


def create(app, d):
    """Save a report about a refused decision. Returns (report, created); the same decision is never reported twice."""
    _ensure(app)
    if not isinstance(d, dict):
        raise HandshakeError(USAGE)
    hid, aid, note = d.get("handshake_id"), d.get("action_id"), d.get("note")
    if (hid is None) == (aid is None):
        raise HandshakeError(USAGE)
    did = hid if hid is not None else aid
    if not isinstance(did, str):
        raise HandshakeError("'%s' must be text. %s" % ("handshake_id" if hid is not None else "action_id", USAGE))
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise HandshakeError("'note' must be text.")
    note = " ".join(note.split())
    if len(note) > MAX_NOTE:
        raise HandshakeError("'note' can be at most %d characters." % MAX_NOTE)
    kind = "handshake" if hid is not None else "action"
    if not re.fullmatch(r"hs_[0-9a-f]+" if kind == "handshake" else r"act_[0-9a-f]+", did):
        raise HandshakeError("No such Handshake." if kind == "handshake" else "No such action.", 404)
    with service(app).lock:
        store_id, data = _from_handshake(app, did) if kind == "handshake" else _from_action(app, did)
        old = app.store.one("SELECT * FROM rp_reports WHERE kind=? AND decision_id=?", (kind, did))
        if old:
            return _view(old), False
        if app.store.one("SELECT COUNT(*) AS n FROM rp_reports")["n"] >= MAX_REPORTS:
            raise HandshakeError("You have saved %d reports, which is the most this demo keeps. Reset the demo to start over." % MAX_REPORTS, 409)
        rid = "rep_" + secrets.token_hex(8)
        data["note"] = note
        app.store.run("INSERT INTO rp_reports(id, ts, kind, decision_id, store_id, data) VALUES(?,?,?,?,?,?)",
                      (rid, time.time(), kind, did, store_id, json.dumps(data)))
        app.store.audit("report.saved", rid, {"kind": kind, "decision": did, "store": store_id})
        return get(app, rid), True


def get(app, rid):
    _ensure(app)
    r = app.store.one("SELECT * FROM rp_reports WHERE id=?", (str(rid),))
    if not r:
        raise HandshakeError("No such report.", 404)
    return _view(r)


def recent(app):
    _ensure(app)
    return [_view(r) for r in app.store.q("SELECT * FROM rp_reports ORDER BY ts DESC, rowid DESC LIMIT ?", (MAX_REPORTS,))]


def signed(app, rid):
    """The report as a signed document: verifies with handshake.verify_doc against the published keys."""
    r = get(app, rid)
    body = {k: v for k, v in r.items() if k not in ("id", "ts")}
    return sign_doc(service(app).book, dict(body, type="lowtides.fraud_report", v=1, id=r["id"], at=_iso(r["ts"]), ts=r["ts"], simulated=True,
                                            sent_to_authorities=False))


# ---- HTTP ------------------------------------------------------------------------------------
def route(h, app, method, path):
    """/api/reports for the person's pages. Returns True when it answered."""
    mr = re.fullmatch(r"/api/reports/(rep_[0-9a-f]+)", path)
    if not ((method, path) in (("GET", "/api/reports"), ("POST", "/api/reports")) or (mr and method == "GET")):
        return False
    if not h._api_allowed(write=method != "GET"):
        return True
    try:
        if mr:
            h._send(200, signed(app, mr.group(1)), {"Cache-Control": "no-store"})
        elif method == "GET":
            h._send(200, {"reports": recent(app)}, {"Cache-Control": "no-store"})
        else:
            h._send(200, create(app, h._json_in())[0])
    except HandshakeError as e:
        h._send(e.http, {"error": str(e)})
    return True
