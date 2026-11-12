"""GuardPooch: the watchdog that checks a STORE is real before an AI shopper pays it.

The Pass protects the owner from the agent. GuardPooch protects the shopper from the store:
fake, lookalike and brand-new stores are the usual way an agent loses money. Four checks, all
plain code (no AI decides anything):

  domain_age        a domain a few days old is a red flag; a few weeks old is a yellow one.
  lookalike         the name or domain is a near-copy of a store we know (a typo-squat).
  policy_signature  the store publishes a Store Policy signed with a key we can verify.
  price             its prices are far below what the same products cost at known stores.

Score starts at 100 and loses points per finding. 90+ is "safe", 60-89 "caution", below 60
"danger". A lookalike combined with a very young domain is "danger" whatever the score.

The demo stores use fixed seed data, so the verdicts are identical every run. A real domain-age
lookup (RDAP, https://rdap.org) exists but only runs for domains that do NOT end in .example,
only when the config turns it on (`guardpooch.rdap: true`), and never in the public demo.
It fails soft: any problem means "unknown", never an exception, and results are cached.
"""
import datetime
import json
import re
import statistics
import time
import urllib.error
import urllib.request

from .stores import DOMAIN_AGE_DAYS

RDAP_URL = "https://rdap.org/domain/"
RDAP_TIMEOUT = 3.0
RDAP_TTL = 24 * 3600
_cache = {}          # domain -> (fetched_at, age_days or None)

SAFE_AT, CAUTION_AT = 90, 60
YOUNG_DAYS, NEW_DAYS = 30, 90


