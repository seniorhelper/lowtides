/* Lowtides control centers: shared helpers for /shop/, /handshake/ and /merchants/.
   window.LT = { api, esc, usd, ... , renderHandshake, passCard, trustMeter, activity, killFlow, confirm }
   No framework, no build step. Every number shown comes from the running server's API. */
(function () {
  "use strict";
  if (window.LT) return;

  var RM = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  var SIG = {
    match: { label: "Both said yes", short: "Both said yes" },
    needs_yes: { label: "Needs your yes", short: "Needs your yes" },
    no_match: { label: "No match — no money moved", short: "No match" }
  };
  var LEVEL_WORD = { ok: "passed", ask: "needs a yes", fail: "says no" };

  function $(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function usd(n, dp) {
    var v = Number(n || 0);
    var d = dp == null ? (v % 1 === 0 && Math.abs(v) >= 1000 ? 0 : 2) : dp;
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function pct(r) { return Math.round(Number(r || 0) * 100) + "%"; }
  function tidy(t) { return String(t == null ? "" : t).replace(/\.{2,}/g, ".").replace(/\.;/g, ";"); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, RM.matches ? 0 : ms); }); }
  function debounce(fn, ms) {
    var t;
    return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }
  function when(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return "";
    var s = (Date.now() - d.getTime()) / 1000;
    if (s < 50) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return d.toLocaleDateString();
  }

  function api(method, path, body) {
    var opt = { method: method, credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          var e = new Error(j.error || ("The server said " + r.status + "."));
          e.status = r.status; e.body = j;
          throw e;
        }
        return j;
      });
    });
  }

  // Scout is created lazily by the first shopping route; the catalog call returns its id.
  var scoutP = null;
  function scout(force) {
    if (force || !scoutP) {
      scoutP = api("GET", "/api/catalog?max_price=0").then(function (r) { return r.agent; });
      scoutP.catch(function () { scoutP = null; });
    }
    return scoutP;
  }

  // ---------- sliders (same look as /site.js, but safe to set from code) ----------
  function syncSlider(input) {
    var min = parseFloat(input.min || "0"), max = parseFloat(input.max || "100"), v = parseFloat(input.value);
    input.style.setProperty("--lt-val", (max > min ? ((v - min) / (max - min)) * 100 : 0) + "%");
    var wrap = input.closest(".lt-slider");
    var out = wrap && wrap.querySelector("output");
    if (out) {
      var fmt = wrap.getAttribute("data-format");
      out.textContent = fmt === "deal" ? dealWord(v) : (wrap.getAttribute("data-prefix") || "") + input.value + (wrap.getAttribute("data-suffix") || "");
    }
  }
  function dealWord(v) { return v >= 67 ? "Best deals" : v >= 50 ? "Deals first" : v > 33 ? "Safest first" : "Safest stores"; }

  // ---------- pictures ----------
  var KEY_SVG = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle cx="16" cy="24" r="9" fill="none" stroke="currentColor" stroke-width="4.5"/><path d="M25 24h17m-6 0v7m-6-7v5" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/></svg>';
  var LOCK_SVG = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false"><path d="M15 22v-6a9 9 0 0 1 18 0v6" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/><rect x="9" y="22" width="30" height="20" rx="5" fill="currentColor"/><circle cx="24" cy="31" r="3" fill="#fff"/></svg>';
  // GuardPooch's picture for a Handshake: barking at a fake store, happy when both said yes, otherwise checking.
  function dogImg(hs) {
    var m = window.LowtidesMascot || {}, gp = ((hs || {}).guardpooch || {}).verdict;
    var k = gp === "danger" ? "bark" : ((hs || {}).signal === "match" ? "happy" : "alert");
    return m[k] ? '<img class="hsr-dog" src="' + m[k] + '" width="72" height="72" alt="">' : POOCH_SVG;
  }
  var POOCH_SVG = '<svg viewBox="0 0 120 120" aria-hidden="true" focusable="false"><circle cx="60" cy="60" r="58" fill="#fff"/>' +
    '<g class="hsr-head"><path d="M30 34c-8 6-12 22-8 34 4 4 10 2 14-4l4-24z" fill="#0D2A2E"/><path d="M90 34c8 6 12 22 8 34-4 4-10 2-14-4l-4-24z" fill="#0D2A2E"/>' +
    '<ellipse cx="60" cy="62" rx="28" ry="30" fill="#F4C27A"/><ellipse cx="60" cy="78" rx="16" ry="12" fill="#FFF3DD"/>' +
    '<circle cx="49" cy="58" r="4.5" fill="#0D2A2E"/><circle cx="71" cy="58" r="4.5" fill="#0D2A2E"/><circle cx="50.5" cy="56.5" r="1.4" fill="#fff"/><circle cx="72.5" cy="56.5" r="1.4" fill="#fff"/>' +
    '<ellipse class="hsr-nose" cx="60" cy="72" rx="6" ry="4.5" fill="#0D2A2E"/><path d="M54 81c3 3 9 3 12 0" fill="none" stroke="#0D2A2E" stroke-width="2.5" stroke-linecap="round"/></g>' +
    '<rect x="40" y="90" width="40" height="8" rx="4" fill="#1FB89A"/><circle cx="60" cy="101" r="5" fill="#E39A2D"/></svg>';
  var GP_WORD = { safe: "Real store", caution: "Be careful", danger: "Fake store" };
  function gpBadge(v) {
    v = v || "safe";
    return '<span class="cc-gp cc-gp--' + esc(v) + '" title="GuardPooch store check">GuardPooch: ' + esc(GP_WORD[v] || v) + "</span>";
  }
  // ---------- two-way trust badges ----------
  // The shopper sees the STORE's badge; the store sees the AI SHOPPER's. Both come from the running server.
  var TB_WORD = { trusted: "Trusted store", careful: "Be careful", danger: "Danger" };
  var STORE_NOTE = "From GuardPooch's checks and your own orders here. Not a public rating.";
  var curWho = "the shopper";
  var storeReg = {};      // store id -> { name, trust }  (trust may be {badge, score} only, until the factors are fetched)
  function regStore(id, name, tt) {
    if (!id || !tt) return;
    var old = storeReg[id] || {};
    var keep = !tt.factors && old.trust && old.trust.factors && old.trust.score === tt.score ? old.trust.factors : null;
    storeReg[id] = { name: name || old.name || id, trust: keep ? Object.assign({}, tt, { factors: keep }) : tt };
  }
  function regStores(list) { (list || []).forEach(function (s) { regStore(s.id, s.name, s.store_trust); }); }
  function storeBadge(id, prefix) {
    var rec = storeReg[id];
    if (!rec || !rec.trust) return "";
    var t = rec.trust, word = TB_WORD[t.badge] || t.badge;
    return '<button type="button" class="lt-tb lt-tb--' + esc(t.badge) + '" data-tb="store" data-store="' + esc(id) + '"' + (prefix ? ' data-prefix="' + esc(prefix) + '"' : "") +
      ' aria-haspopup="dialog" title="Tap to see why">' + (prefix ? esc(prefix) + ": " : "") + esc(word) + " <small>" + esc(t.score) + '</small><span class="cc-sr">, tap to see why</span></button>';
  }
  function paintStoreBadges(id, root) {
    Array.prototype.forEach.call((root || document).querySelectorAll('button[data-tb="store"][data-store="' + id + '"]'), function (b) {
      var h = storeBadge(id, b.getAttribute("data-prefix") || "");
      if (!h) return;
      var t = document.createElement("div");
      t.innerHTML = h;
      b.className = t.firstChild.className;
      b.innerHTML = t.firstChild.innerHTML;
    });
  }
  function refreshStores(agentId) {
    return api("GET", "/api/stores" + (agentId ? "?agent_id=" + encodeURIComponent(agentId) : "")).then(function (r) {
      regStores(r.stores);
      Object.keys(storeReg).forEach(function (id) { paintStoreBadges(id); });
      return r.stores || [];
    });
  }
  // The store's view of an AI shopper: its 0-100 score against the store's own minimum.
  function shopperLevel(score, min) {
    score = Number(score || 0); min = Number(min || 0);
    return score >= min + 10 ? { k: "trusted", w: "Trusted shopper" } : score >= min ? { k: "careful", w: "Meets your minimum" } : { k: "danger", w: "Below your minimum" };
  }
  function shopperBadge(agentId, name, score, min, storeName, noPrefix) {
    var lv = shopperLevel(score, min);
    return '<button type="button" class="lt-tb lt-tb--' + lv.k + '" data-tb="shopper" data-agent="' + esc(agentId) + '" data-name="' + esc(name || "") + '" data-score="' + esc(score) + '" data-min="' + esc(min) +
      '" data-store-name="' + esc(storeName || "your store") + '" aria-haspopup="dialog" title="Tap to see why">' + (name && !noPrefix ? esc(name) + ": trust " + esc(score) + " \u00b7 " : "") + esc(lv.w) +
      '<span class="cc-sr">, tap to see why</span></button>';
  }
  function factorsHtml(factors) {
    return '<ul class="cc-factors">' + (factors || []).map(function (f) {
      var d = Number(f.delta || 0);
      return '<li><span class="cc-delta ' + (d > 0 ? "up" : d < 0 ? "down" : "") + '">' + (d > 0 ? "+" : "") + d + "</span><span><b>" + esc(f.label) + "</b> " + esc(f.detail || "") + "</span></li>";
    }).join("") + "</ul>";
  }
  function infoDialog(title, headHtml, bodyHtml, opener) {
    var d = document.createElement("dialog");
    d.className = "cc-dialog cc-tp";
    d.setAttribute("aria-labelledby", "cc-tp-h");
    d.innerHTML = '<button type="button" class="cc-x" value="no" aria-label="Close">\u00d7</button><h2 id="cc-tp-h">' + esc(title) + "</h2>" + headHtml + bodyHtml +
      '<div class="cc-inline"><button type="button" class="lt-btn lt-btn--primary" value="no">Got it</button></div>';
    document.body.appendChild(d);
    var close = function () { if (d.open) d.close(); d.remove(); if (opener && document.body.contains(opener)) opener.focus(); };
    d.addEventListener("click", function (e) { if (e.target === d || e.target.closest("button[value]")) close(); });
    d.addEventListener("cancel", function (e) { e.preventDefault(); close(); });
    if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
    d.querySelector(".cc-x").focus();
    return d;
  }
  function openStoreTrust(b) {
    var id = b.getAttribute("data-store");
    var show = function () {
      var rec = storeReg[id];
      if (!rec) return;
      var t = rec.trust, word = TB_WORD[t.badge] || t.badge;
      infoDialog(rec.name + ": " + word, '<p class="cc-tp-score"><span class="lt-tb lt-tb--' + esc(t.badge) + '">' + esc(word) + "</span> " + esc(t.score) + ' <small class="cc-fine">out of 100</small></p>',
        factorsHtml(t.factors) + '<p class="cc-tp-note">' + esc(STORE_NOTE) + "</p>", b);
    };
    var rec = storeReg[id];
    if (rec && rec.trust && rec.trust.factors) show(); else refreshStores().then(show).catch(function () {});
  }
  function openShopperTrust(b) {
    var aid = b.getAttribute("data-agent"), name = b.getAttribute("data-name") || "This AI shopper", min = Number(b.getAttribute("data-min")), sname = b.getAttribute("data-store-name");
    api("GET", "/api/agents/" + encodeURIComponent(aid) + "/trust").then(function (t) {
      var lv = shopperLevel(t.score, min), gap = t.score - min;
      infoDialog(name + ": trust " + t.score, '<p class="cc-tp-score"><span class="lt-tb lt-tb--' + lv.k + '">' + esc(lv.w) + "</span> " + esc(t.score) + ' <small class="cc-fine">out of 100</small></p>' +
        "<p>" + esc(sname) + "\u2019s minimum is <b>" + esc(min) + "</b>. " + esc(name) + " is " + (gap === 0 ? "exactly at it" : Math.abs(gap) + " point" + (Math.abs(gap) === 1 ? "" : "s") + (gap > 0 ? " above it" : " below it")) +
        ". \u201cTrusted shopper\u201d means " + (min + 10) + " or more.</p>",
        factorsHtml(t.factors) + '<p class="cc-tp-note">' + esc(t.note || "A heuristic from this agent's history on this Lowtides server, not a network reputation.") + "</p>", b);
    }).catch(function (e) { b.title = e.message; });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("button[data-tb]");
    if (!b) return;
    e.preventDefault();
    if (b.getAttribute("data-tb") === "shopper") openShopperTrust(b); else openStoreTrust(b);
  });

  function sigPill(signal, text) {
    return '<span class="sig sig--' + esc(signal) + '">' + esc(text || (SIG[signal] || {}).label || signal) + "</span>";
  }

  // ---------- the Handshake panel (hero visual) ----------
  function checkLi(c) {
    return '<li class="hsr-chk" data-on="0" data-level="' + esc(c.level || (c.ok ? "ok" : "fail")) + '">' +
      '<span class="hsr-mark" aria-hidden="true">' + ({ ok: "✓", ask: "!", fail: "✕" }[c.level] || "✓") + "</span>" +
      "<span><b>" + esc(c.label) + ' <span class="cc-sr">(' + esc(LEVEL_WORD[c.level] || "") + ")</span></b><small>" + esc(tidy(c.detail)) + "</small></span></li>";
  }
  function plainFails(hs, level) {
    var storeName = (hs.store && hs.store.name) || "the store";
    var out = [];
    (hs.shopper_checks || []).forEach(function (c) {
      if (c.level !== level) return;
      if (c.id === "guardpooch") {   // one line per GuardPooch finding reads better than one long sentence
        String(c.detail || "").replace(/^GuardPooch:\s*/, "").split(/;\s*/).forEach(function (d) { if (d) out.push({ side: "GuardPooch", c: { detail: d } }); });
      } else out.push({ side: "Your rules", c: c });
    });
    (hs.store_checks || []).forEach(function (c) { if (c.level === level) out.push({ side: poss(storeName) + " rules", c: c }); });
    var gp = hs.guardpooch || {};
    if (level === "fail" && gp.verdict === "danger" && !out.some(function (x) { return x.c.id === "guardpooch"; })) {
      out.push({ side: "GuardPooch", c: { detail: (gp.reasons || []).join(" ") } });
    }
    return out;
  }
  function idleHandshake(host, text) {
    host.innerHTML = '<div class="hsr"><div class="hsr-out hsr-idle" role="status" aria-live="polite">' + esc(text) + "</div></div>";
  }

  /* host: element; hs: POST /api/handshake response.
     opts: { onBuy(hs), onAsk(hs), onApprove(hs, btn), onDeny(hs, btn), focus: bool, intro: string } */
  function renderHandshake(host, hs, opts) {
    opts = opts || {};
    var storeName = (hs.store && hs.store.name) || "Store";
    var item = hs.item || {};
    var gp = hs.guardpooch || {};
    var who = (hs.agent && hs.agent.name) || "Scout";
    var root = document.createElement("div");
    root.className = "hsr is-sniffing";
    root.setAttribute("data-gp", gp.verdict || "safe");
    root.setAttribute("data-store", (hs.store && hs.store.id) || "");
    root.setAttribute("data-agent-name", who);
    curWho = who;
    if (hs.trust && hs.trust.score != null) root.setAttribute("data-agent-trust", hs.trust.score);
    if (hs.store_trust && hs.store && hs.store.id) regStore(hs.store.id, storeName, hs.store_trust);
    root.innerHTML =
      '<p class="hsr-title">' + (opts.intro ? esc(opts.intro) + " " : "") + "<b>" + esc((hs.agent && hs.agent.name) || "Scout") + "</b> wants <b>" + esc(item.title || "an item") + "</b> for <b>" + usd(item.amount) + "</b> from <b>" + esc(storeName) + "</b>" + (hs.executed ? "" : " (a check only)") + ".</p>" +
      '<div class="hsr-cols">' +
      '<section class="hsr-side hsr-you" aria-label="Your rules, the key"><div class="hsr-side-h">' + KEY_SVG + "<div><h3>Your rules</h3><p>the key: " + esc(poss(who)) + " signed Pass</p></div></div>" +
      '<ol class="hsr-list">' + (hs.shopper_checks || []).map(checkLi).join("") + "</ol></section>" +
      '<div class="hsr-mid">' + dogImg(hs) + '<p class="hsr-gp">GuardPooch is sniffing<br><span class="cc-mono">' + wbr((hs.store && hs.store.domain) || "") + '</span></p><span class="hsr-bark" aria-hidden="true">WOOF!</span></div>' +
      '<section class="hsr-side hsr-store" aria-label="' + esc(poss(storeName)) + ' rules, the lock"><div class="hsr-side-h">' + LOCK_SVG + "<div><h3>" + esc(poss(storeName)) + " rules</h3><p>" + (((hs || {}).guardpooch || {}).verdict === "danger" ? "the lock: no signed Store Policy, so these are unverified" : "the lock: its signed Store Policy") + "</p>" + (hs.store_trust && hs.store ? storeBadge(hs.store.id) : "") + "</div></div>" +
      '<ol class="hsr-list">' + (hs.store_checks || []).map(checkLi).join("") + "</ol></section>" +
      "</div>" +
      '<div class="hsr-out" role="status" aria-live="polite" tabindex="-1"><p class="hsr-idle" style="margin:0">Checking both sides…</p></div>';
    host.innerHTML = "";
    host.appendChild(root);

    var you = root.querySelectorAll(".hsr-you .hsr-chk"), st = root.querySelectorAll(".hsr-store .hsr-chk");
    var order = [];
    for (var i = 0; i < Math.max(you.length, st.length); i++) { if (you[i]) order.push(you[i]); if (st[i]) order.push(st[i]); }

    var p = Promise.resolve();
    order.forEach(function (li) { p = p.then(function () { li.setAttribute("data-on", "1"); return sleep(130); }); });
    return p.then(function () { return sleep(250); }).then(function () {
      root.classList.remove("is-sniffing");
      var gpLine = root.querySelector(".hsr-gp");
      gpLine.innerHTML = gpBadge(gp.verdict) + '<br><span class="cc-mono cc-dom">' + wbr((hs.store && hs.store.domain) || "") + "</span>";
      root.classList.add("is-done", "sig--" + hs.signal);
      var out = root.querySelector(".hsr-out");
      out.innerHTML = outcomeHtml(hs);
      Array.prototype.forEach.call(out.querySelectorAll(".cc-rate-who"), function (el) { el.textContent = who; });
      wireOutcome(out, hs, opts);
      if (opts.focus) out.focus({ preventScroll: true });
      return hs;
    });
  }

  // One wording for a paid order everywhere: a simulation never says "through PayPal sandbox".
  function payLine(amount, simulated) {
    return simulated ? "Simulated payment: " + usd(amount) + " (nothing was sent to PayPal)" : "Paid " + usd(amount) + " through PayPal sandbox";
  }
  function rateHtml(id, rating) {
    if (!id) return "";
    return '<div class="cc-rate" role="group" aria-label="Was this what you wanted?"><b>Was this what you wanted?</b>' +
      '<button type="button" class="cc-thumb" data-rate="up" data-hs="' + esc(id) + '" aria-pressed="' + (rating === "up") + '" aria-label="Yes, thumbs up">\ud83d\udc4d</button>' +
      '<button type="button" class="cc-thumb" data-rate="down" data-hs="' + esc(id) + '" aria-pressed="' + (rating === "down") + '" aria-label="No, thumbs down">\ud83d\udc4e</button>' +
      '<p class="cc-rate-note">Your rating updates <span class="cc-rate-who">' + esc(curWho) + '</span>\u2019s trust score and this store\u2019s trust badge.</p>' +
      '<small aria-live="polite">' + (rating ? "Saved. You can change it." : "One tap. A thumbs down lowers the shopper\u2019s trust score a little.") + "</small></div>";
  }
  function rateClick(b, opts) {
    var box = b.closest(".cc-rate"), v = b.getAttribute("data-rate");
    api("POST", "/api/handshakes/" + encodeURIComponent(b.getAttribute("data-hs")) + "/rating", { rating: v }).then(function (r) {
      Array.prototype.forEach.call(box.querySelectorAll("[data-rate]"), function (x) { x.setAttribute("aria-pressed", String(x === b)); });
      var panel = box.closest(".hsr"), sid = panel && panel.getAttribute("data-store"), who = (panel && panel.getAttribute("data-agent-name")) || "The shopper";
      var oldS = sid && storeReg[sid] ? storeReg[sid].trust : null, oldA = panel && panel.getAttribute("data-agent-trust");
      var line = v === "up" ? "Thanks, saved." : "Saved.";
      if (r.agent_trust) {
        line += " " + who + "\u2019s trust score " + (oldA != null && oldA !== "" && Number(oldA) !== r.agent_trust.score ? "went from " + oldA + " to " : "is now ") + r.agent_trust.score + ".";
        if (panel) panel.setAttribute("data-agent-trust", r.agent_trust.score);
      }
      if (r.store_trust && sid) {
        regStore(sid, storeReg[sid] && storeReg[sid].name, r.store_trust);
        paintStoreBadges(sid);
        line += " " + ((storeReg[sid] || {}).name || "The store") + " is now \u201c" + (TB_WORD[r.store_trust.badge] || r.store_trust.badge) + "\u201d at " + r.store_trust.score +
          (oldS && oldS.score !== r.store_trust.score ? " (was " + oldS.score + ")" : "") + ".";
      }
      box.querySelector("small").textContent = line;
      if (opts && opts.onRated) opts.onRated(v, r);
    }).catch(function (e) { box.querySelector("small").textContent = "Not saved: " + e.message; });
  }

  function outcomeHtml(hs) {
    var who = (hs.agent && hs.agent.name) || "Scout";
    var storeName = (hs.store && hs.store.name) || "the store";
    var item = hs.item || {};
    var h = sigPill(hs.signal);
    var proof = '<a class="lt-btn lt-btn--ghost" href="/api/handshakes/' + encodeURIComponent(hs.id) + '/proof" download="lowtides-proof-' + esc(hs.id) + '.json">Download proof pack</a>';
    if (hs.signal === "match") {
      h += "<h3>" + esc(hs.headline || "") + "</h3>";
      if (hs.order) {
        var o = hs.order;
        h += '<div class="hsr-pay"><b>' + esc(payLine(o.amount != null ? o.amount : item.amount, o.simulated)) + "</b>" +
          "<span>" + (o.simulated ? "Reference" : "PayPal sandbox id") + ': <span class="cc-mono">' + esc(o.paypal_order_id || "") + "</span></span>" +
          (o.simulated ? "<span>This payment was simulated and is labeled so: nothing was sent to PayPal. With the PayPal sandbox funded, the same step makes a real sandbox payout.</span>"
                       : "<span>Real PayPal sandbox call (test money). Status: " + esc(o.status || "") + ".</span>") + "</div>";
      } else if (!hs.executed) {
        h += "<p>This was a check only. No money moved.</p>";
      }
      if (hs.order && hs.executed && hs.order.status !== "cancelled") h += rateHtml(hs.id, hs.rating);
      if (hs.trust && hs.trust.before != null && hs.trust.score !== hs.trust.before) {
        h += "<p>" + esc(poss(who)) + " trust score went from <b>" + esc(hs.trust.before) + "</b> to <b>" + esc(hs.trust.score) + "</b> (a heuristic from its own history).</p>";
      }
      h += '<div class="hsr-acts">' + (!hs.executed ? '<button type="button" class="lt-btn lt-btn--primary" data-act="buy">Buy with PayPal sandbox</button>' : "") + proof + "</div>";
    } else if (hs.signal === "needs_yes") {
      h += "<h3>Nothing is forbidden, but a person has to say yes first.</h3><ul>" +
        plainFails(hs, "ask").map(function (x) { return "<li><b>" + esc(x.side) + ":</b> " + esc(x.c.detail) + "</li>"; }).join("") + "</ul>";
      if (hs.approval_id) {
        h += "<p>Waiting for you. Nothing is paid until you tap Approve.</p>" +
          '<div class="hsr-acts"><button type="button" class="lt-btn lt-btn--primary" data-act="approve">Approve ' + usd(item.amount) + "</button>" +
          '<button type="button" class="lt-btn lt-btn--danger-ghost" data-act="deny">Deny</button>' + proof + "</div>";
      } else {
        h += "<p>This was a check only. No money moved.</p>" +
          '<div class="hsr-acts"><button type="button" class="lt-btn lt-btn--amber" data-act="ask">Request my yes (hold it)</button>' + proof + "</div>";
      }
    } else {
      var fails = plainFails(hs, "fail");
      h += "<h3>" + esc(who) + " did not buy it. Nothing was sent to PayPal.</h3><ul>" +
        fails.map(function (x) { return "<li><b>" + esc(x.side) + ":</b> " + esc(tidy(x.c.detail)) + "</li>"; }).join("") + "</ul>" +
        '<div class="hsr-acts">' + proof + '<a href="#" class="cc-ask" data-ask="Why was that blocked?">Ask why it was blocked</a></div>';
    }
    h += '<p class="cc-fine" style="margin:12px 0 0">Handshake <span class="cc-mono">' + esc(hs.id) + "</span>, signed receipt kept in the audit log." + (hs.receipt && hs.receipt.kid ? " Key " + esc(hs.receipt.kid) + "." : "") + "</p>";
    return h;
  }

  function wireOutcome(out, hs, opts) {
    out.addEventListener("click", function (ev) {
      var t = ev.target.closest("button[data-rate]");
      if (t) { rateClick(t, opts); return; }
      var b = ev.target.closest("button[data-act]");
      if (!b) return;
      var act = b.getAttribute("data-act");
      var fn = { buy: opts.onBuy, ask: opts.onAsk, approve: opts.onApprove, deny: opts.onDeny }[act];
      if (fn) fn(hs, b);
    });
  }

  // Approve / deny a held purchase with the same routes the dashboard uses.
  function decide(spendId, act) {
    return api("POST", "/api/spend/" + encodeURIComponent(spendId) + "/" + act, act === "deny" ? { note: "Denied from the shopper control center." } : {});
  }
  function decisionHtml(r, act, hsId) {
    if (act === "deny") return sigPill("no_match", "Denied") + "<h3>Denied. " + usd(r.amount) + " to " + esc(r.merchant || "the store") + " will not be paid.</h3>";
    var sim = r.simulated || r.payout_status === "SIMULATED";
    return sigPill("match", "You said yes") + "<h3>" + (r.status === "paid" ? "Approved and paid. " : "Approved. ") + esc(r.status === "paid" ? payLine(r.amount, sim) : "Paying " + usd(r.amount) + " through PayPal sandbox") + " to " + esc(r.merchant || "the store") + ".</h3>" +
      '<div class="hsr-pay"><span>' + (sim ? "Reference" : "PayPal sandbox id") + ': <span class="cc-mono">' + esc(r.payout_batch_id || r.paypal_order_id || "pending") + "</span></span>" +
      (sim ? "<span>Simulated and labeled so: nothing was sent to PayPal.</span>" : "<span>Real PayPal sandbox call (test money).</span>") + "</div>" +
      (r.status === "paid" ? rateHtml(hsId) : "");
  }

  // ---------- Pass card ----------
  function passState(r) {
    var p = (r && r.pass) || {};
    if (p.revoked || !r || !r.pass) return "REVOKED";
    if (r.agent_status === "paused") return "HELD";
    if (!p.valid) return "REVOKED";
    return "VALID";
  }
  function passCard(host, passResp, prefs, name) {
    name = name || ((passResp && passResp.pass && passResp.pass.agent) || {}).name || "Scout";
    var st = passState(passResp);
    var p = (passResp && passResp.pass) || {};
    var pr = prefs || {};
    var off = st === "VALID" && pr.mode === "off";
    host.setAttribute("data-state", off ? "OFF" : st);
    var why = st === "REVOKED" ? (p.revoked_reason || "This Pass is not valid.") + " It is now refused at every store." :
      st === "HELD" ? "Held: " + name + " is paused, so stores refuse the Pass until you resume it." :
      off ? name + " is switched Off, so the Pass can\u2019t be used to buy anything. Switch it back on any time." :
      pr.mode === "assist" ? "Assist mode: " + name + " asks you before any money moves." : "";
    if (off) st = "OFF";
    host.innerHTML =
      '<div class="cc-pass-top"><span class="cc-pass-name">' + esc(name) + '\u2019s Pass</span><span class="cc-pass-state">' + st + "</span></div>" +
      (why ? '<p class="cc-pass-why">' + esc(why) + "</p>" : "<p>Signed with Ed25519. Every store checks it before " + esc(name) + " can pay.</p>") +
      "<dl><div><dt>Most per item</dt><dd>" + usd(pr.per_item_cap, 0) + "</dd></div><div><dt>Daily budget</dt><dd>" + usd(pr.daily_cap, 0) + "</dd></div>" +
      "<div><dt>Ask me above</dt><dd>" + usd(pr.ask_above, 0) + "</dd></div><div><dt>Never buys</dt><dd style=\"font-size:var(--lt-fs-md)\">" + esc((pr.never_buy || []).join(", ") || "nothing listed") + "</dd></div></dl>" +
      '<p class="cc-pass-id">Pass ' + esc(p.jti || "none") + (p.expires_at ? " · expires " + esc(new Date(p.expires_at).toLocaleString()) : "") + "</p>" +
      '<p style="margin:0"><a href="/pass/">Check a Pass yourself</a></p>';
    return st;
  }

  // ---------- trust meter ----------
  function trustMeter(host, t, stores, name) {
    name = name || "Scout";
    var s = Math.max(0, Math.min(100, Number(t.score || 0)));
    var marks = (stores || []).filter(function (x) { return x.policy_signed && x.policy && x.policy.min_trust != null; })
      .sort(function (a, b) { return a.policy.min_trust - b.policy.min_trust; });
    host.innerHTML =
      '<div class="cc-trust-top"><span class="cc-trust-score">' + s + '</span><span class="cc-trust-grade">out of 100 · grade ' + esc(t.grade || "") + "</span></div>" +
      '<div class="cc-meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + s + '" aria-label="' + esc(name) + '\u2019s trust score"><div class="cc-meter-fill" style="width:' + s + '%"></div>' +
      marks.map(function (x, i) {
        return '<span class="cc-meter-mark' + (i % 2 ? " is-high" : "") + '" style="left:' + x.policy.min_trust + '%"><span>' + esc(x.name.split(" ")[0]) + " " + x.policy.min_trust + "</span></span>";
      }).join("") + "</div>" +
      '<ul class="cc-factors">' + (t.factors || []).map(function (f) {
        var d = Number(f.delta || 0);
        return '<li><span class="cc-delta ' + (d > 0 ? "up" : d < 0 ? "down" : "") + '">' + (d > 0 ? "+" : "") + d + "</span><span><b>" + esc(f.label) + "</b> " + esc(f.detail || "") + "</span></li>";
      }).join("") + "</ul>" +
      '<p class="cc-fine" style="margin:0">A heuristic from ' + esc(poss(name)) + ' own history on this Lowtides server, not a credit check or a network reputation. Marks show each demo store’s minimum.</p>';
  }

  // ---------- activity ----------
  function activity(host, list) {
    if (!list || !list.length) {
      host.innerHTML = '<li class="cc-empty" style="display:block">No Handshakes yet. Ask your AI shopper to shop and they show up here.</li>';
      return;
    }
    host.innerHTML = list.map(function (h) {
      var it = h.item || {};
      var o = h.order, ap = h.approval && h.approval.status, sig = h.signal;
      var paidWord = o ? (o.status === "cancelled" || /RETURNED/.test(String(o.status)) ? "Canceled by Stop all" : (o.simulated ? "Simulated payment" : "Paid through PayPal sandbox")) : "";
      var what = h.signal === "match" ? (h.executed ? (o ? paidWord : "Bought") : "Checked: both said yes") :
        h.signal === "needs_yes" ? (h.approval_id ? (o ? "Approved and " + (paidWord === "Canceled by Stop all" ? "then canceled by Stop all" : paidWord.charAt(0).toLowerCase() + paidWord.slice(1)) :
          ap === "denied" ? "Denied by you, no money moved" : "Held for your yes") : "Checked: needs your yes") : "Refused, no money moved";
      if (h.signal === "needs_yes" && o) sig = "match";
      if (h.signal === "needs_yes" && ap === "denied") sig = "no_match";
      return '<li class="sig--' + esc(sig) + '"><span class="dot" aria-hidden="true"></span><span><b>' + esc(it.title || "Order") + " · " + esc((h.store && h.store.name) || "") +
        "</b><small>" + esc(what) + " · " + esc(when(h.at)) + '</small></span><span class="amt">' + usd(it.amount) +
        '<a href="/api/handshakes/' + encodeURIComponent(h.id) + '/proof" download="lowtides-proof-' + esc(h.id) + '.json">Proof<span class="cc-sr"> pack for ' + esc(it.title || "") + "</span></a></span></li>";
    }).join("");
  }

  // ---------- confirm dialog ----------
  function confirmBox(title, text, yes, danger, field) {   // field: optional {label, value} turns it into a one-line prompt
    return new Promise(function (resolve) {
      var d = document.createElement("dialog");
      if (typeof d.showModal !== "function") { resolve(window.confirm(title + "\n\n" + text)); return; }
      d.className = "cc-dialog";
      d.setAttribute("aria-labelledby", "cc-dlg-h");
      d.innerHTML = '<button type="button" class="cc-x" value="no" aria-label="Close">\u00d7</button><h2 id="cc-dlg-h">' + esc(title) + "</h2><p>" + esc(text) + "</p>" +
        (field ? '<label class="cc-label" for="cc-dlg-in">' + esc(field.label) + '</label><input class="cc-input" id="cc-dlg-in" type="text" maxlength="60" autocomplete="off" value="' + esc(field.value || "") + '">' : "") +
        '<div class="cc-inline">' +
        '<button type="button" class="lt-btn ' + (danger ? "lt-btn--danger" : "lt-btn--primary") + '" value="yes">' + esc(yes) + '</button>' +
        '<button type="button" class="lt-btn lt-btn--ghost" value="no">Cancel</button></div>';
      document.body.appendChild(d);
      var done = function (v) {
        var val = field ? d.querySelector("#cc-dlg-in").value.trim() : null;
        if (d.open) d.close(); d.remove();
        resolve(field ? (v ? val : null) : v);
      };
      if (field) d.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target.id === "cc-dlg-in") { e.preventDefault(); done(true); } });
      d.addEventListener("click", function (e) { var b = e.target.closest("button"); if (b) done(b.value === "yes"); });
      d.addEventListener("cancel", function (e) { e.preventDefault(); done(false); });
      d.showModal();
      (field ? d.querySelector("#cc-dlg-in") : d.querySelector('.cc-inline button[value="no"]')).focus();
    });
  }

  function promptBox(title, text, label, value, yes) { return confirmBox(title, text, yes || "Save", false, { label: label, value: value }); }

  function killFlow() {
    return confirmBox("Stop everything now?", "Stop all revokes the Pass of every one of your AI shoppers at every store at once, and cancels purchases still waiting to pay, along with payouts a store has not collected yet (those show as Refunded). You can turn them back on afterwards.", "Yes, stop all", true)
      .then(function (ok) { return ok ? api("POST", "/api/stop-all", {}) : null; });
  }

  // A domain that may wrap only after a dot or a hyphen (escaped for HTML first).
  function wbr(d) { return esc(d).replace(/([.-])/g, "$1<wbr>"); }
  function poss(n) { n = String(n == null ? "" : n); return n + (/s$/i.test(n) ? "’" : "’s"); }
  window.LT = {
    poss: poss, wbr: wbr, $: $, api: api, esc: esc, tidy: tidy, usd: usd, pct: pct, sleep: sleep, debounce: debounce, when: when, RM: RM, SIG: SIG,
    payLine: payLine, storeBadge: storeBadge, shopperBadge: shopperBadge, shopperLevel: shopperLevel, regStores: regStores, regStore: regStore, refreshStores: refreshStores, paintStoreBadges: paintStoreBadges,
    scout: scout, syncSlider: syncSlider, gpBadge: gpBadge, sigPill: sigPill,
    renderHandshake: renderHandshake, idleHandshake: idleHandshake, outcomeHtml: outcomeHtml, decide: decide, decisionHtml: decisionHtml,
    MODE_WORD: { autopilot: "Full Autopilot", assist: "AI Assist", off: "Off", auto: "Full Autopilot", review: "AI Assist", closed: "Off" },
    passCard: passCard, passState: passState, trustMeter: trustMeter, activity: activity, confirm: confirmBox, prompt: promptBox, killFlow: killFlow, rateHtml: rateHtml
  };
})();
