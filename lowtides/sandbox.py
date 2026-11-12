"""Public demo only: one private sandbox per browser, plus per-IP rate limits.

Every browser that opens the public demo gets a signed `lt_demo` cookie. The first
dashboard call carrying it builds that visitor's own sandbox: an in-memory SQLite
store and its own engine, spend governor, money and outside watch (an App built on
them), seeded with the runaway-agent story. STOP ALL, Stop all, Reset and the guided
replay then touch only that visitor's sandbox.

Agent calls (/v1/...) carry an agent key, not a cookie; a key -> sandbox index sends
them to the sandbox that made the key. Requests with no cookie at all (curl, the
scenario CLI) keep using the shared server-wide demo, exactly as before.

Lowtides Passes: every sandbox signs with the root App's one key (the key published at
/.well-known/lowtides-pass-keys.json), and each new pass's id is indexed -> sandbox, so
a cookie-less POST /v1/pass/verify (a merchant, the /pass/ page) is answered by the
sandbox that issued the pass, with that sandbox's live Stop all / revoke state.

Bounds: at most `cap` live sandboxes (least recently used is evicted), each dropped
after `idle` seconds without a request, and at most `per_ip_hour` new sandboxes per
client IP per hour. No sandbox starts its own background thread: one sweeper thread
expires sandboxes and runs each simulated outside-bill check when it is due.
"""
import collections
import copy
import hashlib
import hmac
import os
import secrets
import threading
import time

from . import accounts
from .config import Config
from .store import Store

COOKIE = "lt_demo"
SANDBOX_DB_MB = 8          # most a single sandbox's database may grow to
PASSES_PER_SANDBOX = 200   # pass ids remembered per sandbox; older ones verify as gone
INTERNAL_HEADER = "X-Lowtides-Internal"


