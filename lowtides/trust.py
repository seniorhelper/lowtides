"""Agent trust score, 0 to 100, worked out from the agent's own history in this Lowtides.

It is a HEURISTIC computed from records this server holds (the spend requests, the activity
log, the audit log and the Handshake records). It is not a network-wide reputation, it does
not follow an agent to other Lowtides servers, and it starts from nothing for a new agent.
The formula is written out in docs/TRUST-SCORE.md and in WEIGHTS below; both are checked by tests.

    score = 40
          + 6 for each purchase that was paid and kept       (the total from this is capped at +36)
          - 6 for each purchase a person denied at the approval step
          - 4 for each blocked attempt (a purchase the rules refused, or a Handshake the agent's
                own Pass or rules turned down)
          - 15 for each loop-guard pause
          - 5 for each paid purchase you rated thumbs down ("Was this what you wanted?")
    then limited to 0..100.

Events are applied in time order, so `history` shows the score after each one.
"""
import json

BASE = 40
KINDS = ("kept", "rated_down", "denied", "blocked", "loop")
WEIGHTS = {"kept": 6, "kept_cap": 36, "denied": -6, "blocked": -4, "loop": -15, "rated_down": -5}
GRADES = ((85, "A"), (70, "B"), (55, "C"), (40, "D"), (0, "F"))


def grade(score):
    for floor, letter in GRADES:
        if score >= floor:
            return letter
    return "F"


def _events(store, agent_id):
    """(ts, kind, note) for everything that moves the score."""
    out = []
    for r in store.q("SELECT ts, amount, merchant, status, decided_by, decision, flags FROM spend_requests WHERE agent_id=?", (agent_id,)):
        if r["status"] == "paid":
            out.append((r["ts"], "kept", "$%.2f at %s" % (r["amount"], r["merchant"])))
        elif r["status"] == "cancelled":    # only the owner's Stop all cancels a payout: the agent did nothing wrong, so it still counts as kept
            out.append((r["ts"], "kept", "$%.2f at %s (canceled by Stop all)" % (r["amount"], r["merchant"])))
        elif r["status"] == "denied":
            who = r["decided_by"] or "rules"
            if who == "rules":
                out.append((r["ts"], "blocked", "Rules refused $%.2f at %s" % (r["amount"], r["merchant"])))
            elif who != "stop switch":      # a person said no at the approval step
                out.append((r["ts"], "denied", "You said no to $%.2f at %s" % (r["amount"], r["merchant"])))
    for e in store.q("SELECT ts, message FROM events WHERE agent_id=? AND kind='loop'", (agent_id,)):
        out.append((e["ts"], "loop", "Loop guard paused the agent"))
    try:
        for h in store.q("SELECT ts, store_id FROM hs_handshakes WHERE agent_id=? AND executed=1 AND signal='no_match' "
                         "AND shopper_fail=1 AND spend_id IS NULL AND revoked=0", (agent_id,)):   # a purchase the rules refused is counted above
            out.append((h["ts"], "blocked", "A Handshake at %s was refused on the shopper side" % h["store_id"]))
        for h in store.q("SELECT rated_ts, store_id FROM hs_handshakes WHERE agent_id=? AND rating='down'", (agent_id,)):
            out.append((h["rated_ts"] or 0, "rated_down", "You rated a purchase at %s thumbs down" % h["store_id"]))
    except Exception:       # the Handshake tables do not exist yet
        pass
    # Same instant (a coarse clock, or the audit log's 6-decimal rounding): purchases first, then stops.
    order = {"kept": 0, "rated_down": 1, "denied": 2, "blocked": 3, "loop": 4}
    out.sort(key=lambda x: (round(x[0], 6), order[x[1]]))
    return out


def compute(store, agent_id):
    """{score, grade, factors:[{label,delta,detail}], history:[{at,score}]} for one agent."""
    import datetime
    score, kept_points = BASE, 0
    totals = {k: [0, 0] for k in KINDS}   # count, points
    history = []
    for ts, kind, _ in _events(store, agent_id):
        if kind == "kept":
            gain = max(0, min(WEIGHTS["kept"], WEIGHTS["kept_cap"] - kept_points))
            kept_points += gain
            delta = gain
        else:
            delta = WEIGHTS[kind]
        before = score
        score = max(0, min(100, score + delta))
        totals[kind][0] += 1
        totals[kind][1] += score - before
        history.append({"at": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "score": score})
    label = {"kept": "Purchases paid and kept", "denied": "Purchases you denied",
             "blocked": "Blocked attempts", "loop": "Loop-guard pauses",
             "rated_down": "Purchases you rated thumbs down"}
    detail = {"kept": "+%d each, up to +%d in total" % (WEIGHTS["kept"], WEIGHTS["kept_cap"]),
              "rated_down": "%d each", "denied": "%d each", "blocked": "%d each", "loop": "%d each"}
    factors = [{"label": "Starting point for every agent", "delta": BASE, "detail": "A new agent starts at %d." % BASE}]
    for k in KINDS:
        n, pts = totals[k]
        if n:
            d = detail[k] if k == "kept" else detail[k] % WEIGHTS[k]
            factors.append({"label": label[k], "delta": pts, "detail": "%d time%s (%s)" % (n, "" if n == 1 else "s", d)})
    if len(factors) == 1:
        factors.append({"label": "No history yet", "delta": 0, "detail": "Nothing bought, blocked or stopped so far."})
    return {"score": score, "grade": grade(score), "factors": factors, "history": history,
            "kind": "heuristic", "note": "A heuristic from this agent's history on this Lowtides server, not a network reputation."}


