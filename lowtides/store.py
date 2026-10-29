"""SQLite storage. One file, no server, safe across request threads."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT DEFAULT '',
  markup_percent REAL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  daily_budget REAL NOT NULL,
  task_budget REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'on',
  client_id INTEGER,
  use_wallet INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  agent_id INTEGER NOT NULL,
  task_id TEXT,
  requested_model TEXT,
  used_model TEXT,
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  trimmed_tokens INTEGER DEFAULT 0,
  cost REAL DEFAULT 0,
  baseline_cost REAL DEFAULT 0,
  source TEXT DEFAULT 'model',
  escalations INTEGER DEFAULT 0,
  usage_estimated INTEGER DEFAULT 0,
  note TEXT DEFAULT '',
  client_id INTEGER
);
CREATE INDEX IF NOT EXISTS ledger_agent_ts ON ledger(agent_id, ts);
CREATE INDEX IF NOT EXISTS ledger_task ON ledger(task_id);
CREATE TABLE IF NOT EXISTS memory (
  key TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  api TEXT NOT NULL,
  text TEXT NOT NULL,
  response TEXT NOT NULL,
  baseline_cost REAL NOT NULL,
  created REAL NOT NULL,
  hits INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS memory_agent ON memory(agent_id, api);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  label TEXT DEFAULT '',
  budget REAL NOT NULL,
  quote_low REAL DEFAULT 0,
  quote_likely REAL DEFAULT 0,
  quote_high REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open',
  hold_order_id TEXT,
  hold_auth_id TEXT,
  hold_amount REAL DEFAULT 0,
  captured_amount REAL DEFAULT 0,
  paypal_status TEXT DEFAULT '',
  created REAL NOT NULL,
  closed REAL
);
CREATE TABLE IF NOT EXISTS wallets (
  agent_id INTEGER PRIMARY KEY,
  balance REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wallet_tx (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  agent_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS topups (
  order_id TEXT PRIMARY KEY,
  agent_id INTEGER NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  agent_id INTEGER,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id INTEGER NOT NULL,
  paypal_invoice_id TEXT,
  period_start REAL,
  period_end REAL,
  amount REAL,
  status TEXT,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  agent_id INTEGER NOT NULL,
  governed_model TEXT,
  baseline_model TEXT,
  governed_text TEXT,
  baseline_text TEXT,
  similarity REAL,
  shadow_cost REAL
);
CREATE TABLE IF NOT EXISTS webhooks_seen (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_requests (
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  agent_id INTEGER NOT NULL,
  task_id TEXT,
  amount REAL NOT NULL,
  merchant TEXT NOT NULL,
  item TEXT DEFAULT '',
  category TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  decision TEXT NOT NULL,
  status TEXT NOT NULL,
  reasons TEXT DEFAULT '[]',
  flags TEXT DEFAULT '[]',
  payee TEXT DEFAULT '',
  payout_batch_id TEXT,
  payout_item_id TEXT,
  payout_status TEXT DEFAULT '',
  simulated INTEGER DEFAULT 0,
  decided_by TEXT DEFAULT 'rules',
  decided_ts REAL,
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS spend_agent_ts ON spend_requests(agent_id, ts);
CREATE TABLE IF NOT EXISTS merchants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  paypal_email TEXT DEFAULT '',
  category TEXT DEFAULT '',
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT,
  data TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(ledger)").fetchall()}
        if "client_id" not in cols:  # databases made by earlier builds
            self.db.execute("ALTER TABLE ledger ADD COLUMN client_id INTEGER")
        acols = {r[1] for r in self.db.execute("PRAGMA table_info(agents)").fetchall()}
        if "rules" not in acols:
            self.db.execute("ALTER TABLE agents ADD COLUMN rules TEXT DEFAULT ''")
        self.db.commit()

    # ---- helpers ---------------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def run(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # ---- clients -----------------------------------------------------------
    def add_client(self, name, email="", markup_percent=0.0):
        return self.run("INSERT INTO clients(name,email,markup_percent,created) VALUES(?,?,?,?)",
                        (name.strip(), email.strip(), float(markup_percent), time.time()))

    def clients(self):
        return self.q("SELECT * FROM clients ORDER BY id")

    def client(self, cid):
        return self.one("SELECT * FROM clients WHERE id=?", (cid,))

    # ---- agents ------------------------------------------------------------
    def add_agent(self, name, daily_budget, task_budget, client_id=None, use_wallet=False):
        key = "lt-" + secrets.token_urlsafe(24)
        aid = self.run(
            "INSERT INTO agents(name,key,daily_budget,task_budget,status,client_id,use_wallet,created) VALUES(?,?,?,?,?,?,?,?)",
            (name.strip(), key, float(daily_budget), float(task_budget), "on", client_id, 1 if use_wallet else 0, time.time()))
        self.run("INSERT OR IGNORE INTO wallets(agent_id,balance) VALUES(?,0)", (aid,))
        return aid

    def agents(self):
        return self.q("SELECT * FROM agents ORDER BY id")

    def agent(self, aid):
        return self.one("SELECT * FROM agents WHERE id=?", (aid,))

    def agent_by_key(self, key):
        if not key:
            return None
        return self.one("SELECT * FROM agents WHERE key=?", (key,))

    def update_agent(self, aid, **fields):
        allowed = {"name", "daily_budget", "task_budget", "status", "client_id", "use_wallet", "rules"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(k + "=?")
                args.append(v)
        if sets:
            args.append(aid)
            self.run("UPDATE agents SET " + ",".join(sets) + " WHERE id=?", args)

    def delete_agent(self, aid):
        self.run("DELETE FROM agents WHERE id=?", (aid,))

    # ---- ledger ------------------------------------------------------------
    def record(self, **row):
        row.setdefault("ts", time.time())
        if "client_id" not in row and row.get("agent_id"):
            # Stamp the client at the time of the call, so reassigning an agent later
            # never moves old usage onto a different client's bill.
            a = self.agent(row["agent_id"])
            row["client_id"] = a["client_id"] if a else None
        cols = ",".join(row.keys())
        marks = ",".join("?" for _ in row)
        return self.run("INSERT INTO ledger(" + cols + ") VALUES(" + marks + ")", tuple(row.values()))

    def spend_since(self, agent_id, since):
        r = self.one("SELECT COALESCE(SUM(cost),0) AS s FROM ledger WHERE agent_id=? AND ts>=?", (agent_id, since))
        return r["s"] if r else 0.0

    def task_spend(self, task_id):
        r = self.one("SELECT COALESCE(SUM(cost),0) AS s FROM ledger WHERE task_id=?", (task_id,))
        return r["s"] if r else 0.0

    def ledger(self, agent_id=None, limit=500, since=None):
        sql = "SELECT l.*, a.name AS agent_name FROM ledger l LEFT JOIN agents a ON a.id=l.agent_id WHERE 1=1"
        args = []
        if agent_id:
            sql += " AND l.agent_id=?"
            args.append(agent_id)
        if since:
            sql += " AND l.ts>=?"
            args.append(since)
        sql += " ORDER BY l.id DESC LIMIT ?"
        args.append(int(limit))
        return self.q(sql, args)

    def totals(self, agent_id=None, since=None, until=None, client_id=None):
        sql = ("SELECT COALESCE(SUM(CASE WHEN source IN ('model','memory') THEN 1 ELSE 0 END),0) AS calls,"
               " COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(baseline_cost),0) AS baseline,"
               " COALESCE(SUM(CASE WHEN source='memory' THEN 1 ELSE 0 END),0) AS memory_hits,"
               " COALESCE(SUM(CASE WHEN source='blocked' THEN 1 ELSE 0 END),0) AS blocked,"
               " COALESCE(SUM(escalations),0) AS escalations,"
               " COALESCE(SUM(trimmed_tokens),0) AS trimmed_tokens,"
               " COALESCE(SUM(CASE WHEN used_model<>requested_model AND source='model' THEN 1 ELSE 0 END),0) AS rerouted,"
               " COALESCE(SUM(usage_estimated),0) AS estimated_rows,"
               " COUNT(DISTINCT task_id) AS tasks"
               " FROM ledger l LEFT JOIN agents a ON a.id=l.agent_id WHERE 1=1")
        args = []
        if agent_id:
            sql += " AND l.agent_id=?"
            args.append(agent_id)
        if client_id:
            # Bill by the client stamped on each call. Quality spot-checks are the
            # owner's own cost of proof, so they are never billed to a client.
            sql += " AND l.client_id=? AND l.source<>'shadow'"
            args.append(client_id)
        if since:
            sql += " AND l.ts>=?"
            args.append(since)
        if until:
            sql += " AND l.ts<?"
            args.append(until)
        t = self.one(sql, args)
        t["saved"] = max(0.0, t["baseline"] - t["cost"])
        return t

    # ---- answer memory -------------------------------------------------------
    def memory_get(self, key):
        return self.one("SELECT * FROM memory WHERE key=?", (key,))

    def memory_candidates(self, agent_id, api, since, prefix):
        return self.q("SELECT * FROM memory WHERE agent_id=? AND api=? AND created>=? AND text LIKE ? ORDER BY created DESC LIMIT 300",
                      (agent_id, api, since, prefix + "%"))

    def memory_put(self, key, agent_id, api, text, response, baseline_cost):
        self.run("INSERT OR REPLACE INTO memory(key,agent_id,api,text,response,baseline_cost,created,hits) VALUES(?,?,?,?,?,?,?,0)",
                 (key, agent_id, api, text, json.dumps(response), float(baseline_cost), time.time()))

    def memory_hit(self, key):
        self.run("UPDATE memory SET hits=hits+1 WHERE key=?", (key,))

    def memory_clear(self, agent_id=None):
        if agent_id:
            self.run("DELETE FROM memory WHERE agent_id=?", (agent_id,))
        else:
            self.run("DELETE FROM memory")

    # ---- tasks ---------------------------------------------------------------
    def add_task(self, agent_id, label, budget, quote=None, tid=None):
        tid = tid or ("t-" + secrets.token_hex(6))
        q = quote or {}
        self.run("INSERT OR IGNORE INTO tasks(id,agent_id,label,budget,quote_low,quote_likely,quote_high,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
                 (tid, agent_id, label, float(budget), q.get("low", 0), q.get("likely", 0), q.get("high", 0), "open", time.time()))
        return tid

    def task(self, tid):
        return self.one("SELECT * FROM tasks WHERE id=?", (tid,))

    def tasks(self, agent_id=None, limit=100):
        if agent_id:
            return self.q("SELECT * FROM tasks WHERE agent_id=? ORDER BY created DESC LIMIT ?", (agent_id, limit))
        return self.q("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,))

    def update_task(self, tid, **fields):
        allowed = {"status", "hold_order_id", "hold_auth_id", "hold_amount", "captured_amount", "paypal_status", "closed", "label", "budget"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(k + "=?")
                args.append(v)
        if sets:
            args.append(tid)
            self.run("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", args)

    # ---- wallet ----------------------------------------------------------------
    def balance(self, agent_id):
        r = self.one("SELECT balance FROM wallets WHERE agent_id=?", (agent_id,))
        return r["balance"] if r else 0.0

    def wallet_move(self, agent_id, amount, kind, ref=None):
        """Add (positive) or spend (negative). A ref makes the move idempotent."""
        with self.lock:
            if ref and self.db.execute("SELECT 1 FROM wallet_tx WHERE ref=?", (ref,)).fetchone():
                return False
            self.db.execute("INSERT OR IGNORE INTO wallets(agent_id,balance) VALUES(?,0)", (agent_id,))
            self.db.execute("UPDATE wallets SET balance=ROUND(balance+?, 8) WHERE agent_id=?", (float(amount), agent_id))
            self.db.execute("INSERT INTO wallet_tx(ts,agent_id,amount,kind,ref) VALUES(?,?,?,?,?)",
                            (time.time(), agent_id, float(amount), kind, ref))
            self.db.commit()
            return True

    def wallet_history(self, agent_id, limit=50):
        return self.q("SELECT * FROM wallet_tx WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit))

    def add_topup(self, order_id, agent_id, amount):
        self.run("INSERT OR REPLACE INTO topups(order_id,agent_id,amount,status,created) VALUES(?,?,?,?,?)",
                 (order_id, agent_id, float(amount), "CREATED", time.time()))

    def topup(self, order_id):
        return self.one("SELECT * FROM topups WHERE order_id=?", (order_id,))

    def set_topup_status(self, order_id, status):
        self.run("UPDATE topups SET status=? WHERE order_id=?", (status, order_id))

    # ---- events --------------------------------------------------------------------
    def event(self, agent_id, kind, message):
        return self.run("INSERT INTO events(ts,agent_id,kind,message) VALUES(?,?,?,?)", (time.time(), agent_id, kind, message))

    def events(self, limit=50):
        return self.q("SELECT e.*, a.name AS agent_name FROM events e LEFT JOIN agents a ON a.id=e.agent_id ORDER BY e.id DESC LIMIT ?", (limit,))

    # ---- invoices ----------------------------------------------------------------------
    def add_invoice(self, client_id, paypal_invoice_id, start, end, amount, status):
        return self.run("INSERT INTO invoices(client_id,paypal_invoice_id,period_start,period_end,amount,status,created) VALUES(?,?,?,?,?,?,?)",
                        (client_id, paypal_invoice_id, start, end, amount, status, time.time()))

    def invoices(self):
        return self.q("SELECT i.*, c.name AS client_name FROM invoices i LEFT JOIN clients c ON c.id=i.client_id ORDER BY i.id DESC")

    # ---- shadow tests --------------------------------------------------------------------
    def add_shadow(self, **row):
        row.setdefault("ts", time.time())
        cols = ",".join(row.keys())
        return self.run("INSERT INTO shadow(" + cols + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))

    def shadows(self, limit=50):
        return self.q("SELECT * FROM shadow ORDER BY id DESC LIMIT ?", (limit,))

    # ---- webhooks --------------------------------------------------------------------------
    def webhook_seen(self, wid):
        return bool(self.one("SELECT 1 AS x FROM webhooks_seen WHERE id=?", (wid,)))

    def webhook_done(self, wid):
        """Mark an event handled. Called only after it was processed successfully,
        so a failed event is retried by PayPal instead of being dropped as a duplicate."""
        self.run("INSERT OR IGNORE INTO webhooks_seen(id,ts) VALUES(?,?)", (wid, time.time()))

    def last_invoice(self, client_id):
        return self.one("SELECT * FROM invoices WHERE client_id=? AND status<>'failed' ORDER BY period_end DESC LIMIT 1", (client_id,))

    # ---- spend requests (agent purchases) ------------------------------------------------
    @staticmethod
    def _spend_row(r):
        if r:
            r["reasons"] = json.loads(r.get("reasons") or "[]")
            r["flags"] = json.loads(r.get("flags") or "[]")
            r["simulated"] = bool(r.get("simulated"))
        return r

    def add_spend(self, **row):
        row = dict(row, reasons=json.dumps(row.get("reasons", [])), flags=json.dumps(row.get("flags", [])))
        row.setdefault("ts", time.time())
        cols = ",".join(row.keys())
        self.run("INSERT INTO spend_requests(" + cols + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        return self.spend(row["id"])

    def update_spend(self, sid, **fields):
        for k in ("reasons", "flags"):
            if k in fields:
                fields[k] = json.dumps(fields[k])
        sets = ",".join(k + "=?" for k in fields)
        self.run("UPDATE spend_requests SET " + sets + " WHERE id=?", tuple(fields.values()) + (sid,))
        return self.spend(sid)

    def spend(self, sid):
        return self._spend_row(self.one("SELECT s.*, a.name AS agent_name FROM spend_requests s LEFT JOIN agents a ON a.id=s.agent_id "
                                        "WHERE s.id=?", (sid,)))

    def spends(self, status=None, agent_id=None, limit=100):
        sql, args = "SELECT s.*, a.name AS agent_name FROM spend_requests s LEFT JOIN agents a ON a.id=s.agent_id WHERE 1=1", []
        if status:
            sql += " AND s.status=?"
            args.append(status)
        if agent_id:
            sql += " AND s.agent_id=?"
            args.append(agent_id)
        sql += " ORDER BY s.ts DESC LIMIT ?"
        args.append(int(limit))
        return [self._spend_row(r) for r in self.q(sql, args)]

    def purchases_since(self, agent_id, since):
        """Money committed to purchases: paid, approved, or waiting on a person (so a
        burst of requests can't each squeeze under the limit while others wait)."""
        r = self.one("SELECT COALESCE(SUM(amount),0) AS s FROM spend_requests WHERE agent_id=? AND ts>=? "
                     "AND status IN ('paid','approved','pending')", (agent_id, since))
        return float(r["s"])

    def purchase_count_since(self, agent_id, since):
        return int(self.one("SELECT COUNT(*) AS n FROM spend_requests WHERE agent_id=? AND ts>=?", (agent_id, since))["n"])

    # ---- merchants ------------------------------------------------------------------------
    def merchants(self):
        return self.q("SELECT * FROM merchants ORDER BY name")

    def merchant(self, name):
        return self.one("SELECT * FROM merchants WHERE name=? COLLATE NOCASE", ((name or "").strip(),))

    def add_merchant(self, name, paypal_email="", category=""):
        self.run("INSERT INTO merchants(name,paypal_email,category,created) VALUES(?,?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET paypal_email=excluded.paypal_email, category=excluded.category",
                 (name.strip(), paypal_email.strip(), category.strip().lower(), time.time()))
        return self.merchant(name)

    # ---- tamper-evident audit log ----------------------------------------------------------
    @staticmethod
    def _audit_hash(prev, ts, kind, ref, data):
        raw = json.dumps([prev, round(ts, 6), kind, ref, data], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def audit(self, kind, ref, data):
        """Append-only, hash-chained: each entry includes the previous entry's hash, so
        editing or deleting any past entry breaks every hash after it."""
        with self.lock:
            last = self.db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = last[0] if last else "0" * 64
            ts = round(time.time(), 6)
            body = json.dumps(data, sort_keys=True, default=str)
            h = self._audit_hash(prev, ts, kind, ref, body)
            self.db.execute("INSERT INTO audit(ts,kind,ref,data,prev_hash,hash) VALUES(?,?,?,?,?,?)", (ts, kind, ref, body, prev, h))
            self.db.commit()
            return h

    def audit_entries(self, ref=None, limit=1000):
        if ref:
            return self.q("SELECT * FROM audit WHERE ref=? ORDER BY id", (ref,))
        return self.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))

    def audit_verify(self):
        prev, n = "0" * 64, 0
        for r in self.q("SELECT * FROM audit ORDER BY id"):
            n += 1
            if r["prev_hash"] != prev or self._audit_hash(prev, r["ts"], r["kind"], r["ref"], r["data"]) != r["hash"]:
                return {"ok": False, "count": n, "broken_at": r["id"]}
            prev = r["hash"]
        return {"ok": True, "count": n, "broken_at": None}