def _trim_heap():
    """Hand freed memory back to the OS after closing sandboxes (glibc only; harmless elsewhere)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class TooManySandboxes(Exception):
    pass


class _IndexedStore(Store):
    """A sandbox store that reports every new agent key, so agent calls find their sandbox."""

    def __init__(self, on_key):
        super().__init__(":memory:")
        # Small page cache (a sandbox holds a few hundred rows), and a hard size cap so one
        # visitor can't grow their sandbox without bound: past it, writes fail with an error.
        self.db.execute("PRAGMA cache_size=-256")
        self.db.execute("PRAGMA max_page_count=%d" % (SANDBOX_DB_MB * 256))  # 4 KB pages
        self._on_key = on_key

    def size(self):
        with self.lock:
            pages = self.db.execute("PRAGMA page_count").fetchone()[0]
            psize = self.db.execute("PRAGMA page_size").fetchone()[0]
        return pages * psize

    def add_agent(self, *a, **kw):
        aid = super().add_agent(*a, **kw)
        row = self.agent(aid)
        if row:
            self._on_key(row["key"])
        return aid


class Sandboxes:
    def __init__(self, root, cap=None, idle=None, per_ip_hour=None, seed=True):
        self.root = root
        self.cap = int(cap or os.environ.get("LOWTIDES_DEMO_SANDBOXES", 200))
        self.idle = float(idle or os.environ.get("LOWTIDES_DEMO_IDLE_SECONDS", 7200))
        self.per_ip_hour = int(per_ip_hour or os.environ.get("LOWTIDES_DEMO_NEW_PER_IP_HOUR", 120))
        # All sandbox databases together stay under this; the least recently used go first.
        self.budget = float(os.environ.get("LOWTIDES_DEMO_DB_BUDGET_MB", 160)) * 1024 * 1024
        self.seed = seed
        # Signs cookies. Set LOWTIDES_DEMO_SECRET so cookies stay valid across restarts;
        # otherwise a new one each start (old cookies then just get a fresh sandbox).
        env_secret = os.environ.get("LOWTIDES_DEMO_SECRET", "")
        self.secret = env_secret.encode() if len(env_secret) >= 16 else secrets.token_bytes(32)
        self.internal = secrets.token_urlsafe(32)      # lets the in-process story seeder skip rate limits
        self.base_data = copy.deepcopy(root.config.data)
        self.lock = threading.RLock()
        self.live = collections.OrderedDict()          # sid -> App, least recently used first
        self.seen = {}                                 # sid -> last request time
        self.ready = {}                                # sid -> Event set once the story is seeded
        self.keys = {}                                 # agent key -> sid
        self.pass_ids = {}                             # pass jti -> sid
        self.pass_order = collections.defaultdict(collections.deque)  # sid -> its jtis, oldest first
        self.made = collections.defaultdict(collections.deque)  # ip -> creation times (last hour)
        self._thread = None
        self._stop = threading.Event()
        self.graveyard = collections.deque()           # (dropped at, App) waiting to be closed
        self.grace = 30.0

    # ---- cookies ------------------------------------------------------------
    def _sig(self, sid):
        return hmac.new(self.secret, sid.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:32]

    def new_id(self):
        sid = secrets.token_urlsafe(18)
        return sid + "." + self._sig(sid)

    def parse(self, value):
        """The sandbox id from a cookie value, or None if it was not signed by this server."""
        sid, _, sig = (value or "").partition(".")
        if not sid or len(sid) > 64 or not hmac.compare_digest(sig.encode("utf-8", "replace"), self._sig(sid).encode()):
            return None
        return sid

    def internal_ok(self, given):
        return bool(given) and hmac.compare_digest(str(given).encode("utf-8", "replace"), self.internal.encode())

    # ---- lookup -------------------------------------------------------------
    def get(self, sid, ip="", wait=True, create=True):
        """The sandbox for `sid`, building (and seeding) it on first use."""
        now = time.time()
        built = False
        saved, read_ok, acct = None, True, accounts.is_account(sid)
        if create and acct and (sid not in self.live or now - self.seen.get(sid, now) > self.idle):
            read_ok, saved = accounts.fetch2(sid)   # a signed-in account: its saved state, read before taking the lock
        with self.lock:
            app = self.live.get(sid)
            if app is not None and now - self.seen.get(sid, now) > self.idle:
                self._drop(sid)
                app = None
            if app is not None:
                self.live.move_to_end(sid)
                self.seen[sid] = now
                ev = self.ready.get(sid)
            elif not create:
                return None
            else:
                q = self.made[ip]
                while q and q[0] < now - 3600:
                    q.popleft()
                if len(q) >= self.per_ip_hour:
                    raise TooManySandboxes()
                q.append(now)
                while len(self.live) >= self.cap:
                    self._drop(next(iter(self.live)))
                self._fit_budget(keep=0)
                app, restored = self._build(sid, saved)
                if acct:                           # decided here, under the lock, by the one request that builds
                    if read_ok and (restored or not saved):
                        accounts.unblock(sid)
                    else:
                        accounts.block(sid)        # storage unreadable: a temporary copy that is never saved over the real one
                self.live[sid] = app
                if restored:                   # its agents' keys and Passes route to this sandbox again
                    for row in app.store.q("SELECT key FROM agents"):
                        self.keys[row["key"]] = sid
                    try:
                        for row in app.store.q("SELECT jti FROM passes ORDER BY issued DESC LIMIT %d" % PASSES_PER_SANDBOX):
                            self.pass_ids[row["jti"]] = sid
                            self.pass_order[sid].appendleft(row["jti"])
                    except Exception:
                        pass
                self.seen[sid] = now
                ev = self.ready[sid] = threading.Event()
                built = True
        if built:
            try:  # seeded outside the lock; other requests for this sid wait on `ev`
                if self.seed and not restored:
                    app.reset_demo(seed_story=True)
                if accounts.is_account(sid) and not restored:
                    accounts.touch(sid)        # a new account: store its starting state
            finally:
                ev.set()
        elif wait and ev is not None:
            ev.wait(60)
        return app

    def by_key(self, key):
        if not key:
            return None
        with self.lock:
            sid = self.keys.get(key)
            app = self.live.get(sid) if sid else None
            if app is not None:
                self.live.move_to_end(sid)
                self.seen[sid] = time.time()
            return app

    def by_pass(self, jti):
        """The live sandbox that issued pass `jti`, or None. A merchant checking a pass does
        not count as the visitor using their sandbox, so this doesn't keep it alive."""
        with self.lock:
            sid = self.pass_ids.get(jti)
            app = self.live.get(sid) if sid else None
            if app is not None and time.time() - self.seen.get(sid, 0) > self.idle:
                return None  # idle past its time; the sweeper is about to drop it
            return app

    def _build(self, sid, saved=None):
        """A new sandbox App. With `saved` (an account's snapshot) it starts from that state. Returns (app, restored)."""
        from . import passport
        from .server import App
        root = self.root
        shared_key = dict(passport.book(root)._key())  # one signing key for the whole demo
        store = _IndexedStore(lambda key, sid=sid: self._index(key, sid))
        data = copy.deepcopy(self.base_data)
        conf = accounts.load_into(store, saved) if saved else None
        restored = conf is not None
        if restored:
            for k in accounts.CONFIG_KEYS:
                if k in conf:
                    data[k] = conf[k]
        app = App(Config(data), store, paypal=root.paypal, config_path=None,
                  public_demo=True)
        app.sandbox_id = sid
        app.demo_root = root                         # pass verify and keys are answered via the root
        app.pass_key = shared_key
        app.on_pass = lambda jti, sid=sid: self._index_pass(jti, sid)
        app.self_url = root.self_url
        app.loopback = root.loopback
        app.admin_token = ""
        app.claude_compiles = root.claude_compiles   # one Claude budget for the whole demo
        app.save_config = lambda: None               # visitor settings live in memory only
        app.seed_headers = {"Cookie": "%s=%s.%s" % (COOKIE, sid, self._sig(sid)), INTERNAL_HEADER: self.internal}
        app.on_reset = lambda sid=sid: self._forget_keys(sid)
        app.next_outside = time.time() + 60
        return app, restored

    def _fit_budget(self, keep=1):
        """Evict least recently used sandboxes while their databases together exceed the budget."""
        sizes = {sid: app.store.size() for sid, app in self.live.items()}
        total = sum(sizes.values())
        while total > self.budget and len(self.live) > keep:
            sid = next(iter(self.live))
            total -= sizes.get(sid, 0)
            self._drop(sid)

    def db_bytes(self):
        with self.lock:
            return sum(app.store.size() for app in self.live.values())

    def _index(self, key, sid):
        with self.lock:
            if sid in self.live:
                self.keys[key] = sid

    def _index_pass(self, jti, sid):
        with self.lock:
            if sid not in self.live:
                return
            self.pass_ids[jti] = sid
            q = self.pass_order[sid]
            q.append(jti)
            while len(q) > PASSES_PER_SANDBOX:
                self.pass_ids.pop(q.popleft(), None)

    def _forget_keys(self, sid):
        """Forget the sandbox's agent keys and pass ids (on reset and when it is dropped)."""
        with self.lock:
            for k in [k for k, s in self.keys.items() if s == sid]:
                del self.keys[k]
            for jti in self.pass_order.pop(sid, ()):
                self.pass_ids.pop(jti, None)

    def _drop(self, sid):
        app = self.live.pop(sid, None)
        self.seen.pop(sid, None)
        ev = self.ready.pop(sid, None)
        if ev:
            ev.set()
        self._forget_keys(sid)
        if app is not None and accounts.is_account(sid):
            accounts.save_dropped(sid, app)    # a signed-in account's unsaved changes are stored before it leaves memory
        if app is not None:
            # Closed a little later, not now: a request already in flight may still be using it.
            # Closing explicitly matters: SQLite gives its memory back far sooner than via GC.
            app.outside.stop()
            if getattr(app, "notify", None) is not None:
                app.notify.stop()  # its webhook delivery threads go with it
            self.graveyard.append((time.time(), app))
        self._bury()

    def _bury(self, now=None):
        now = now or time.time()
        closed = 0
        while self.graveyard and now - self.graveyard[0][0] > self.grace:
            _, app = self.graveyard.popleft()
            try:
                with app.store.lock:
                    app.store.db.close()
                closed += 1
            except Exception:
                pass
        return closed

    # ---- background ---------------------------------------------------------
    def sweep(self, now=None):
        """Drop idle sandboxes, forget old per-IP counts, and run due outside-bill checks."""
        now = now or time.time()
        due = []
        with self.lock:
            for sid in [s for s, t in self.seen.items() if now - t > self.idle]:
                self._drop(sid)
            for ip in [ip for ip, q in self.made.items() if not q or q[-1] < now - 3600]:
                del self.made[ip]
            self._fit_budget()
            if self._bury(now):
                _trim_heap()
            for app in self.live.values():
                if getattr(app, "next_outside", 0) <= now:
                    app.next_outside = now + max(60.0, float(app.outside.settings["poll_minutes"]) * 60)
                    due.append(app)
        for app in due:
            try:
                if app.outside.settings["enabled"]:
                    app.outside.check()
            except Exception:
                pass

    def start(self, every=30):
        if self._thread:
            return

        def loop():
            while not self._stop.wait(every):
                try:
                    self.sweep()
                    self.root.limiter.prune()
                except Exception:
                    pass
        self._thread = threading.Thread(target=loop, name="demo-sandboxes", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def count(self):
        with self.lock:
            return len(self.live)


class RateLimiter:
    """Token bucket per client IP: `rate` requests a second, bursts up to `burst`."""

    def __init__(self, rate=None, burst=None):
        self.rate = float(rate or os.environ.get("LOWTIDES_DEMO_RATE", 20))
        self.burst = float(burst or os.environ.get("LOWTIDES_DEMO_BURST", 80))
        self.lock = threading.Lock()
        self.buckets = {}  # ip -> [tokens, last]

    def allow(self, ip, now=None):
        now = now or time.monotonic()
        with self.lock:
            b = self.buckets.get(ip)
            if b is None:
                b = self.buckets[ip] = [self.burst, now]
            b[0] = min(self.burst, b[0] + (now - b[1]) * self.rate)
            b[1] = now
            if b[0] < 1:
                return False
            b[0] -= 1
            return True

    def prune(self, now=None):
        now = now or time.monotonic()
        with self.lock:
            full = self.burst / self.rate if self.rate else 0
            for ip in [ip for ip, b in self.buckets.items() if now - b[1] > full]:
                del self.buckets[ip]