# ---------------------------------------------------------------------------------------------------
# Store trust: what the SHOPPER sees about a STORE. Also a heuristic, never a public rating.
#
#     start  = 75% of GuardPooch's 0..100 score for the store (so the shopper's own orders can move it)
#            + 4 for each order kept at this store                   (the total from this is capped at +12)
#            + 6 for each thumbs up the shopper gave here            (the total from this is capped at +12)
#            - 10 for each thumbs down
#            - 15 for each order pulled back (refund / canceled payout)
#     then limited to 0..100, then GuardPooch has the last word:
#            danger  => never above 39, badge "danger"
#            caution => never above 69, badge at most "careful"
#     badge: "trusted" needs a GuardPooch "safe" and a score of 70 or more; below 40 is "danger"; else "careful".
STORE_WEIGHTS = {"gp_share_pct": 75, "kept": 4, "kept_cap": 12, "up": 6, "up_cap": 12, "down": -10, "refund": -15,
                 "danger_max": 39, "caution_max": 69, "trusted_min": 70, "danger_below": 40}
STORE_NOTE = "From GuardPooch's checks and your own orders here. Not a public rating."
STORE_BADGE_WORD = {"trusted": "Trusted store", "careful": "Be careful", "danger": "Danger"}


def store_trust(verdict, gp_score, kept=0, up=0, down=0, refunds=0):
    """Pure function: {badge, score, factors:[{label,delta,detail}], kind, note}. Factors add up to score."""
    w = STORE_WEIGHTS
    gp_score = max(0, min(100, int(gp_score)))
    base = round(gp_score * w["gp_share_pct"] / 100.0)
    factors = [{"label": "GuardPooch's checks", "delta": base,
                "detail": "GuardPooch scored this store %d out of 100 (%s). Counted at %d%% so your own orders can move it."
                          % (gp_score, {"safe": "a real store", "caution": "be careful", "danger": "a fake store"}.get(verdict, verdict), w["gp_share_pct"])}]
    total = base

    def add(label, delta, detail):
        nonlocal total
        if delta:
            factors.append({"label": label, "delta": delta, "detail": detail})
            total += delta
    add("Orders you kept here", min(kept * w["kept"], w["kept_cap"]),
        "%d kept (+%d each, up to +%d)" % (kept, w["kept"], w["kept_cap"]) if kept else "")
    add("Thumbs up from you", min(up * w["up"], w["up_cap"]), "%d time%s (+%d each, up to +%d)" % (up, "" if up == 1 else "s", w["up"], w["up_cap"]) if up else "")
    add("Thumbs down from you", down * w["down"], "%d time%s (%d each)" % (down, "" if down == 1 else "s", w["down"]) if down else "")
    add("Orders pulled back (refunds)", refunds * w["refund"], "%d time%s (%d each)" % (refunds, "" if refunds == 1 else "s", w["refund"]) if refunds else "")
    clamped = max(0, min(100, total))
    if clamped != total:
        factors.append({"label": "Limited to 0 to 100", "delta": clamped - total, "detail": "The score never goes past 0 or 100."})
    score = clamped
    cap = w["danger_max"] if verdict == "danger" else w["caution_max"] if verdict == "caution" else None
    if cap is not None and score > cap:
        factors.append({"label": "GuardPooch has the last word", "delta": cap - score,
                        "detail": "A store GuardPooch calls %s can't score above %d, whatever you bought." % ("fake" if verdict == "danger" else "risky (be careful)", cap)})
        score = cap
    if verdict == "danger" or score < w["danger_below"]:
        badge = "danger"
    elif verdict == "safe" and score >= w["trusted_min"]:
        badge = "trusted"
    else:
        badge = "careful"
    if not (kept or up or down or refunds):
        factors.append({"label": "Your own history here", "delta": 0, "detail": "No orders or ratings yet at this store."})
    return {"badge": badge, "word": STORE_BADGE_WORD[badge], "score": score, "factors": factors, "kind": "heuristic", "note": STORE_NOTE}


def store_history(store, agent_id, store_id):
    """How this shopper's own orders at one store went: {kept, up, down, refunds}."""
    h = {"kept": 0, "up": 0, "down": 0, "refunds": 0}
    try:
        rows = store.q("SELECT h.rating AS rating, s.status AS status FROM hs_handshakes h JOIN spend_requests s ON s.id = h.spend_id "
                       "WHERE h.agent_id=? AND h.store_id=? AND h.executed=1", (agent_id, store_id))
    except Exception:       # the Handshake tables do not exist yet
        return h
    for r in rows:
        if r["status"] == "paid":
            h["kept"] += 1
            if r["rating"] == "up":
                h["up"] += 1
            elif r["rating"] == "down":
                h["down"] += 1
        elif r["status"] == "cancelled":    # canceled by the owner's Stop all: not the store's fault, still counts as kept
            h["kept"] += 1
    return h