def lev(a, b):
    """Edit distance between two short strings."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _slug(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _label(domain):
    """The first part of a domain, without punctuation: 'harbor-hardwarre-deals.example' -> 'harborhardwarredeals'."""
    return _slug((domain or "").split(".")[0])


def looks_like(candidate, target, max_dist=2):
    """True when `candidate` is not `target` but contains something within `max_dist` edits of it
    (a typo-squat such as harborhardwarre inside harborhardwarredeals). Short names never match,
    so two unrelated short words are not called lookalikes."""
    if len(target) < 8 or candidate == target:
        return False
    n = len(target)
    for size in range(max(1, n - 1), n + 2):
        for i in range(0, max(0, len(candidate) - size) + 1):
            w = candidate[i:i + size]
            if w != target and lev(w, target) <= max_dist:
                return True
    return False


def lookalike_of(store, known):
    """The known store this one imitates, or None."""
    mine_d, mine_n = _label(store["domain"]), _slug(store["name"])
    for k in known:
        if k["id"] == store["id"] or k["domain"] == store["domain"]:
            continue
        kd, kn = _label(k["domain"]), _slug(k["name"])
        if looks_like(mine_d, kd) or looks_like(mine_n, kn) or looks_like(mine_d, kn):
            return k
    return None


def rdap_age_days(domain, fetch=None, now=None):
    """Age of a real domain in days from RDAP, or None. Cached; never raises."""
    now = time.time() if now is None else now
    hit = _cache.get(domain)
    if hit and now - hit[0] < RDAP_TTL:
        return hit[1]
    age = None
    try:
        if fetch is not None:
            data = fetch(RDAP_URL + domain)
        else:
            req = urllib.request.Request(RDAP_URL + domain, headers={"Accept": "application/rdap+json"})
            with urllib.request.urlopen(req, timeout=RDAP_TIMEOUT) as r:
                data = json.loads(r.read(200000).decode("utf-8", "replace"))
        for ev in data.get("events", []):
            if ev.get("eventAction") == "registration":
                born = datetime.datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                age = max(0, (datetime.datetime.now(datetime.timezone.utc) - born).days)
                break
    except Exception:       # offline, rate limited, bad JSON, anything: unknown
        age = None
    _cache[domain] = (now, age)
    return age


def domain_age_days(domain, rdap_enabled=False, fetch=None):
    """(days or None, source). Seeded .example domains use fixed data; others use RDAP only when enabled."""
    domain = (domain or "").lower().strip()
    if domain.endswith(".example"):
        return DOMAIN_AGE_DAYS.get(domain), "demo data"
    if rdap_enabled:
        return rdap_age_days(domain, fetch), "RDAP"
    return None, "not looked up"


def price_ratio(store_items, known_items):
    """Median of (this store's price / known stores' price) for products with the same title, or None."""
    ref = {}
    for it in known_items:
        ref.setdefault(it["title"].strip().lower(), []).append(float(it["price"]))
    ratios = []
    for it in store_items:
        prices = ref.get(it["title"].strip().lower())
        if prices:
            ratios.append(float(it["price"]) / statistics.median(prices))
    return (statistics.median(ratios), len(ratios)) if ratios else (None, 0)


def check(store, known, policy_signed, age_days=None, age_source="demo data", store_items=(), known_items=()):
    """Verdict for one store.

    store: {id, name, domain}; known: the known-good stores [{id, name, domain}];
    policy_signed: True only if the store's Store Policy carries a signature that verified;
    age_days: domain age (None = unknown); store_items / known_items: catalog rows with title and price.
    Returns {verdict, score, reasons:[str], checks:[{id,label,ok,detail,level}]}.
    """
    checks, score, reasons = [], 100, []

    def add(cid, label, level, detail, cost):
        nonlocal score
        checks.append({"id": cid, "label": label, "ok": level == "ok", "level": level, "detail": detail})
        if level != "ok":
            score -= cost
            reasons.append(detail)

    # 1. domain age
    if age_days is None:
        add("domain_age", "Domain age", "warn", "How old %s is could not be checked (%s)." % (store["domain"], age_source), 10)
    elif age_days < YOUNG_DAYS:
        add("domain_age", "Domain age", "fail", "%s was registered only %d days ago." % (store["domain"], age_days), 40)
    elif age_days < NEW_DAYS:
        add("domain_age", "Domain age", "warn", "%s is new: registered %d days ago." % (store["domain"], age_days), 15)
    else:
        add("domain_age", "Domain age", "ok", "%s was registered %s ago." % (store["domain"], _years(age_days)), 0)

    # 2. lookalike
    twin = lookalike_of(store, known)
    if twin:
        add("lookalike", "Lookalike name", "fail",
            "The name and domain look like a copy of %s (%s)." % (twin["name"], twin["domain"]), 35)
    else:
        add("lookalike", "Lookalike name", "ok", "Not a near-copy of any store we know.", 0)

    # 3. signed policy
    if policy_signed:
        add("policy_signature", "Signed Store Policy", "ok", "Publishes a Store Policy with a signature that verifies.", 0)
    else:
        add("policy_signature", "Signed Store Policy", "fail", "No signed Store Policy, so nobody vouches for its rules.", 20)

    # 4. prices too good to be true
    ratio, n = price_ratio(store_items, known_items)
    if ratio is None:
        add("price", "Prices vs. known stores", "ok", "No products to compare with known stores.", 0)
    elif ratio < 0.5:
        add("price", "Prices vs. known stores", "fail",
            "Prices are %d%% below what known stores charge for the same %d product%s." % (round((1 - ratio) * 100), n, "" if n == 1 else "s"), 25)
    elif ratio < 0.75:
        add("price", "Prices vs. known stores", "warn",
            "Prices are %d%% below known stores for the same products." % round((1 - ratio) * 100), 10)
    else:
        add("price", "Prices vs. known stores", "ok", "In line with known stores for the same %d product%s." % (n, "" if n == 1 else "s"), 0)

    score = max(0, min(100, score))
    verdict = "safe" if score >= SAFE_AT else ("caution" if score >= CAUTION_AT else "danger")
    by = {c["id"]: c["level"] for c in checks}
    if by["lookalike"] == "fail" and by["domain_age"] == "fail":
        verdict = "danger"
    return {"verdict": verdict, "score": score, "reasons": reasons or ["Looks like a real, established store."], "checks": checks}


def _years(days):
    if days >= 730:
        return "%d years" % (days // 365)
    if days >= 365:
        return "about a year"
    return "%d days" % days
