/* /handshake/ — "See both sides": the 90-second tour, free play and the live split screen. Uses /cc.js.
   Every step is a real call to this server's API and shows the real answer. The animation only paces what the API
   said; it never decides an outcome, and nothing advances before the real response has arrived.
   Narration reads the fixed caption lines below (the server allows exactly these, see TOUR_LINES in
   lowtides/assistant.py) through Ask Lowtides' voice; when a live answer differs from the script, the caption shows the
   live answer and nothing scripted is spoken. */
(function () {
  "use strict";
  var LT = window.LT, $ = LT.$, esc = LT.esc, usd = LT.usd;

  /* TOUR_LINES:start */
  var LINES = {
    intro: "Scout, the AI shopper, carries a signed Pass: the key. Each store publishes a signed Policy: the lock.",
    s1: "Scout wants a small item from Harbor Hardware. Both sides check.",
    yes: "Both said yes. Only then does money move, through PayPal.",
    s2: "Next, a pricier saw. It is over Scout’s ask-me line.",
    ask: "Needs your yes. Nothing is paid until a person approves.",
    approved: "Approved, and paid.",
    s3: "Now a deal that looks too good, at a lookalike store. GuardPooch sniffs it first.",
    fake: "Fake store. No money moved.",
    s4: "Brightside Office only sells to AI shoppers it trusts enough.",
    earned: "Scout’s trust grew from its own history. Now the same store says yes.",
    s5: "One press of Stop all revokes Scout’s Pass at every store.",
    killed: "Every store refused the revoked Pass. Scout’s trust score did not change.",
    s6: "Turn Scout back on, and it gets a fresh Pass.",
    again: "Shopping works again.",
    end: "That’s Lowtides: both say yes, or no money moves.",
    g_match: "Both said yes.",
    g_needs_yes: "Needs your yes.",
    g_no_match: "No match. No money moved."
  };
  /* TOUR_LINES:end */

  var KEY_SVG = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false"><circle cx="16" cy="24" r="9" fill="none" stroke="currentColor" stroke-width="4.5"/><path d="M25 24h17m-6 0v7m-6-7v5" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/></svg>';
  var LOCK_SVG = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false"><path d="M15 22v-6a9 9 0 0 1 18 0v6" fill="none" stroke="currentColor" stroke-width="4.5" stroke-linecap="round"/><rect x="9" y="22" width="30" height="20" rx="5" fill="currentColor"/><circle cx="24" cy="31" r="3" fill="#fff"/></svg>';
  var STORE_SVG = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false"><path d="M8 18l4-9h24l4 9M8 18h32v4a5 5 0 0 1-8 4 5 5 0 0 1-8 0 5 5 0 0 1-8 0 5 5 0 0 1-8-4zM11 27v12h26V27" fill="none" stroke="currentColor" stroke-width="4" stroke-linejoin="round"/></svg>';
  var POOCH_SVG = '<svg viewBox="0 0 120 120" aria-hidden="true" focusable="false"><circle cx="60" cy="60" r="58" fill="#fff"/>' +
    '<g class="hsr-head"><path d="M30 34c-8 6-12 22-8 34 4 4 10 2 14-4l4-24z" fill="#0D2A2E"/><path d="M90 34c8 6 12 22 8 34-4 4-10 2-14-4l-4-24z" fill="#0D2A2E"/>' +
    '<ellipse cx="60" cy="62" rx="28" ry="30" fill="#F4C27A"/><ellipse cx="60" cy="78" rx="16" ry="12" fill="#FFF3DD"/>' +
    '<circle cx="49" cy="58" r="4.5" fill="#0D2A2E"/><circle cx="71" cy="58" r="4.5" fill="#0D2A2E"/><circle cx="50.5" cy="56.5" r="1.4" fill="#fff"/><circle cx="72.5" cy="56.5" r="1.4" fill="#fff"/>' +
    '<ellipse class="hsr-nose" cx="60" cy="72" rx="6" ry="4.5" fill="#0D2A2E"/><path d="M54 81c3 3 9 3 12 0" fill="none" stroke="#0D2A2E" stroke-width="2.5" stroke-linecap="round"/></g>' +
    '<rect x="40" y="90" width="40" height="8" rx="4" fill="#1FB89A"/><circle cx="60" cy="101" r="5" fill="#E39A2D"/></svg>';

  var agent = null, stores = [], prefs = null, catalog = [], trustNow = null, lastStore = "harbor";
  function rm() { return !!LT.RM.matches; }
  // On a narrow screen the stage is taller than the window, so while the tour plays the view follows the action.
  function follow(el, where) {
    if (!el || window.innerWidth >= 860 || !T.started) return;
    var r = el.getBoundingClientRect();
    if (r.top >= 0 && r.bottom <= window.innerHeight) return;
    el.scrollIntoView({ behavior: rm() ? "auto" : "smooth", block: where || "nearest" });
  }
  function errText(e) { return (e && e.message) || "Something went wrong. Please try again."; }
  function storeById(id) { return stores.filter(function (s) { return s.id === id; })[0] || { id: id, name: id, domain: "" }; }
  function itemById(id) { return catalog.filter(function (x) { return x.id === id; })[0] || null; }

  // ---------- the live dot: on while any real tour call is in flight ----------
  var inFlight = 0, liveOff = null;
  function live(p) {
    inFlight++; clearTimeout(liveOff); $("tv-live").hidden = false;
    // stays lit a moment after a fast answer, so the eye can catch that a real call happened
    var done = function () { inFlight = Math.max(0, inFlight - 1); if (!inFlight) liveOff = setTimeout(function () { if (!inFlight) $("tv-live").hidden = true; }, 600); };
    p.then(done, done);
    return p;
  }
  function hs(store, item, execute) { return LT.api("POST", "/api/handshake", { agent_id: agent.id, store_id: store, item_id: item, execute: !!execute }); }

  // =====================================================================================
  // Stage: the one showpiece animation, shared by the tour and free play.
  // =====================================================================================
  function Stage(host, timing) {
    var wait = timing.wait, anim = timing.anim, gen = timing.gen;
    function q(s) { return host.querySelector(s); }
    function root() { return q(".tvs"); }
    function build() {
      host.innerHTML =
        '<div class="tvs">' +
        '<section class="tvs-col tvs-shop" aria-label="Shopper side: Scout’s Pass, the key">' +
        '<div class="tvs-h"><span class="tvs-ico tvs-keyhome">' + KEY_SVG + "</span><div><b>Scout · AI shopper</b><small>Pass: <span class=\"tvs-pass\" data-state=\"VALID\">checking…</span></small></div>" +
        '<div class="tvs-trust" title="Scout’s trust score, a heuristic from its own history"><b class="tvs-trust-n">–</b><small>trust</small></div></div>' +
        '<ol class="tvs-list" data-list="shop"></ol><div class="tvs-passcard" hidden></div></section>' +
        '<div class="tvs-mid"><div class="tvs-lock" title="The store’s lock: its signed Store Policy">' + LOCK_SVG + '</div><div class="tvs-pooch">' + POOCH_SVG + '<img class="tvs-dog" data-mascot="alert" src="/brand/mascot/dog-alert.webp" width="88" height="88" alt="">' + '<span class="tvs-woof" aria-hidden="true">WOOF!</span>' +
        '<button type="button" class="estop" hidden aria-label="Stop all: revoke Scout’s Pass at every store"><span class="estop-cap" aria-hidden="true">Stop all</span></button></div>' +
        '<p class="tvs-gp">GuardPooch checks the store</p></div>' +
        '<section class="tvs-col tvs-store" aria-label="Store side: its Store Policy, the lock">' +
        '<div class="tvs-h"><span class="tvs-ico">' + STORE_SVG + '</span><div><b class="tvs-sname">Pick a store</b><small class="tvs-sdom">demo store</small></div><span class="tvs-sgp"></span></div>' +
        '<ol class="tvs-list" data-list="store"></ol><ul class="tvs-stores" hidden></ul></section>' +
        '<div class="tvs-out"><div class="tvs-out-in"><p class="tvs-idle">' + esc(timing.idle || "") + "</p></div></div>" +
        '<span class="tvs-key" aria-hidden="true">' + KEY_SVG + "</span></div>";
      root().classList.toggle("is-rm", rm());
    }
    build();

    var S = {
      host: host,
      rebuild: build,
      el: root,
      clear: function () {
        var r = root();
        r.className = "tvs" + (rm() ? " is-rm" : "");
        r.removeAttribute("data-sig");
        ["match", "needs_yes", "no_match"].forEach(function (k) { r.classList.remove("sig--" + k); });
        q('[data-list="shop"]').innerHTML = ""; q('[data-list="store"]').innerHTML = "";
        q(".tvs-stores").hidden = true; q('[data-list="store"]').hidden = false;
        q(".tvs-passcard").hidden = true; q('[data-list="shop"]').hidden = false;
        q(".estop").hidden = true; q(".estop").setAttribute("aria-pressed", "false");
        q(".tvs-gp").textContent = "GuardPooch checks the store";
        var k = q(".tvs-key"); if (k.getAnimations) k.getAnimations().forEach(function (a) { a.cancel(); }); k.style.opacity = "0";
      },
      idle: function (text) { S.clear(); S.out('<p class="tvs-idle">' + esc(text) + "</p>"); },
      setStore: function (s) {
        q(".tvs-sname").textContent = s.name;
        q(".tvs-sdom").innerHTML = LT.wbr(s.domain || "") + " · demo store";
        q(".tvs-sgp").innerHTML = "";
      },
      setTrust: function (n) { q(".tvs-trust-n").textContent = n == null ? "–" : String(n); },
      setPass: function (state, why) {
        var p = q(".tvs-pass"); p.setAttribute("data-state", state); p.textContent = state;
        var pc = q(".tvs-passcard");
        if (pc && !pc.hidden) { pc.setAttribute("data-state", state); pc.querySelector(".tvs-pc-s").textContent = state; if (why) pc.querySelector("p").textContent = why; }
      },
      countTrust: function (from, to) {
        var el = q(".tvs-trust-n"), box = q(".tvs-trust");
        if (from == null || to == null || from === to || rm()) { S.setTrust(to); return Promise.resolve(); }
        var g = gen(), ms = 1100, t0 = null;
        box.classList.remove("is-up");
        return new Promise(function (res) {
          // Background tabs pause animation frames; never let the tour wait on one.
          setTimeout(function () { if (g === gen()) { el.textContent = String(to); box.classList.add("is-up"); } res(); }, ms + 400);
          function f(t) {
            if (g !== gen()) return res();
            if (t0 == null) t0 = t;
            var k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3);
            el.textContent = String(Math.round(from + (to - from) * e));
            if (k < 1) requestAnimationFrame(f); else { box.classList.add("is-up"); res(); }
          }
          requestAnimationFrame(f);
        });
      },
      pose: function (k) { var d = q(".tvs-dog"), m = window.LowtidesMascot || {}; if (d && m[k] && d.getAttribute("src") !== m[k]) d.setAttribute("src", m[k]); },
      sniff: function (on) { root().classList.toggle("is-sniff", !!on); if (on) S.pose("alert"); if (on) q(".tvs-gp").innerHTML = "GuardPooch is sniffing<br><span class=\"cc-mono\">" + LT.wbr(q(".tvs-sdom").textContent.replace(/ · demo store$/, "")) + "</span>"; },
      skeleton: function () {
        var row = function (w) { return '<li class="tvs-chk tvs-skel" aria-hidden="true"><span class="tvs-mark"></span><i style="width:' + w + '%"></i></li>'; };
        q('[data-list="shop"]').innerHTML = [62, 70, 58, 66, 54, 64, 72, 60].map(row).join("");
        q('[data-list="store"]').innerHTML = [60, 52, 66, 70, 74, 56].map(row).join("");
      },
      // The key travels from the shopper's side to the store's lock. Transform and opacity only.
      travel: function () {
        var r = root(), k = q(".tvs-key"), from = q(".tvs-keyhome").getBoundingClientRect(), to = q(".tvs-lock").getBoundingClientRect(), base = r.getBoundingClientRect();
        var x0 = from.left - base.left + from.width / 2 - 22, y0 = from.top - base.top + from.height / 2 - 22;
        var x1 = to.left - base.left + to.width / 2 - 22, y1 = to.top - base.top + to.height / 2 - 22;
        if (rm() || !k.animate) { r.classList.add("is-in"); return Promise.resolve(); }
        k.style.opacity = "0";
        var a = k.animate([
          { transform: "translate(" + x0 + "px," + y0 + "px) scale(.6) rotate(0deg)", opacity: 0 },
          { transform: "translate(" + x0 + "px," + y0 + "px) scale(1) rotate(0deg)", opacity: 1, offset: 0.12 },
          { transform: "translate(" + (x0 + (x1 - x0) * 0.55) + "px," + (y0 + (y1 - y0) * 0.55 - 26) + "px) scale(1.12) rotate(-12deg)", opacity: 1, offset: 0.6 },
          { transform: "translate(" + x1 + "px," + y1 + "px) scale(.9) rotate(0deg)", opacity: 1, offset: 0.88 },
          { transform: "translate(" + x1 + "px," + y1 + "px) scale(.5) rotate(90deg)", opacity: 0 }
        ], { duration: anim(1150), easing: "cubic-bezier(.45,.05,.25,1)", fill: "forwards" });
        // Background tabs do not finish animations; a timer makes sure the tour never waits on one.
        return new Promise(function (res) { a.onfinish = res; a.oncancel = res; setTimeout(res, 1150 + 500); }).then(function () { r.classList.add("is-in"); });
      },
      // Checks from the real response tick in one by one, both columns in turn.
      ticks: function (h, instant) {
        var mk = function (c) {
          var lv = c.level || (c.ok ? "ok" : "fail");
          return '<li class="tvs-chk" data-on="0" data-level="' + esc(lv) + '" title="' + esc(LT.tidy(c.detail)) + '"><span class="tvs-mark" aria-hidden="true">' + ({ ok: "✓", ask: "!", fail: "✕" }[lv] || "✓") + "</span><span>" + esc(c.label) +
            ' <span class="cc-sr">(' + ({ ok: "passed", ask: "needs a yes", fail: "says no" }[lv] || "") + ")</span></span></li>";
        };
        q('[data-list="shop"]').innerHTML = (h.shopper_checks || []).map(mk).join("");
        q('[data-list="store"]').innerHTML = (h.store_checks || []).map(mk).join("");
        var you = host.querySelectorAll('[data-list="shop"] .tvs-chk'), st = host.querySelectorAll('[data-list="store"] .tvs-chk'), order = [];
        for (var i = 0; i < Math.max(you.length, st.length); i++) { if (you[i]) order.push(you[i]); if (st[i]) order.push(st[i]); }
        if (instant || rm()) { order.forEach(function (li) { li.setAttribute("data-on", "1"); }); return Promise.resolve(); }
        var p = Promise.resolve();
        order.forEach(function (li) { p = p.then(function () { li.setAttribute("data-on", "1"); return wait(anim(150)); }); });
        return p;
      },
      verdict: function (h) {
        var r = root(), gp = (h.guardpooch || {}).verdict || "safe";
        S.sniff(false);
        S.pose(gp === "danger" ? "bark" : (h.signal === "match" ? "happy" : "alert"));
        if ((h.guardpooch || {}).verdict) q(".tvs-gp").innerHTML = LT.gpBadge(gp);
        if (h.trust && h.trust.score != null) S.setTrust(h.trust.score);
        if (h.pass_state) S.setPass(h.pass_state);
        r.setAttribute("data-sig", h.signal);
        r.classList.add("is-done", "is-in", "sig--" + h.signal);
      },
      out: function (html) { var o = q(".tvs-out"); o.innerHTML = '<div class="tvs-out-in">' + html + "</div>"; if (host.id === "stage" && html.indexOf("tvs-idle") < 0) follow(o); return o; },
      moment: function (cls, ms) { var r = root(); r.classList.remove(cls); void r.offsetWidth; r.classList.add(cls); if (ms) setTimeout(function () { r.classList.remove(cls); }, ms); },
      killMode: function (list) {
        S.clear();
        var r = root();
        r.classList.add("is-kill");
        q(".tvs-sname").textContent = "Every demo store";
        q(".tvs-sdom").textContent = list.length + " stores check the same Pass";
        q('[data-list="store"]').hidden = true;
        var ul = q(".tvs-stores");
        ul.hidden = false;
        ul.innerHTML = list.map(function (s) { return '<li class="tvs-st" data-st="' + esc(s.id) + '"><span>' + esc(s.name) + "</span><em>Pass accepted</em></li>"; }).join("");
        q(".estop").hidden = false;
        q(".tvs-gp").textContent = "The real Stop all button";
        q('[data-list="shop"]').hidden = true;
        var pc = q(".tvs-passcard");
        pc.hidden = false;
        pc.innerHTML = '<span class="tvs-pc-k">Scout’s Pass</span><b class="tvs-pc-s">VALID</b><p>Signed with Ed25519. Every store checks it before Scout can pay.</p>';
        return q(".estop");
      },
      flip: function (id, text, out) {
        var li = q('.tvs-st[data-st="' + id + '"]');
        if (!li) return;
        li.classList.toggle("is-out", !!out);
        li.querySelector("em").textContent = text;
      }
    };
    return S;
  }

  // Outcome HTML pieces. Every number and id is from the response.
  function payHtml(amount, simulated, ref) {
    return '<p class="tvs-pay">' + esc(LT.payLine(amount, simulated)) + "</p>" +
      (ref ? '<p class="cc-fine">' + (simulated ? "Reference" : "PayPal sandbox id") + ': <span class="cc-mono">' + esc(ref) + "</span></p>" : "");
  }
  function asks(h, level, n) {
    var out = [];
    (h.shopper_checks || []).concat(h.store_checks || []).forEach(function (c) { if (c.level === level) out.push(LT.tidy(c.detail)); });
    return out.slice(0, n || 2);
  }
  function genericOut(h) {
    var o = h.order, it = h.item || {};
    if (h.signal === "match") {
      return LT.sigPill("match") + (o ? payHtml(o.amount != null ? o.amount : it.amount, o.simulated, o.paypal_order_id) : "<p>This was a check only. No money moved.</p>");
    }
    if (h.signal === "needs_yes") {
      return LT.sigPill("needs_yes") + "<ul>" + asks(h, "ask").map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul>" +
        (h.approval_id ? '<div class="tvs-out-row"><button type="button" class="lt-btn lt-btn--primary is-tap" data-approve="' + esc(h.approval_id) + '">Approve ' + usd(it.amount) + '</button><span class="cc-fine">Nothing is paid until a person taps it.</span></div>'
          : "<p>This was a check only. No money moved.</p>");
    }
    var gp = h.guardpooch || {}, why = gp.verdict === "danger" ? (gp.reasons || []).slice(0, 2) : asks(h, "fail");
    return LT.sigPill("no_match") + "<ul>" + why.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul>";
  }
  function approvedHtml(r) {
    var sim = r.simulated || r.payout_status === "SIMULATED";
    return LT.sigPill("match", "You said yes") + "<h3>" + (r.status === "paid" ? "Approved and paid." : "Approved.") + "</h3>" +
      (r.status === "paid" ? payHtml(r.amount, sim, r.payout_batch_id || r.paypal_order_id) : "<p>Paying " + usd(r.amount) + " through PayPal sandbox.</p>");
  }

  // =====================================================================================
  // The tour
  // =====================================================================================
  var ABORT = { abort: true };
  var T = { cur: -1, playing: false, paused: false, running: false, rush: false, gen: 0, after: null, started: false, killed: false, before: null, ended: false, holdTimer: null };
  var SOUND_KEY = "lt_tour_voice", sound = true;      // narration is on unless the visitor muted it
  try { sound = window.localStorage.getItem(SOUND_KEY) !== "0"; } catch (e) { sound = true; }

  // A wait that stops while paused, ends early when rushed, and is dropped on restart/exit (by generation).
  function wait(ms) {
    var g = T.gen;
    return new Promise(function (res, rej) {
      var t = 0;
      (function tick() {
        if (g !== T.gen) return rej(ABORT);
        if (T.rush || t >= ms) return res();
        if (!T.paused) t += 40;
        setTimeout(tick, 40);
      })();
    });
  }
  function anim(ms) { return rm() ? 0 : ms; }
  // The pause after a step's first caption: with motion it overlaps the key's travel; without, it is reading time.
  function lead() { return rm() ? 1600 : 450; }
  function guard(p) { var g = T.gen; return p.then(function (v) { if (g !== T.gen) throw ABORT; return v; }); }

  var stage = null, fstage = null;

  // Narration plays recorded clips (/brand/tour/<line>.mp3), one per fixed line. If a clip cannot play,
  // the caption carries the line and nothing is spoken: the tour never falls back to a synthetic browser voice.
  var CLIP = { killed: "stopped" }, clip = null;
  function narrate(key) {
    if (!sound || !key || !LINES[key] || !window.Audio) return;
    hush();
    try {
      var a = new window.Audio("/brand/tour/" + (CLIP[key] || key) + ".mp3");
      clip = a;
      a.addEventListener("ended", function () { if (clip === a) clip = null; });
      a.addEventListener("error", function () { if (clip === a) clip = null; });
      var pr = a.play();
      if (pr && pr.catch) pr.catch(function () { if (clip === a) clip = null; });
    } catch (e) { clip = null; }
  }
  function hush() { try { if (clip) { clip.pause(); clip = null; } } catch (e) { clip = null; } }
  function talking() { return !!(clip && !clip.paused && !clip.ended); }
  // Let the previous line finish before the next one starts, within reason.
  function speechGap() {
    if (!sound || !talking()) return Promise.resolve();
    var t = 0;
    return new Promise(function (res) {
      (function f() { if (!talking() || t >= 9000 || T.rush) return res(); t += 100; setTimeout(f, 100); })();
    });
  }
  // A caption: a fixed line (spoken when sound is on) or the live answer (shown, not spoken).
  function cap(key, sub, liveText) {
    $("cap-big").textContent = liveText || LINES[key] || "";
    $("cap-sub").innerHTML = sub || "";
    T.capKey = liveText ? null : key;
    if (!liveText) narrate(key);
  }

  var STEPS = [
    { t: "Small buy", run: step1 }, { t: "Needs your yes", run: step2 }, { t: "Fake store", run: step3 },
    { t: "Trust earns its way in", run: step4 }, { t: "Stop all, everywhere", run: step5 }, { t: "Back on", run: step6 }
  ];
  STEPS.forEach(function (s) { s.done = false; s.sig = null; s.snap = null; });

  function renderProg() {
    $("steps").innerHTML = STEPS.map(function (s, i) {
      var can = !T.running && (s.done || (i === T.cur + 1 && (i === 0 || STEPS[i - 1].done)) || i === T.cur);
      var cls = (s.done ? "is-done " : "") + (i === T.cur ? "is-cur " : "") + (s.sig ? "sig--" + s.sig : "");
      return '<li><button type="button" data-i="' + i + '" class="' + cls + '"' + (i === T.cur ? ' aria-current="step"' : "") + (can ? "" : " disabled") +
        ' aria-label="Step ' + (i + 1) + ": " + esc(s.t) + (s.done ? ", done" : "") + '"><i aria-hidden="true"></i><span class="tv-seg-t">' + (i + 1) + ". " + esc(s.t) + "</span></button></li>";
    }).join("");
    $("tv-count").textContent = T.cur < 0 ? STEPS.length + " steps" : "Step " + (T.cur + 1) + " of " + STEPS.length;
    $("back").disabled = T.running || T.cur <= 0;
    $("next").disabled = T.cur >= STEPS.length - 1 && STEPS[STEPS.length - 1].done && T.ended;
    var pb = $("play");
    pb.setAttribute("aria-pressed", String(T.playing));
    pb.setAttribute("aria-label", T.playing ? "Pause the tour" : "Play the tour");
  }
  function setPaused(p) {
    T.paused = p;
    var r = stage && stage.el();
    if (r) r.classList.toggle("is-paused", !!p);
  }

  function snapshot(i) {
    STEPS[i].snap = { stage: stage.host.innerHTML, big: $("cap-big").textContent, sub: $("cap-sub").innerHTML, key: T.capKey };
  }
  function showSnap(i) {
    var s = STEPS[i].snap;
    if (!s) return;
    T.cur = i;
    stage.host.innerHTML = s.stage;
    Array.prototype.forEach.call(stage.host.querySelectorAll("button"), function (b) { b.disabled = true; });
    $("cap-big").textContent = s.big; $("cap-sub").innerHTML = s.sub; T.capKey = s.key;
    hush();
    renderProg();
  }

  function fail(i, e) {
    T.running = false; T.playing = false; T.rush = false; T.after = null;
    $("cap-big").textContent = "That call didn’t work.";
    $("cap-sub").innerHTML = esc(errText(e)) + ' <button type="button" class="lt-btn lt-btn--ghost tv-retry" id="retry">Retry</button>';
    $("retry").addEventListener("click", function () { T.playing = true; runStep(i); });
    renderProg();
  }

  function runStep(i) {
    if (T.running) return;
    T.running = true; T.cur = i; T.rush = false; setPaused(false); T.ended = false;
    $("end").hidden = true;
    follow($("tv"), "start");
    renderProg();
    var pre = (i === 0 && !T.before) ? fresh() : Promise.resolve();
    pre.then(speechGap).then(function () { return STEPS[i].run(); }).then(function (sig) {
      STEPS[i].done = true; STEPS[i].sig = sig || STEPS[i].sig;
      snapshot(i);
      T.running = false; T.rush = false;
      renderProg();
      afterStep(i);
      refreshSides();
    }, function (e) {
      if (e === ABORT) { T.running = false; renderProg(); return; }
      fail(i, e);
    });
  }
  function afterStep(i) {
    if (T.after === "next") { T.after = null; go(i + 1); return; }
    if (!T.playing) return;
    var g = T.gen;
    wait(i === STEPS.length - 1 ? 2200 : 900).then(speechGap).then(function () { if (g === T.gen && T.playing && !T.running && T.cur === i) go(i + 1); }, function () {});
  }
  function go(i) {
    if (i >= STEPS.length) { showEnd(); return; }
    if (i < 0) return;
    if (STEPS[i].done) {
      showSnap(i);
      if (T.playing) afterStep(i);
      return;
    }
    if (i > 0 && !STEPS[i - 1].done) return;
    runStep(i);
  }

  // ----- step bodies -----
  function fresh() {
    stage.idle("Resetting your demo sandbox…");
    cap("intro", "First, a fresh start: the tour resets your own demo sandbox (test data only).");
    return guard(quiet.then(function () { return live(LT.api("POST", "/api/demo/reset", {})); })).then(function () {
      T.killed = false;
      return guard(live(load(true)));
    }).then(function () {
      // A real check (no money) at Brightside before anything else: the "before" picture for step 4.
      return guard(live(hs("brightside", "bright-lamp", false)));
    }).then(function (h) {
      T.before = h;
      stage.setTrust(h.trust && h.trust.score); stage.setPass(h.pass_state || "VALID");
      return wait(rm() ? 2600 : 2200);
    });
  }
  // One purchase on the stage: the call goes out as the key starts to travel; checks tick in from the real answer.
  function playBuy(S, storeId, itemId, execute, w) {
    var s = storeById(storeId);
    S.clear(); S.setStore(s); S.skeleton(); S.sniff(true);
    S.out('<p class="tvs-idle">Checking both sides…</p>');
    var call = live(hs(storeId, itemId, execute));
    return Promise.all([call, S.travel()]).then(function (r) {
      var h = r[0];
      return S.ticks(h).then(function () { return w(anim(320)); }).then(function () { S.verdict(h); lastStore = h.store.id; detail(h, S); return h; });
    });
  }
  function itemLine(id) {
    var it = itemById(id);
    return it ? esc(it.title) + " · " + usd(it.price) + " · " + esc(it.store_name) : "";
  }
  function trustMove(h) {
    return h.trust && h.trust.before != null && h.trust.before !== h.trust.score ? " Scout’s trust score: " + esc(h.trust.before) + " → " + esc(h.trust.score) + "." : "";
  }
  function liveCap(h) {   // the live answer, when it is not the scripted one
    var gk = "g_" + h.signal;
    cap(gk, esc(h.headline || ""));
  }

  function step1() {
    cap("s1", itemLine("harbor-anchors"));
    return wait(lead()).then(function () { return guard(playBuy(stage, "harbor", "harbor-anchors", true, wait)); }).then(function (h) {
      stage.out(genericOut(h) + (trustMove(h) ? "<p>" + trustMove(h) + "</p>" : ""));
      return speechGap().then(function () {
        if (h.signal === "match" && h.order) {
          stage.moment("is-pulse", 2400);
          cap("yes", esc(LT.payLine(h.order.amount, h.order.simulated)) + ' <span class="cc-mono">' + esc(h.order.paypal_order_id || "") + "</span>." + trustMove(h));
        } else liveCap(h);
        return wait(3400).then(function () { return h.signal; });
      });
    });
  }

  var approving = {};
  function approve(id, btn, S) {
    if (approving[id]) return approving[id];
    if (btn) { btn.classList.add("is-pressed"); btn.disabled = true; }
    approving[id] = live(LT.api("POST", "/api/spend/" + encodeURIComponent(id) + "/approve", {})).then(function (r) {
      S.out(approvedHtml(r));
      S.el().setAttribute("data-sig", "match");
      S.el().classList.remove("sig--needs_yes"); S.el().classList.add("sig--match");
      S.moment("is-pulse", 2400);
      return r;
    });
    approving[id].catch(function () { delete approving[id]; if (btn) { btn.disabled = false; btn.classList.remove("is-pressed"); } });
    return approving[id];
  }
  function step2() {
    cap("s2", itemLine("harbor-miter"));
    return wait(lead()).then(function () { return guard(playBuy(stage, "harbor", "harbor-miter", true, wait)); }).then(function (h) {
      stage.out(genericOut(h));
      if (h.signal !== "needs_yes" || !h.approval_id) {
        liveCap(h);
        return wait(3200).then(function () { return h.signal; });
      }
      var btn = stage.host.querySelector("[data-approve]");
      return speechGap().then(function () {
        cap("ask", esc(asks(h, "ask", 1)[0] || h.headline || "") + " The tour taps Approve for you in a moment.");
        return wait(rm() ? 2600 : 2600);
      }).then(function () {
        btn = stage.host.querySelector("[data-approve]") || btn;
        return guard(approve(h.approval_id, btn, stage));
      }).then(function (r) {
        var sim = r.simulated || r.payout_status === "SIMULATED";
        return speechGap().then(function () {
          if (r.status === "paid") cap("approved", "One tap. " + esc(LT.payLine(r.amount, sim)) + " to " + esc(r.merchant || "the store") + ". Nothing would have moved without it.");
          else cap(null, "Paying " + usd(r.amount) + " through PayPal sandbox.", "Approved.");
          return wait(3000).then(function () { return "match"; });
        });
      });
    });
  }
  function step3() {
    cap("s3", itemLine("fake-drill"));
    return wait(lead()).then(function () { return guard(playBuy(stage, "fakeharbor", "fake-drill", true, wait)); }).then(function (h) {
      var gp = h.guardpooch || {};
      stage.out(h.signal === "no_match" && gp.verdict === "danger" ? LT.sigPill("no_match", "Fake store. No money moved.") + "<ul>" + (gp.reasons || []).slice(0, 2).map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul>" : genericOut(h));
      if (gp.verdict === "danger") { stage.moment("is-shake", 700); stage.moment("is-bark"); }
      return speechGap().then(function () {
        if (h.signal === "no_match" && gp.verdict === "danger") cap("fake", "GuardPooch found " + (gp.reasons || []).length + " problems. Top two: " + esc((gp.reasons || []).slice(0, 2).join(" ")));
        else liveCap(h);
        return wait(3600).then(function () { return h.signal; });
      });
    });
  }
  function minTrust(h) { return (h && (h.store_checks || []).filter(function (c) { return c.id === "min_trust"; })[0]) || null; }
  function step4() {
    var b = T.before, bc = minTrust(b), bs = storeById("brightside");
    cap("s4", bs.policy ? "Its signed Store Policy asks for a trust score of " + esc(bs.policy.min_trust) + " or more." : "");
    // First, the real check from the start of the tour (no money): refused.
    stage.clear(); stage.setStore(bs);
    return stage.ticks(b, true).then(function () {
      stage.verdict(b);
      stage.out(LT.sigPill(b.signal, b.signal === "no_match" ? "Refused at the start of the tour" : null) + "<p>" + esc(LT.tidy(bc ? bc.detail : b.headline)) + " (a check, no money)</p>");
      return wait(2600);
    }).then(function () { return guard(live(LT.api("GET", "/api/agents/" + agent.id + "/trust"))); }).then(function (t) {
      $("cap-sub").innerHTML = "Since then, Scout’s own history moved its trust score: " + esc(b.trust.score) + " → " + esc(t.score) + " (a heuristic from this server).";
      return stage.countTrust(b.trust.score, t.score).then(function () { return wait(900); });
    }).then(function () { return guard(playBuy(stage, "brightside", "bright-lamp", true, wait)); }).then(function (h) {
      var c = minTrust(h);
      stage.out(genericOut(h) + (c ? "<p>" + esc(LT.tidy(c.detail)) + "</p>" : ""));
      var earned = b.signal === "no_match" && h.signal === "match" && h.trust && h.trust.score > b.trust.score;
      if (h.signal === "match") stage.moment("is-pulse", 2400);
      return speechGap().then(function () {
        if (earned) cap("earned", "Before: " + esc(LT.tidy(bc ? bc.detail : "")) + " Now: " + esc(LT.tidy(c ? c.detail : h.headline)));
        else liveCap(h);
        return wait(3400).then(function () { return h.signal; });
      });
    });
  }
  function step5() {
    var picks = {};
    catalog.forEach(function (it) { if (!picks[it.store_id]) picks[it.store_id] = it.id; });
    var list = stores.filter(function (s) { return picks[s.id]; });
    var btn = stage.killMode(list), trustBefore = null, pressed = null;
    stage.setPass("VALID");
    cap("s5", "The real Stop all button: the same one as on the shopper page." + (T.playing ? " The tour presses it in a moment." : " Press it, or press Next."));
    return guard(live(LT.api("GET", "/api/agents/" + agent.id + "/trust"))).then(function (t) {
      trustBefore = t.score; stage.setTrust(t.score);
      return new Promise(function (res, rej) {
        var g = T.gen;
        var did = false;
        pressed = function () { if (did) return; did = true; btn.removeEventListener("click", pressed); res(); };
        btn.addEventListener("click", pressed);
        var hold = function () { if (did) return; if (g !== T.gen) return rej(ABORT); if (T.playing || T.rush) return pressed(); setTimeout(hold, 100); };
        wait(2400).then(hold, rej);
      });
    }).then(function () {
      btn.setAttribute("aria-pressed", "true"); btn.disabled = true;
      return guard(live(LT.api("POST", "/api/stop-all", {})));
    }).then(function () {
      T.killed = true;
      stage.setPass("REVOKED", "Revoked by Stop all. Every store now refuses it; nothing can be paid with it.");
      $("cap-sub").textContent = "Stop all pressed. Every store now checks the same Pass…";
      var seq = Promise.resolve(), res = [];
      list.forEach(function (s) {
        seq = seq.then(function () { return guard(live(hs(s.id, picks[s.id], false))); }).then(function (h) {
          var out = h.pass_state === "REVOKED" && h.signal === "no_match";
          stage.flip(s.id, out ? "Pass revoked — refused" : LT.SIG[h.signal].short, out);
          setTile(s.id, h.signal, out ? "Pass revoked — refused" : LT.SIG[h.signal].short);
          res.push(h);
          return wait(anim(260));
        });
      });
      return seq.then(function () { return res; });
    }).then(function (res) {
      return guard(live(LT.api("GET", "/api/agents/" + agent.id + "/trust"))).then(function (t) {
        var refused = res.filter(function (h) { return h.pass_state === "REVOKED" && h.signal === "no_match"; }).length;
        stage.setTrust(t.score);
        var same = t.score === trustBefore;
        stage.verdict({ signal: "no_match", guardpooch: {}, pass_state: "REVOKED", trust: { score: t.score } });
        stage.el().classList.add("is-kill");
        stage.host.querySelector(".tvs-gp").textContent = "Stop all pressed";
        stage.out(LT.sigPill("no_match", "Pass revoked — refused") + "<p>Refused at " + refused + " of " + res.length + " stores, each from a real check. No money can move until Scout is turned back on.</p>" +
          "<p>Trust score before Stop all: <b>" + esc(trustBefore) + "</b>, after: <b>" + esc(t.score) + "</b>" + (same ? " (unchanged: Stop all is your choice, not Scout’s misbehavior)." : ".") + "</p>");
        return speechGap().then(function () {
          var sub = "Refused at " + refused + " of " + res.length + " stores. Trust score before Stop all " + esc(trustBefore) + ", after " + esc(t.score) + ".";
          if (refused === res.length && same) cap("killed", sub);
          else cap(null, sub, "Stop all pressed: the live answers are below.");
          return wait(3600).then(function () { return "no_match"; });
        });
      });
    });
  }
  function step6() {
    cap("s6", "Same as the “Turn Scout back on” button on the shopper page: resume, then a freshly signed Pass.");
    return guard(live(LT.api("POST", "/api/agents/" + agent.id + "/resume", {}))).then(function () {
      return guard(live(LT.api("POST", "/api/agents/" + agent.id + "/pass", {})));
    }).then(function () {
      T.killed = false;
      return wait(lead());
    }).then(function () { return guard(playBuy(stage, "harbor", "harbor-anchors", true, wait)); }).then(function (h) {
      stage.out(genericOut(h));
      if (h.signal === "match") stage.moment("is-pulse", 2400);
      return speechGap().then(function () {
        if (h.signal === "match" && h.order) cap("again", esc(LT.payLine(h.order.amount, h.order.simulated)) + ' <span class="cc-mono">' + esc(h.order.paypal_order_id || "") + "</span>.");
        else liveCap(h);
        return wait(2400).then(function () { return h.signal; });
      });
    });
  }

  // ----- control -----
  function showEnd() {
    T.playing = false; T.ended = true; T.cur = STEPS.length - 1;
    renderProg();
    $("end").hidden = false;
    $("end-status").textContent = T.killed ? "Scout is still switched off by Stop all. Try it yourself turns it back on first." : "";
    $("cap-big").textContent = "Tour complete: " + STEPS.length + " steps, all live.";
    $("cap-sub").textContent = "Every result came from this server’s live API, in your own demo sandbox.";
    T.capKey = "end";
    narrate("end");
    $("end-h").focus({ preventScroll: true });
  }
  function begin() {
    T.started = true;
    $("tv").setAttribute("data-state", "on");
    $("cover").hidden = true;
    $("play").focus({ preventScroll: true });
  }
  function play() {
    if (!T.started) begin();
    if (T.ended) { restart(); return; }
    T.playing = true; setPaused(false);
    renderProg();
    if (T.running) return;
    if (T.cur < 0) { go(0); return; }
    if (STEPS[T.cur] && STEPS[T.cur].done) go(T.cur + 1); else go(T.cur);
  }
  function pause() {
    T.playing = false; setPaused(true); hush();
    renderProg();
  }
  function toggle() { if (T.playing) pause(); else play(); }
  function next() {
    if (!T.started) begin();
    if (T.running) { T.rush = true; T.after = "next"; setPaused(false); return; }
    if (T.ended) return;
    hush();
    if (T.cur < 0) { go(0); return; }
    if (!STEPS[T.cur].done) { go(T.cur); return; }
    go(T.cur + 1);
  }
  function back() {
    if (T.running || T.cur <= 0) return;
    T.playing = false; T.ended = false; $("end").hidden = true;
    showSnap(T.cur - 1);
  }
  function restart() {
    T.gen++; hush();
    T.cur = -1; T.playing = true; T.running = false; T.rush = false; T.after = null; T.before = null; T.ended = false; setPaused(false);
    STEPS.forEach(function (s) { s.done = false; s.sig = null; s.snap = null; });
    approving = {};
    if (!T.started) begin();
    $("end").hidden = true; $("cover").hidden = true;
    renderProg();
    go(0);
  }
  function exitTour() {
    if (!T.started) return;
    T.playing = false; setPaused(true); hush();
    T.started = false;
    $("tv").setAttribute("data-state", "idle");
    var mid = T.cur >= 0 && !T.ended;
    $("start-t").textContent = mid ? "Resume the tour" : "Play the 90-second tour";
    $("start-over").hidden = !mid;
    $("cover-note").textContent = mid ? "Paused at step " + (T.cur + 1) + " of " + STEPS.length + "." : "The tour first resets your own demo sandbox. It only touches test data.";
    $("end").hidden = true;
    $("cover").hidden = false;
    renderProg();
    $("start").focus({ preventScroll: true });
  }
  function setSound(on, keep) {
    sound = !!on;
    if (!keep) { try { window.localStorage.setItem(SOUND_KEY, sound ? "1" : "0"); } catch (e) { /* private mode */ } }   // only a press of the speaker button is remembered
    var b = $("sound");
    b.setAttribute("aria-pressed", String(sound));
    b.title = sound ? "Narration on (tap to mute)" : "Narration off (tap to hear the captions)";
    b.setAttribute("aria-label", sound ? "Narration on" : "Narration off");
    if (!sound) hush(); else if (T.started && T.capKey) narrate(T.capKey);
  }

  // ---------- the live split screen below ----------
  var TILE = {};
  function renderTiles() {
    $("tiles").innerHTML = stores.map(function (s) {
      var p = s.policy || {};
      var rules = s.policy_signed ? "Up to " + usd(p.max_order_no_human, 0) + " without a person · trust " + p.min_trust + "+ · sells " + (p.categories || []).join(", ") : "No signed Store Policy";
      var mode = p.mode || "auto";
      var sel = s.policy_signed ? '<small><label for="sm-' + esc(s.id) + '">Store mode </label><select class="cc-select" style="min-height:44px;padding:6px 12px;font-size:var(--lt-fs-sm)" id="sm-' + esc(s.id) + '" data-smode="' + esc(s.id) + '">' +
        ["auto", "review", "closed"].map(function (m) { return '<option value="' + m + '"' + (m === mode ? " selected" : "") + ">" + esc(LT.MODE_WORD[m]) + "</option>"; }).join("") + "</select></small>"
        : '<small><span class="cc-mode-tag cc-mode-tag--closed">Can’t sign a policy</span></small>';
      return '<li class="cc-tile" id="tile-' + esc(s.id) + '"><b>' + esc(s.name) + '</b><span class="cc-tile-st">' + LT.gpBadge(s.guardpooch && s.guardpooch.verdict) + "</span>" +
        "<small>" + LT.wbr(s.domain) + " (demo store) · " + esc(rules) + "</small>" + '<span class="cc-tile-tb">' + LT.storeBadge(s.id) + "</span>" + sel + "</li>";
    }).join("");
    Object.keys(TILE).forEach(function (k) { setTile(k, TILE[k].sig, TILE[k].text, true); });
  }
  function setTile(id, sig, text, quiet) {
    TILE[id] = { sig: sig, text: text };
    var el = $("tile-" + id);
    if (!el) return;
    el.className = "cc-tile sig--" + sig + (quiet ? "" : " is-flip");
    el.querySelector(".cc-tile-st").textContent = text;
  }
  function renderFacing() {
    var s = stores.filter(function (x) { return x.id === lastStore; })[0] || stores[0];
    if (!s || !agent) return;
    var who = agent.name || "Scout";
    $("face-shopper").innerHTML = '<span class="cc-facing-lead">' + esc(who) + " sees:</span> " + (LT.storeBadge(s.id, s.name) || esc(s.name));
    var min = s.policy_signed && s.policy ? s.policy.min_trust : null;
    $("face-store").innerHTML = '<span class="cc-facing-lead">' + esc(s.name) + " sees:</span> " +
      (trustNow == null ? "" : min == null ? '<span class="lt-tb lt-tb--danger">' + esc(who) + ": trust " + esc(trustNow) + "</span> (no signed policy to compare with)" : LT.shopperBadge(agent.id, who, trustNow, min, s.name));
  }
  function refreshShopper() {
    return Promise.all([
      LT.api("GET", "/api/agents/" + agent.id + "/pass").catch(function () { return null; }),
      LT.api("GET", "/api/agents/" + agent.id + "/trust"),
      LT.api("GET", "/api/shopper/prefs?agent_id=" + agent.id)
    ]).then(function (r) {
      prefs = r[2];
      var m = document.querySelector('#mode input[value="' + (prefs.mode || "autopilot") + '"]');
      if (m) m.checked = true;
      LT.passCard($("pass"), r[0], prefs);
      LT.trustMeter($("trust"), r[1], stores);
      trustNow = r[1].score;
      renderFacing();
    });
  }
  function refreshStoresNow() {
    return LT.api("GET", "/api/stores?agent_id=" + agent.id).then(function (r) { stores = r.stores || stores; LT.regStores(stores); renderTiles(); }).catch(function () {});
  }
  // Background reads of the split screen. A reset waits for them, so none of them asks about a Scout that was just reset away.
  var quiet = Promise.resolve();
  function track(p) { var q = p.catch(function () {}); quiet = Promise.all([quiet, q]); return p; }
  function refreshSides() { if (agent) track(refreshStoresNow().then(refreshShopper)); }
  // The full check-by-check panel (details, proof pack) for the latest answer.
  function detail(h, S) {
    var label = h.signal === "match" ? (h.order ? "Paid " + usd(h.item.amount) : "Both said yes") : h.signal === "needs_yes" ? "Needs your yes" : "Refused, no money moved";
    if (h.pass_state === "REVOKED") label = "Pass revoked — refused";
    setTile(h.store.id, h.signal, label);
    LT.renderHandshake($("hs-panel"), h, { focus: false, onApprove: function (x, b) { approve(x.approval_id, null, S); b.disabled = true; } }).catch(function () {});
  }

  // ---------- free play: the same stage ----------
  function fillItems() {
    var sid = $("f-store").value;
    $("f-item").innerHTML = catalog.filter(function (it) { return it.store_id === sid; }).map(function (it) {
      return '<option value="' + esc(it.id) + '">' + esc(it.title) + " · " + usd(it.price) + "</option>";
    }).join("");
  }
  var freeBusy = false;
  function free(e) {
    e.preventDefault();
    if (freeBusy) return;
    if (T.running) { $("free-status").textContent = "The tour is mid-step. Pause or finish it first."; return; }
    var s = $("f-store").value, it = $("f-item").value;
    if (!s || !it || !agent) return;
    freeBusy = true;
    $("free-status").textContent = "Checking both sides…";
    var sl = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    playBuy(fstage, s, it, $("f-exec").checked, sl).then(function (h) {
      fstage.out(genericOut(h));
      if (h.signal === "match" && h.order) fstage.moment("is-pulse", 2400);
      if (h.signal === "no_match" && (h.guardpooch || {}).verdict === "danger") { fstage.moment("is-shake", 700); fstage.moment("is-bark"); }
      $("free-status").textContent = LT.SIG[h.signal].label + ". Every check’s detail and the proof pack are in the Handshake panel below.";
      refreshSides();
    }).catch(function (err) { $("free-status").textContent = errText(err); fstage.idle("That didn’t work. Try again."); })
      .then(function () { freeBusy = false; });
  }

  // ---------- load ----------
  function load(force) {
    return LT.scout(force).then(function (a) {
      agent = a;
      return Promise.all([LT.api("GET", "/api/stores?agent_id=" + a.id), LT.api("GET", "/api/catalog?agent_id=" + a.id)]);
    }).then(function (r) {
      stores = r[0].stores || [];
      LT.regStores(stores);
      catalog = (r[1].items || []).slice().sort(function (x, y) { return x.price - y.price; });
      renderTiles();
      var keep = $("f-store").value;
      $("f-store").innerHTML = stores.map(function (s) { return '<option value="' + esc(s.id) + '">' + esc(s.name) + "</option>"; }).join("");
      if (keep) $("f-store").value = keep;
      fillItems();
      return refreshShopper();
    }).then(function () {
      if (!T.started) { stage.setTrust(trustNow); }
    });
  }
  function resetDemo() {
    LT.confirm("Start the demo over?", "This clears your demo sandbox back to the start. It only touches test data.", "Yes, reset", false).then(function (ok) {
      if (!ok) return;
      $("end-status").textContent = "Resetting…";
      return quiet.then(function () { return LT.api("POST", "/api/demo/reset", {}); }).then(function () {
        T.killed = false; TILE = {};
        return load(true);
      }).then(function () { $("end-status").textContent = "Demo reset. Scout is back at the start."; });
    }).catch(function (e) { $("end-status").textContent = "Reset failed: " + errText(e); });
  }
  // The next click must work: if Stop all left Scout off, turn it back on before leaving for the shopper page.
  function leave(e) {
    if (!T.killed || !agent) return;
    e.preventDefault();
    var href = e.currentTarget.getAttribute("href");
    $("end-status").textContent = "Turning Scout back on…";
    LT.api("POST", "/api/agents/" + agent.id + "/resume", {}).then(function () { return LT.api("POST", "/api/agents/" + agent.id + "/pass", {}); })
      .then(function () { T.killed = false; window.location.href = href; })
      .catch(function (err) { $("end-status").textContent = "Couldn’t turn Scout back on: " + errText(err); });
  }
  function resync() {
    if (!agent || T.running) return;
    track(LT.api("GET", "/api/stores?agent_id=" + agent.id).then(function (r) { stores = r.stores || []; LT.regStores(stores); renderTiles(); return refreshShopper(); }))
      .then(function () { $("sync-status").textContent = "In sync with the shopper and store pages (" + new Date().toLocaleTimeString() + ")."; })
      .catch(function () {});
  }

  function onKey(e) {
    if (!T.started || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    if (document.querySelector("dialog[open]") || document.querySelector(".lta-open")) return;
    var t = e.target, tag = (t && t.tagName) || "";
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(tag) || (t && t.isContentEditable)) return;
    if (e.key === "Escape") { e.preventDefault(); exitTour(); return; }
    var inTour = t === document.body || $("tv").contains(t);
    if (!inTour) return;
    if (e.key === " " || e.key === "Spacebar") {
      if (/^(BUTTON|A)$/.test(tag)) return;          // a focused button already acts on Space
      e.preventDefault(); toggle(); return;
    }
    if (e.key === "ArrowRight") { e.preventDefault(); next(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); back(); }
  }

  function init() {
    stage = Stage($("stage"), { wait: wait, anim: anim, gen: function () { return T.gen; }, idle: "Press Play. Scout will shop on this stage." });
    fstage = Stage($("fp-stage"), { wait: function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); }, anim: anim, gen: function () { return 0; }, idle: "Pick a store and an item, then press Go." });
    renderProg();
    setSound(sound, true);
    $("start").addEventListener("click", function () { if (T.cur >= 0 && !T.ended) { begin(); play(); } else restart(); });
    $("start-over").addEventListener("click", restart);
    $("hdr-tour").addEventListener("click", function (e) { e.preventDefault(); $("tour").scrollIntoView({ behavior: rm() ? "auto" : "smooth", block: "start" }); if (!T.started) { if (T.cur >= 0 && !T.ended) { begin(); play(); } else restart(); } });
    $("play").addEventListener("click", toggle);
    $("next").addEventListener("click", next);
    $("back").addEventListener("click", back);
    $("restart").addEventListener("click", restart);
    $("tour-x").addEventListener("click", exitTour);
    $("sound").addEventListener("click", function () { setSound(!sound); });
    $("again").addEventListener("click", restart);
    $("reset").addEventListener("click", resetDemo);
    $("end-shop").addEventListener("click", leave);
    $("end-store").addEventListener("click", leave);
    $("steps").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-i]");
      if (!b || T.running) return;
      var i = Number(b.getAttribute("data-i"));
      T.playing = false; T.ended = false; $("end").hidden = true;
      if (STEPS[i].done) showSnap(i); else go(i);
    });
    $("stage").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-approve]");
      if (b && !b.disabled) approve(b.getAttribute("data-approve"), b, stage);
    });
    $("fp-stage").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-approve]");
      if (!b || b.disabled) return;
      approve(b.getAttribute("data-approve"), b, fstage).then(function (r) { $("free-status").textContent = (r.status === "paid" ? "Approved and paid." : "Approved.") + " " + LT.payLine(r.amount, r.simulated || r.payout_status === "SIMULATED"); refreshSides(); })
        .catch(function (err) { $("free-status").textContent = errText(err); });
    });
    document.addEventListener("keydown", onKey);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) resync(); });
    window.addEventListener("focus", resync);
    if (LT.RM.addEventListener) LT.RM.addEventListener("change", function () { [stage, fstage].forEach(function (s) { var r = s.el(); if (r) r.classList.toggle("is-rm", rm()); }); });
    $("mode").addEventListener("change", function (e) {
      if (!agent || !e.target.value) return;
      LT.api("PUT", "/api/shopper/prefs", { agent_id: agent.id, mode: e.target.value }).then(function () {
        $("sync-status").textContent = "Scout is now on " + LT.MODE_WORD[e.target.value] + ". The shopper page shows the same.";
        return refreshShopper();
      }).catch(function (err) { $("sync-status").textContent = errText(err); });
    });
    $("tiles").addEventListener("change", function (e) {
      var id = e.target.getAttribute("data-smode");
      if (!id) return;
      LT.api("PUT", "/api/stores/" + encodeURIComponent(id) + "/policy", { mode: e.target.value }).then(function (s) {
        stores = stores.map(function (x) { return x.id === s.id ? s : x; });
        $("sync-status").textContent = s.name + " is now " + LT.MODE_WORD[s.policy.mode] + " (signed and published). The store page shows the same.";
      }).catch(function (err) { $("sync-status").textContent = errText(err); });
    });
    LT.idleHandshake($("hs-panel"), "The latest Handshake shows up here with every check’s detail and its proof pack.");
    $("f-store").addEventListener("change", fillItems);
    $("free").addEventListener("submit", free);
    var loaded = track(load()).catch(function (e) { $("pass").innerHTML = "<p>" + esc(errText(e)) + "</p>"; });
    // /handshake/?tour=1 starts the tour. Sound needs one press first (browsers only speak after a tap).
    if (/[?&]tour=1\b/.test(window.location.search)) {
      $("tour").scrollIntoView({ block: "start" });
      if (sound) { $("cover-note").textContent = "Press Play to start. The tour talks you through it; the speaker button mutes it."; $("start").focus({ preventScroll: true }); }
      else loaded.then(function () { if (!T.started) restart(); });
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
