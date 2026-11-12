/* /shop/ — the shopper control center. Uses /cc.js (window.LT). All data comes from the live API. */
(function () {
  "use strict";
  var LT = window.LT, $ = LT.$, esc = LT.esc, usd = LT.usd;
  var agent = null, prefs = null, stores = [], loading = true, lastQuery = { q: "", max: "" }, killed = false;
  var SLIDERS = ["daily_cap", "per_item_cap", "ask_above", "deal_mode"];

  function step(n) { var el = $("st-" + n); if (el) el.classList.add("is-done"); }
  function status(id, text, kind) {
    var el = $(id);
    el.textContent = text || "";
    el.className = "cc-status" + (kind ? " is-" + kind : "");
  }
  function errText(e) {
    if (e && (e.status === 401 || e.status === 403) && /password/i.test(e.message || "")) return "This server is password-protected. Open the dashboard and sign in first.";
    return (e && e.message) || "Something went wrong. Please try again.";
  }

  // ---------- rules form ----------
  function allCategories() {
    var set = {};
    stores.forEach(function (s) { if (s.policy_signed) (s.policy.categories || []).forEach(function (c) { set[c] = 1; }); });
    (prefs.categories_allowed || []).forEach(function (c) { set[c] = 1; });
    return Object.keys(set).sort();
  }
  function renderCats() {
    var allowed = prefs.categories_allowed || [];
    $("cats").innerHTML = allCategories().map(function (c) {
      return '<button type="button" class="cc-chip" data-cat="' + esc(c) + '" aria-pressed="' + (allowed.indexOf(c) >= 0) + '">' + esc(c) + "</button>";
    }).join("");
  }
  function renderNever() {
    var list = prefs.never_buy || [];
    $("never").innerHTML = list.length ? list.map(function (c) {
      return '<button type="button" class="cc-chip cc-chip--no" data-never="' + esc(c) + '" aria-label="Remove ' + esc(c) + ' from never buy">' + esc(c) + "</button>";
    }).join("") : '<span class="cc-fine">Nothing on the list.</span>';
  }
  function fillForm() {
    loading = true;
    SLIDERS.forEach(function (k) {
      var el = $(k);
      if (prefs[k] != null) el.value = prefs[k];
      LT.syncSlider(el);
    });
    var r = document.querySelector('input[name="guard_strictness"][value="' + (prefs.guard_strictness || "balanced") + '"]');
    if (r) r.checked = true;
    var m = document.querySelector('input[name="mode"][value="' + (prefs.mode || "autopilot") + '"]');
    if (m) m.checked = true;
    syncModeQ();
    renderCats();
    renderNever();
    loading = false;
    syncPassDesign();
  }

  // ---------- Design your Pass: three starting points that set the same inputs a person would ----------
  var PASS_DESIGNS = {
    careful: { label: "Careful", daily_cap: 100, per_item_cap: 40, ask_above: 20, guard_strictness: "strict" },
    balanced: { label: "Balanced", daily_cap: 400, per_item_cap: 200, ask_above: 100, guard_strictness: "balanced" },
    handsoff: { label: "Hands-off", daily_cap: 800, per_item_cap: 400, ask_above: 300, guard_strictness: "relaxed" }
  };
  var PASS_FIELDS = ["daily_cap", "per_item_cap", "ask_above"], designing = false;
  function currentPassDesign() {
    var strict = (document.querySelector('input[name="guard_strictness"]:checked') || {}).value;
    return Object.keys(PASS_DESIGNS).filter(function (k) {
      var d = PASS_DESIGNS[k];
      return d.guard_strictness === strict && PASS_FIELDS.every(function (f) { return Number($(f).value) === d[f]; });
    })[0] || "";
  }
  function syncPassDesign() {
    var cur = currentPassDesign();
    Array.prototype.forEach.call(document.querySelectorAll("[data-pass-design]"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-pass-design") === cur));
    });
    $("pd-custom").hidden = !!cur;
  }
  function setPassDesign(key) {
    var d = PASS_DESIGNS[key];
    if (!d || loading || !agent) return;
    designing = true;
    var r = document.querySelector('input[name="guard_strictness"][value="' + d.guard_strictness + '"]');
    if (r) r.checked = true;
    // Same path as moving a slider by hand: the "input" listener updates the shown value and queues one save.
    PASS_FIELDS.forEach(function (f) {
      var el = $(f);
      el.value = d[f];
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
    designing = false;
    syncPassDesign();
    status("pd-status", d.label + " Pass set.", "ok");
  }
  function bindPassDesign() {
    $("pass-design").addEventListener("click", function (e) {
      var b = e.target.closest("[data-pass-design]");
      if (b) setPassDesign(b.getAttribute("data-pass-design"));
    });
    function changed() { if (designing) return; syncPassDesign(); status("pd-status", ""); }
    $("rules").addEventListener("input", changed);
    $("rules").addEventListener("change", changed);
  }
  function collect() {
    var strict = document.querySelector('input[name="guard_strictness"]:checked');
    return {
      agent_id: agent.id,
      daily_cap: Number($("daily_cap").value), per_item_cap: Number($("per_item_cap").value), ask_above: Number($("ask_above").value),
      deal_mode: Number($("deal_mode").value), guard_strictness: strict ? strict.value : "balanced",
      mode: (document.querySelector('input[name="mode"]:checked') || { value: "autopilot" }).value,
      categories_allowed: prefs.categories_allowed || [], never_buy: prefs.never_buy || []
    };
  }
  var saveNow = function () {
    if (loading || !agent) return;
    status("save-status", "Saving…");
    LT.api("PUT", "/api/shopper/prefs", collect()).then(function (r) {
      prefs = r;
      step(1);
      status("save-status", r.pass_reissued ? "Saved \u2014 " + agent.name + "\u2019s Pass was re-issued with your new rules." :
        "Saved. " + agent.name + " is stopped, so no new Pass was issued. Turn it back on to get one.", "ok");
      loadAgents();
      refreshPass();
      runSearch(true);
    }).catch(function (e) { status("save-status", "Not saved: " + errText(e), "err"); });
  };
  var saveSoon = LT.debounce(saveNow, 600);
  var MODE_SAID = {
    autopilot: "Full Autopilot is on: it buys green matches by itself and asks you on yellow.",
    assist: "AI Assist is on: every purchase now waits for your yes, even green ones.",
    off: "Off: nothing can be bought. Switch back to Full Autopilot or AI Assist any time (this is not Stop all)."
  };
  // The small switch beside the search mirrors the big mode control, so the mode is always in view when shopping.
  var AUTO_SAID = {
    autopilot: ["Just buy the best match", "Type what you want above. {n} picks the best item both sides accept and buys it inside its Pass."],
    assist: ["Pick the best and ask me", "Type what you want above. {n} picks the best item and waits for your yes. No money moves until you say so."],
    off: ["{n} is Off", "{n} is Off, so it can\u2019t buy. Switch to AI Assist or Full Autopilot to let it shop."]
  };
  function syncModeQ() {
    var mode = killed ? "" : ((prefs && prefs.mode) || "autopilot");
    Array.prototype.forEach.call(document.querySelectorAll('input[name="mode-q"]'), function (r) { r.checked = r.value === mode; r.disabled = killed; });
    var nm = agent ? agent.name : "Scout", t = AUTO_SAID[mode] || ["Stopped", nm + " is stopped (Stop all). Turn it back on to shop."];
    $("auto-go").textContent = t[0].replace("{n}", nm);
    $("auto-go").disabled = !mode || mode === "off";
    $("auto-help").textContent = t[1].replace(/\{n\}/g, nm);
  }
  function saveMode() {
    if (loading || !agent) return;
    var v = (document.querySelector('input[name="mode"]:checked') || {}).value;
    status("mode-status", "Saving…");
    LT.api("PUT", "/api/shopper/prefs", { agent_id: agent.id, mode: v }).then(function (r) {
      prefs = r;
      status("mode-status", MODE_SAID[r.mode] || "Saved.", r.mode === "off" ? "" : "ok");
      syncModeQ();
      loadAgents();
      refreshPass();
      runSearch(true);
    }).catch(function (e) { status("mode-status", "Not saved: " + errText(e), "err"); });
  }

  function bindForm() {
    SLIDERS.forEach(function (k) {
      $(k).addEventListener("input", function () { LT.syncSlider(this); if (!loading) { status("save-status", "Changes not saved yet…"); saveSoon(); } });
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="guard_strictness"]'), function (r) { r.addEventListener("change", saveNow); });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="mode"]'), function (r) { r.addEventListener("change", saveMode); });
    $("cats").addEventListener("click", function (e) {
      var b = e.target.closest("[data-cat]");
      if (!b) return;
      var c = b.getAttribute("data-cat"), list = (prefs.categories_allowed || []).slice(), i = list.indexOf(c);
      if (i >= 0) list.splice(i, 1); else list.push(c);
      prefs.categories_allowed = list;
      b.setAttribute("aria-pressed", String(i < 0));
      saveNow();
    });
    $("never").addEventListener("click", function (e) {
      var b = e.target.closest("[data-never]");
      if (!b) return;
      prefs.never_buy = (prefs.never_buy || []).filter(function (x) { return x !== b.getAttribute("data-never"); });
      renderNever();
      saveNow();
      $("never-in").focus();
    });
    function addNever() {
      var v = $("never-in").value.trim().toLowerCase();
      if (!v) return;
      if ((prefs.never_buy || []).indexOf(v) < 0) prefs.never_buy = (prefs.never_buy || []).concat([v]);
      $("never-in").value = "";
      renderNever();
      saveNow();
    }
    $("never-add").addEventListener("click", addNever);
    $("never-in").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); addNever(); } });
    $("rules").addEventListener("submit", function (e) { e.preventDefault(); saveNow(); });
  }

  // ---------- Pass, trust, activity ----------
  function refreshPass() {
    if (!agent) return Promise.resolve();
    return LT.api("GET", "/api/agents/" + agent.id + "/pass").catch(function () { return null; }).then(function (r) {
      var st = LT.passCard($("pass"), r, prefs, agent.name);
      killed = st === "REVOKED";
      $("resume").hidden = !killed;
      // While the Pass is revoked no mode is active, so the mode control must not claim Autopilot.
      Array.prototype.forEach.call(document.querySelectorAll('input[name="mode"]'), function (r) {
        r.disabled = killed;
        r.checked = !killed && prefs && (prefs.mode || "autopilot") === r.value;
      });
      syncModeQ();
      var ms = $("mode-status");
      if (killed) status("mode-status", agent.name + " is stopped (you pressed Stop all), so no mode is active. Turn " + agent.name + " back on to pick one.", "err");
      else if (/stopped \(you pressed Stop all\)/.test(ms.textContent)) status("mode-status", "");
    });
  }
  function refreshSide() {
    if (!agent) return;
    LT.api("GET", "/api/agents/" + agent.id + "/trust").then(function (t) { LT.trustMeter($("trust"), t, stores, agent.name); }).catch(function (e) { $("trust").textContent = errText(e); });
    LT.api("GET", "/api/handshakes?limit=8&agent_id=" + agent.id).then(function (r) { LT.activity($("activity"), r.handshakes); }).catch(function () {});
    moneyWent();
    loadReceipts(true);
  }

  // ---------- Match Finder ----------
  function itemCard(it) {
    var acts = '<button type="button" class="lt-btn lt-btn--ghost" data-do="check">Check both sides</button>';
    if (it.signal === "match") acts += '<button type="button" class="lt-btn lt-btn--primary" data-do="buy">Buy with PayPal sandbox</button>';
    else if (it.signal === "needs_yes") acts += '<button type="button" class="lt-btn lt-btn--amber" data-do="ask">Request my yes</button>';
    var reasons = (it.reasons || []).map(function (r) { return r.split("; "); }).reduce(function (a, b) { return a.concat(b); }, []);
    return '<li class="cc-item sig--' + esc(it.signal) + '" data-id="' + esc(it.id) + '" data-store="' + esc(it.store_id) + '">' +
      '<div class="cc-item-top"><h3>' + esc(it.title) + '</h3><span class="cc-item-price">' + usd(it.price) + "</span></div>" +
      '<p class="cc-item-store"><span>' + esc(it.store_name) + " (demo store)</span>" + LT.gpBadge(it.guardpooch_verdict) + LT.storeBadge(it.store_id) + "</p>" +
      LT.sigPill(it.signal) +
      (reasons.length ? "<ul>" + reasons.slice(0, 3).map(function (r) { return "<li>" + esc(LT.tidy(r)) + "</li>"; }).join("") + "</ul>"
        : '<p class="cc-fine" style="margin:0">Inside your rules and the store’s rules.</p>') +
      '<div class="cc-item-acts">' + acts + "</div></li>";
  }
  var items = {};
  // Filters work on what the search already found, so they are instant and never hide a rule's answer.
  var found = [], filt = { sig: "", store: "", cat: "", sort: "" };
  function fillFilters() {
    $("filters").hidden = !found.length;
    function opts(id, first, pairs, cur) {
      var keep = pairs.some(function (p) { return p[0] === cur; }) ? cur : "";
      $(id).innerHTML = '<option value="">' + first + "</option>" + pairs.map(function (p) {
        return '<option value="' + esc(p[0]) + '"' + (p[0] === keep ? " selected" : "") + ">" + esc(p[1]) + "</option>";
      }).join("");
      return keep;
    }
    var st = {}, ct = {};
    found.forEach(function (it) { st[it.store_id] = it.store_name; if (it.category) ct[it.category] = it.category; });
    filt.store = opts("f-store", "All stores", Object.keys(st).map(function (k) { return [k, st[k]]; }), filt.store);
    filt.cat = opts("f-cat", "All categories", Object.keys(ct).sort().map(function (k) { return [k, k]; }), filt.cat);
    var n = { "": found.length, match: 0, needs_yes: 0, no_match: 0 };
    found.forEach(function (it) { n[it.signal] = (n[it.signal] || 0) + 1; });
    Array.prototype.forEach.call(document.querySelectorAll("#filters [data-sig]"), function (b) {
      var k = b.getAttribute("data-sig");
      if (!b.dataset.label) b.dataset.label = b.textContent;
      b.textContent = b.dataset.label + " (" + (n[k] || 0) + ")";
      b.setAttribute("aria-pressed", String(k === filt.sig));
    });
  }
  function renderResults() {
    var list = found.filter(function (it) {
      return (!filt.sig || it.signal === filt.sig) && (!filt.store || it.store_id === filt.store) && (!filt.cat || it.category === filt.cat);
    });
    if (filt.sort === "low") list = list.slice().sort(function (a, b) { return a.price - b.price; });
    if (filt.sort === "high") list = list.slice().sort(function (a, b) { return b.price - a.price; });
    if (filt.sort === "trust") list = list.slice().sort(function (a, b) { return ((b.store_trust || {}).score || 0) - ((a.store_trust || {}).score || 0); });
    $("results").innerHTML = list.length ? list.map(itemCard).join("") :
      '<li class="cc-empty"><b>Nothing fits those filters.</b><br>' + found.length + " found in all. Press Clear filters to see them.</li>";
  }
  function bindFilters() {
    $("filters").addEventListener("click", function (e) {
      var b = e.target.closest("[data-sig]");
      if (b) { filt.sig = b.getAttribute("data-sig"); fillFilters(); renderResults(); return; }
      if (e.target.id === "f-clear") { filt = { sig: "", store: "", cat: "", sort: "" }; $("f-sort").value = ""; fillFilters(); renderResults(); }
    });
    $("filters").addEventListener("change", function () {
      filt.store = $("f-store").value; filt.cat = $("f-cat").value; filt.sort = $("f-sort").value;
      renderResults();
    });
  }
  function runSearch(quiet) {
    if (!agent) return Promise.resolve();
    var qs = "?agent_id=" + agent.id + (lastQuery.q ? "&q=" + encodeURIComponent(lastQuery.q) : "") + (lastQuery.max ? "&max_price=" + encodeURIComponent(lastQuery.max) : "");
    if (!quiet) status("find-status", agent.name + " is looking\u2026");
    return Promise.all([LT.api("GET", "/api/catalog" + qs), LT.api("GET", "/api/stores?agent_id=" + agent.id).catch(function () { return null; })]).then(function (both) {
      var r = both[0];
      if (both[1]) { stores = both[1].stores || stores; LT.regStores(stores); }
      items = {};
      (r.items || []).forEach(function (it) { items[it.store_id + "/" + it.id] = it; LT.regStore(it.store_id, it.store_name, it.store_trust); });
      var n = { match: 0, needs_yes: 0, no_match: 0 };
      (r.items || []).forEach(function (it) { n[it.signal] = (n[it.signal] || 0) + 1; });
      found = r.items || [];
      fillFilters();
      $("find-ask").setAttribute("data-ask", lastQuery.q ? "Find me " + lastQuery.q + (lastQuery.max ? " under $" + lastQuery.max : "") : "Find me a drill under $90");
      if (found.length) renderResults();
      else $("results").innerHTML =
        '<li class="cc-empty"><b>' + esc(agent.name) + ' found nothing' + (lastQuery.q ? " for “" + esc(lastQuery.q) + "”" : "") + (lastQuery.max ? " under " + usd(lastQuery.max) : "") +
        ".</b><br>The demo stores sell tools, hardware, lumber, paint, garden, office, electronics and pet supplies. Try a quick pick.</li>";
      if (!quiet || (r.items || []).length) {
        status("find-status", (r.items || []).length ? (r.items.length + " found: " + n.match + " green, " + n.needs_yes + " yellow, " + n.no_match + " red.") : "No matches.");
      }
    }).catch(function (e) { status("find-status", errText(e), "err"); });
  }
  function bindSearch() {
    // Arriving from the home page's search bar (/shop/?q=...): start with that search and show the results.
    var q0 = "";
    try { q0 = (new URLSearchParams(location.search).get("q") || "").trim().slice(0, 80); } catch (e) { /* old browser */ }
    if (q0) {
      $("q").value = q0;
      lastQuery = { q: q0, max: "" };
      step(2);
      setTimeout(function () { $("find").scrollIntoView({ behavior: "auto", block: "start" }); }, 700);
    }
    $("search").addEventListener("submit", function (e) {
      e.preventDefault();
      lastQuery = { q: $("q").value.trim(), max: $("maxp").value.trim() };
      step(2);
      runSearch();
    });
    document.querySelector('[aria-label="Quick picks"]').addEventListener("click", function (e) {
      var b = e.target.closest("[data-q]");
      if (!b) return;
      $("q").value = b.getAttribute("data-q");
      lastQuery = { q: $("q").value, max: $("maxp").value.trim() };
      step(2);
      runSearch();
    });
    $("results").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-do]");
      if (!b) return;
      var li = b.closest(".cc-item");
      var it = items[li.getAttribute("data-store") + "/" + li.getAttribute("data-id")];
      if (!it) return;
      step(2);
      runHandshake(it, b.getAttribute("data-do") !== "check");
    });
  }

  // ---------- say it, and the shopper acts (POST /api/shopper/autopilot) ----------
  function autoGo() {
    if (busy || !agent) return;
    var q = $("q").value.trim(), mx = $("maxp").value.trim();
    if (!q) { status("find-status", "Type what to buy first, like \u201ccordless drill under $90\u201d.", "err"); $("q").focus(); return; }
    busy = true;
    var said = null;
    $("auto-go").disabled = true;
    status("find-status", agent.name + " is shopping\u2026");
    step(2);
    LT.api("POST", "/api/shopper/autopilot", { agent_id: agent.id, ask: q + (mx ? " under $" + mx : "") }).then(function (r) {
      var hs = r.handshake, paid = hs && hs.signal === "match" && hs.order;
      said = [r.said, paid ? "ok" : hs && hs.signal === "needs_yes" ? "" : "err"];
      status("find-status", said[0], said[1]);
      lastQuery = { q: r.understood.q, max: r.understood.max_price == null ? "" : String(r.understood.max_price) };
      if (!hs) return;
      step(3);
      if (paid) step(4);
      var it = r.picked;
      $("hs").scrollIntoView({ behavior: LT.RM.matches ? "auto" : "smooth", block: "start" });
      return LT.renderHandshake($("hs-panel"), hs, {
        focus: true,
        onBuy: function () { busy = false; runHandshake(it, true); },
        onAsk: function () { busy = false; runHandshake(it, true); },
        onApprove: function (h, btn) { decide(h, btn, "approve"); },
        onDeny: function (h, btn) { decide(h, btn, "deny"); },
        onRated: function () { refreshSide(); loadAgents(); runSearch(true); }
      });
    }).catch(function (e) { status("find-status", "That didn\u2019t work: " + errText(e), "err"); })
      .then(function () {
        busy = false; syncModeQ(); refreshSide(); refreshPass(); loadAgents();
        return runSearch(true).then(function () { if (said) status("find-status", said[0], said[1]); });   // keep what the shopper did on screen
      });
  }

  // ---------- receipts ----------
  var rcQuery = "";
  var RC_WORD = { paid: "Paid", refunded: "Refunded", waiting: "Waiting for your yes", blocked: "Blocked, nothing paid", added: "Added by you", done: "Done" };
  function rcRow(r) {
    var d = new Date(r.ts * 1000);
    var when = d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    return '<li class="cc-rc-row st--' + esc(r.status) + '"><div class="cc-rc-main"><b>' + esc(r.title) + "</b><span>" + esc(r.store) +
      (r.kind === "lowtides" ? " (demo store)" : (r.kind === "action" ? " (demo business)" : "")) + " \u00b7 " + esc(when) + (r.shopper ? " \u00b7 " + esc(r.shopper) : "") + "</span>" +
      (r.payment ? '<span class="cc-rc-pay">' + esc(r.payment) + "</span>" : "") + "</div>" +
      '<div class="cc-rc-side"><span class="cc-rc-amt">' + (r.kind === "action" && !r.amount ? "no charge" : usd(r.amount)) + '</span><span class="cc-rc-st">' + esc(r.kind === "action" && r.status === "blocked" ? "Blocked, nothing happened" : (RC_WORD[r.status] || r.status)) + "</span>" +
      (r.proof_url ? '<a href="' + esc(r.proof_url) + '" download="lowtides-proof-' + esc(r.id) + '.json">Signed proof</a>'
        : '<button type="button" class="cc-rc-x" data-rc="' + esc(r.id) + '">Remove</button>') + "</div></li>";
  }
  // ---------- receipts: Activity panel + CSV, built from the list loadReceipts() already fetched ----------
  var rcLoaded = [];
  // Same status words the rows use. Receipts you added yourself are your notes, not something the shopper did, so they are not in the bars.
  var RC_GROUP = { paid: "yes", done: "yes", refunded: "yes", waiting: "ask", blocked: "no" };
  var RC_BARS = [["yes", "Both said yes"], ["ask", "Waiting for your yes now"], ["no", "Blocked purchases"]];
  function rcResult(r) {
    return r.kind === "action" && r.status === "blocked" ? "Blocked, nothing happened" : (RC_WORD[r.status] || r.status || "");
  }
  function renderRcChart() {
    var n = { yes: 0, ask: 0, no: 0 }, paid = 0;
    rcLoaded.forEach(function (r) {
      var g = RC_GROUP[r.status];
      if (g) n[g]++;
      if (r.status === "paid") paid += Number(r.amount) || 0;
    });
    var body = $("ra-body"), all = n.yes + n.ask + n.no, max = Math.max(n.yes, n.ask, n.no, 1);
    body.textContent = "";
    if (!all) {
      var none = document.createElement("p");
      none.className = "cc-fine ra-none";
      none.textContent = "No activity yet. Let Scout shop and it shows up here.";
      body.appendChild(none);
    } else {
      var ul = document.createElement("ul");
      ul.className = "ra-bars";
      RC_BARS.forEach(function (bar) {
        var li = document.createElement("li"), name = document.createElement("span"), dot = document.createElement("i"),
          track = document.createElement("span"), fill = document.createElement("i"), count = document.createElement("b");
        li.className = "ra--" + bar[0];
        name.className = "ra-name";
        dot.className = "ra-dot";
        dot.setAttribute("aria-hidden", "true");
        name.appendChild(dot);
        name.appendChild(document.createTextNode(bar[1]));
        track.className = "ra-track";
        track.setAttribute("aria-hidden", "true");
        fill.className = "ra-fill";
        fill.style.width = (n[bar[0]] ? Math.max(4, Math.round(n[bar[0]] / max * 100)) : 0) + "%";
        track.appendChild(fill);
        count.className = "ra-count";
        count.textContent = String(n[bar[0]]);
        li.appendChild(name); li.appendChild(track); li.appendChild(count);
        ul.appendChild(li);
      });
      body.appendChild(ul);
    }
    $("ra-total").textContent = all ? "Total paid: " + usd(paid) + (rcQuery ? " (in this search)" : "") : "";
    $("ra-csv").disabled = !rcLoaded.length;
  }
  function csvField(v) {
    var s = String(v == null ? "" : v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;   // stop a spreadsheet from running the field as a formula
    return '"' + s.replace(/"/g, '""') + '"';
  }
  function rcCsv() {
    function two(x) { return (x < 10 ? "0" : "") + x; }
    var rows = [["date", "store or business", "item or action", "amount", "result", "receipt id"]];
    rcLoaded.forEach(function (r) {
      var d = new Date(r.ts * 1000);
      rows.push([isNaN(d) ? "" : d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()),
        r.store, r.title, (Number(r.amount) || 0).toFixed(2), rcResult(r), r.id]);
    });
    return rows.map(function (row) { return row.map(csvField).join(","); }).join("\r\n") + "\r\n";
  }
  function downloadRcCsv() {
    if (!rcLoaded.length) return;
    var url = URL.createObjectURL(new Blob(["﻿" + rcCsv()], { type: "text/csv;charset=utf-8" }));
    var a = document.createElement("a");
    a.href = url;
    a.download = "lowtides-receipts.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function loadReceipts(quiet) {
    return LT.api("GET", "/api/receipts" + (rcQuery ? "?q=" + encodeURIComponent(rcQuery) : "")).then(function (r) {
      $("rc-list").innerHTML = r.receipts.map(rcRow).join("");
      rcLoaded = r.receipts || [];
      renderRcChart();
      if (!quiet || rcQuery || !r.total) status("rc-status", r.summary, r.count || !r.total ? "" : "err");
      else status("rc-status", r.summary);
    }).catch(function (e) { status("rc-status", errText(e), "err"); });
  }
  function bindReceipts() {
    $("ra-csv").addEventListener("click", downloadRcCsv);
    $("rc-search").addEventListener("submit", function (e) { e.preventDefault(); rcQuery = $("rc-q").value.trim(); loadReceipts(); });
    $("rc-picks").addEventListener("click", function (e) {
      var b = e.target.closest("[data-rq]");
      if (!b) return;
      rcQuery = $("rc-q").value = b.getAttribute("data-rq");
      loadReceipts();
    });
    $("rc-list").addEventListener("click", function (e) {
      var b = e.target.closest("[data-rc]");
      if (!b) return;
      b.disabled = true;
      LT.api("POST", "/api/receipts/delete", { id: b.getAttribute("data-rc") }).then(function () { loadReceipts(); })
        .catch(function (er) { b.disabled = false; status("rc-status", errText(er), "err"); });
    });
    $("rc-add").addEventListener("submit", function (e) {
      e.preventDefault();
      var body = {}, amt = $("rc-amount").value.trim();
      if ($("rc-store").value.trim()) body.store = $("rc-store").value.trim();
      if ($("rc-title").value.trim()) body.title = $("rc-title").value.trim();
      if (amt) body.amount = Number(amt);
      if ($("rc-date").value) body.bought_on = $("rc-date").value;
      if ($("rc-text").value.trim()) body.text = $("rc-text").value;
      status("rc-add-status", "Adding\u2026");
      LT.api("POST", "/api/receipts", body).then(function (r) {
        status("rc-add-status", "Added: " + r.title + " at " + r.store + ", " + usd(r.amount) + ".", "ok");
        $("rc-add").reset();
        rcQuery = $("rc-q").value = "";
        loadReceipts();
      }).catch(function (er) { status("rc-add-status", errText(er), "err"); });
    });
  }

  // ---------- Report this store (lowtides/reports.py) ----------
  // Shared with /shop/muzzle.js through window.LowtidesReport. Every piece of data goes in with textContent.
  var rpList = null, rpAt = 0;
  function rpEl(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function rpReports() {
    if (!rpList || Date.now() - rpAt > 3000) {
      rpAt = Date.now();
      rpList = LT.api("GET", "/api/reports").then(function (r) { return (r && Array.isArray(r.reports)) ? r.reports : []; }, function () { return []; });
    }
    return rpList;
  }
  function rpSafeUrl(u, outside) {
    u = String(u || "");
    return (outside ? /^https:\/\//.test(u) : /^\/api\/[A-Za-z0-9_\/-]+$/.test(u)) ? u : "";
  }
  function rpPanel(rep, already) {
    var box = rpEl("div", "rp-panel");
    box.setAttribute("tabindex", "-1");
    box.appendChild(rpEl("p", "rp-saved", already ? "Reported by you" : "Report saved."));
    if (rep.demo_note) box.appendChild(rpEl("p", "rp-demo", rep.demo_note));
    var why = Array.isArray(rep.reasons) ? rep.reasons : [];
    if (why.length) {
      box.appendChild(rpEl("p", "rp-h", "Why " + ((rep.store && rep.store.name) || "this store") + " was refused"));
      var ul = rpEl("ul", "rp-reasons");
      why.forEach(function (t) { ul.appendChild(rpEl("li", "", t)); });
      box.appendChild(ul);
    }
    var links = rpEl("p", "rp-links");
    [[rep.report_url, "Download signed report", "lowtides-report-" + rep.id + ".json"],
     [rep.evidence_url, "Download signed evidence", "lowtides-evidence-" + ((rep.decision && rep.decision.id) || rep.id) + ".json"]].forEach(function (l) {
      var href = rpSafeUrl(l[0], false);
      if (!href) return;
      var a = rpEl("a", "rp-link", l[1]);
      a.href = href;
      a.setAttribute("download", String(l[2]).replace(/[^A-Za-z0-9_.-]/g, "_"));
      links.appendChild(a);
    });
    if (links.firstChild) box.appendChild(links);
    var official = (Array.isArray(rep.official) ? rep.official : []).filter(function (o) { return o && rpSafeUrl(o.url, true); });
    if (official.length) {
      box.appendChild(rpEl("p", "rp-h", "Report real fraud to the authorities"));
      var ol = rpEl("ul", "rp-official");
      official.forEach(function (o) {
        var li = rpEl("li"), a = rpEl("a", "rp-link", o.label || o.name || o.url);
        a.href = rpSafeUrl(o.url, true);
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        li.appendChild(a);
        ol.appendChild(li);
      });
      box.appendChild(ol);
    }
    return box;
  }
  // kind: "handshake" or "action". Puts the button (or "Reported by you" and the panel) at the end of `host`.
  function rpMount(host, kind, id) {
    if (!host || !id) return;
    var wrap = rpEl("div", "rp");
    wrap.setAttribute("data-rp", String(id));
    host.appendChild(wrap);
    var show = function (rep, already) {
      while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
      var panel = rpPanel(rep, already);
      wrap.appendChild(panel);
      return panel;
    };
    rpReports().then(function (list) {
      var mine = list.filter(function (r) { return r && r.decision && r.decision.kind === kind && r.decision.id === id; })[0];
      if (mine) { show(mine, true); return; }
      var btn = rpEl("button", "lt-btn lt-btn--ghost rp-btn", "Report this store");
      btn.type = "button";
      var msg = rpEl("p", "rp-err");
      msg.setAttribute("role", "alert");
      btn.addEventListener("click", function () {
        btn.disabled = true;
        msg.textContent = "";
        var body = {};
        body[kind === "action" ? "action_id" : "handshake_id"] = id;
        LT.api("POST", "/api/reports", body).then(function (rep) {
          rpList = null;
          var panel = show(rep, false);
          try { panel.focus({ preventScroll: true }); } catch (e) { panel.focus(); }
        }, function (e) {
          btn.disabled = false;
          msg.textContent = "The report was not saved. " + errText(e);
        });
      });
      wrap.appendChild(btn);
      wrap.appendChild(msg);
    });
  }
  function rpFake(hs) {
    if (!hs || hs.signal !== "no_match" || !hs.id) return false;
    if ((hs.guardpooch || {}).verdict === "danger") return true;
    return (hs.shopper_checks || []).concat(hs.store_checks || []).some(function (c) { return c && c.id === "guardpooch" && c.level === "fail"; });
  }
  window.LowtidesReport = { mount: rpMount };

  // ---------- Handshake ----------
  var busy = false;
  function runHandshake(it, execute) {
    if (busy) return;
    busy = true;
    Array.prototype.forEach.call(document.querySelectorAll("#results button"), function (b) { b.disabled = true; });
    var panel = $("hs-panel");
    LT.idleHandshake(panel, agent.name + " is asking " + it.store_name + " for " + it.title + "…");
    $("hs").scrollIntoView({ behavior: LT.RM.matches ? "auto" : "smooth", block: "start" });
    LT.api("POST", "/api/handshake", { agent_id: agent.id, store_id: it.store_id, item_id: it.id, execute: !!execute })
      .then(function (hs) {
        step(3);
        if (hs.signal === "match" && hs.order) step(4);
        return LT.renderHandshake(panel, hs, {
          focus: true,
          onBuy: function () { busy = false; runHandshake(it, true); },
          onAsk: function () { busy = false; runHandshake(it, true); },
          onApprove: function (h, btn) { decide(h, btn, "approve"); },
          onDeny: function (h, btn) { decide(h, btn, "deny"); },
          onRated: function () { refreshSide(); loadAgents(); runSearch(true); }
        });
      })
      .then(function (hs) {
        var root = panel.querySelector(".hsr");
        if (root && rpFake(hs)) rpMount(root, "handshake", hs.id);
      })
      .catch(function (e) { LT.idleHandshake(panel, "That didn’t work: " + errText(e)); })
      .then(function () {
        busy = false;
        refreshSide();
        refreshPass();
        loadAgents();
        runSearch(true);
      });
  }
  function decide(hs, btn, act) {
    var out = btn.closest(".hsr-out");
    Array.prototype.forEach.call(out.querySelectorAll("button"), function (b) { b.disabled = true; });
    LT.decide(hs.approval_id, act).then(function (r) {
      if (act === "approve") step(4);
      out.innerHTML = LT.decisionHtml(r, act, hs.id) + '<div class="hsr-acts"><a class="lt-btn lt-btn--ghost" href="/api/handshakes/' + encodeURIComponent(hs.id) + '/proof" download="lowtides-proof-' + esc(hs.id) + '.json">Download proof pack</a></div>';
      out.focus({ preventScroll: true });
      refreshSide();
      runSearch(true);
    }).catch(function (e) {
      Array.prototype.forEach.call(out.querySelectorAll("button"), function (b) { b.disabled = false; });
      var p = document.createElement("p");
      p.setAttribute("role", "alert");
      p.textContent = (act === "approve" ? "Couldn’t approve: " : "Couldn’t deny: ") + errText(e);
      out.appendChild(p);
    });
  }

  // ---------- Stop all, resume, reset ----------
  function kill(btn) {
    btn.setAttribute("aria-pressed", "true");
    LT.killFlow().then(function (r) {
      btn.setAttribute("aria-pressed", "false");
      if (!r) return;
      status("kill-status", "Stop all pressed. Every AI shopper’s Pass is REVOKED and is now refused at every store." + (r.payouts_cancelled ? " " + r.payouts_cancelled + " waiting payouts were canceled." : ""), "err");
      return refreshPass().then(function () { runSearch(true); refreshSide(); loadAgents(); $("pass").scrollIntoView({ behavior: LT.RM.matches ? "auto" : "smooth", block: "center" }); });
    }).catch(function (e) { btn.setAttribute("aria-pressed", "false"); status("kill-status", "Stop all failed: " + errText(e), "err"); });
  }
  function resumeAgent(a, where) {
    status(where, "Turning " + a.name + " back on…");
    return LT.api("POST", "/api/agents/" + a.id + "/resume", {})
      .then(function () { return LT.api("POST", "/api/agents/" + a.id + "/pass", {}); })
      .then(function () { status(where, a.name + " is back on with a fresh Pass.", "ok"); return refreshAll(); })
      .catch(function (e) { status(where, "Couldn’t turn " + a.name + " back on: " + errText(e), "err"); });
  }
  function reset() {
    LT.confirm("Start the demo over?", "This clears your demo sandbox: your AI shoppers, their rules, purchases and history go back to the start. It only touches test data.", "Yes, reset", false)
      .then(function (ok) {
        if (!ok) return;
        status("kill-status", "Resetting…");
        return LT.api("POST", "/api/demo/reset", {}).then(function () {
          Array.prototype.forEach.call(document.querySelectorAll(".cc-stepper li"), function (li) { li.classList.remove("is-done"); });
          LT.idleHandshake($("hs-panel"), "Pick a match above and press Check both sides or Buy. The result shows up here.");
          status("save-status", "");
          remember(null);
          return load(true);
        }).then(function () { status("kill-status", "Demo reset. Scout is back at the start.", "ok"); });
      }).catch(function (e) { status("kill-status", "Reset failed: " + errText(e), "err"); });
  }

  // ---------- Connect your own AI ----------
  var conn = null, connKind = "mcp";
  var CONN_NOTES = {
    mcp: "For AI apps that take MCP servers. Paste into the app's MCP settings, with the Lowtides code folder on that computer (python -m lowtides.mcp). Your AI gets seven tools: list_stores, handshake, request_action, get_pass, get_budget, request_spend, check_spend.",
    curl: "For any agent that can make a web call. First line: check both sides. Add \"execute\": true to pay on a match.",
    python: "Plain Python, nothing to install. Check both sides, then pay only on a match."
  };
  function connCode() {
    if (!conn) return "Loading…";
    var o = conn.base_url, k = conn.key;
    if (connKind === "mcp") {
      return ["{", '  "mcpServers": {', '    "lowtides": {', '      "command": "python",', '      "args": ["-m", "lowtides.mcp"],',
        '      "env": { "LOWTIDES_URL": "' + o + '", "LOWTIDES_AGENT_KEY": "' + k + '" }', "    }", "  }", "}"].join("\n");
    }
    if (connKind === "curl") {
      return ["# 1. See the stores, their rules for AI shoppers, and what they sell",
        "curl " + o + '/v1/stores -H "Authorization: Bearer ' + k + '"', "",
        "# 2. Check both sides before buying (nothing is paid)",
        "curl -X POST " + o + "/v1/handshake \\", '  -H "Authorization: Bearer ' + k + '" \\', '  -H "Content-Type: application/json" \\',
        "  -d '{\"store_id\":\"harbor\",\"item_id\":\"harbor-tape\"}'", "",
        '# 3. Same call with "execute": true pays on a match (PayPal sandbox)'].join("\n");
    }
    return ["import json, urllib.request", "", 'BASE, KEY = "' + o + '", "' + k + '"', "",
      "def handshake(store_id, item_id, execute=False):",
      '    req = urllib.request.Request(BASE + "/v1/handshake", method="POST",',
      '        data=json.dumps({"store_id": store_id, "item_id": item_id, "execute": execute}).encode(),',
      '        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})',
      "    return json.load(urllib.request.urlopen(req))", "",
      'answer = handshake("harbor", "harbor-tape")           # check only',
      'print(answer["signal"], "-", answer["headline"])      # match / needs_yes / no_match',
      'if answer["signal"] == "match":',
      '    paid = handshake("harbor", "harbor-tape", execute=True)',
      '    print(paid["next"])'].join("\n");
  }
  function renderConn() {
    $("connect-code").textContent = connCode();
    $("connect-note").textContent = CONN_NOTES[connKind];
    Array.prototype.forEach.call(document.querySelectorAll("#connect [data-kind]"), function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.kind === connKind));
    });
  }
  function loadConn() {
    if (!agent || !$("connect-more").open) return;
    if (conn && conn.agent.id === agent.id) return renderConn();
    conn = null;
    renderConn();
    LT.api("GET", "/api/shopper/connect?agent_id=" + agent.id).then(function (r) { conn = r; status("connect-status", ""); renderConn(); })
      .catch(function (e) { $("connect-code").textContent = ""; status("connect-status", errText(e), "err"); });
  }
  $("connect-more").addEventListener("toggle", loadConn);
  $("connect").addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-kind]");
    if (b) { connKind = b.dataset.kind; renderConn(); return; }
    if (ev.target.id !== "connect-copy" || !conn) return;
    var text = connCode(), btn = ev.target;
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () {
      btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 1500);
    }).catch(function () { status("connect-status", "Select the text above and copy it.", "err"); });
  });

  // ---------- My AI shoppers ----------
  var agents = [];
  function remember(id) { try { if (id) localStorage.setItem("lt_shop_agent", String(id)); else localStorage.removeItem("lt_shop_agent"); } catch (e) { /* private mode */ } }
  function remembered() { try { return Number(localStorage.getItem("lt_shop_agent")) || null; } catch (e) { return null; } }
  function setNames() {
    Array.prototype.forEach.call(document.querySelectorAll(".agent-name"), function (el) { el.textContent = agent ? agent.name : "Scout"; });
  }
  function paypalLine(a) {
    var p = a.paypal || {};
    var on = p.status === "active";
    return "PayPal on file: " + esc(on ? "connected" : (p.label || "not connected")) + (p.simulated ? " (simulated in this demo)" : "");
  }
  function renderAgents() {
    $("agents").innerHTML = agents.map(function (a) {
      var on = agent && a.id === agent.id;
      var pct = a.daily_cap ? Math.min(100, Math.round(a.spent_today / a.daily_cap * 100)) : 0;
      var stopped = a.pass_state === "REVOKED" || a.status === "stopped";
      var pst = a.mode === "off" && a.pass_state === "VALID" ? "OFF" : a.pass_state;
      var tone = stopped ? " is-revoked" : pst === "HELD" ? " is-held" : pst === "OFF" ? " is-off" : "";
      return '<li class="cc-agent' + (on ? " is-on" : "") + tone + '" data-id="' + a.id + '">' +
        '<div class="cc-agent-top"><img class="cc-agent-pic" src="/brand/scout/scout-' + (stopped || pst === "OFF" ? 4 : pst === "HELD" ? 3 : on ? 2 : 1) + '.webp" width="56" height="56" alt="" loading="lazy"><h3>' + esc(a.name) + '</h3><span class="cc-mode-tag cc-mode-tag--' + esc(a.mode) + '">' + esc(LT.MODE_WORD[a.mode] || a.mode) + "</span></div>" +
        "<p>Pass: <b class=\"cc-pstate--" + esc(pst) + '">' + esc(pst) + "</b> · Trust <b>" + a.trust + "</b> (heuristic)</p>" +
        "<p>Today: " + usd(a.spent_today) + " of " + usd(a.daily_cap, 0) + "</p>" +
        '<div class="cc-spend" role="img" aria-label="' + pct + '% of today’s budget used"><i style="width:' + pct + '%"></i></div>' +
        "<p>" + paypalLine(a) + "</p>" +
        '<div class="cc-agent-acts">' +
        (on ? '<button type="button" class="lt-btn lt-btn--primary" disabled aria-pressed="true">Selected</button>'
            : '<button type="button" class="lt-btn lt-btn--ghost" data-a="pick" aria-pressed="false">Select<span class="cc-sr"> ' + esc(a.name) + "</span></button>") +
        '<button type="button" class="lt-btn lt-btn--ghost" data-a="rename">Rename</button>' +
        (stopped ? '<button type="button" class="lt-btn lt-btn--ghost" data-a="resume">Turn back on</button>'
                 : '<button type="button" class="lt-btn lt-btn--ghost" data-a="off">' + (a.mode === "off" ? "Switch on" : "Off") + "</button>" +
                   '<button type="button" class="lt-btn lt-btn--danger" data-a="kill">Stop</button>') +
        "</div></li>";
    }).join("") || '<li class="cc-empty">No AI shoppers yet.</li>';
  }
  function loadAgents() {
    return LT.api("GET", "/api/shopper/agents").then(function (r) {
      agents = r.agents || [];
      if (agent) agents.forEach(function (a) { if (a.id === agent.id) agent.name = a.name; });
      renderAgents();
      setNames();
      return agents;
    });
  }
  function pick(id) {
    var a = agents.filter(function (x) { return x.id === id; })[0];
    if (!a) return Promise.resolve();
    agent = { id: a.id, name: a.name };
    remember(a.id);
    renderAgents();
    setNames();
    loadConn();
    LT.idleHandshake($("hs-panel"), "Pick a match above and press Check both sides or Buy. The result shows up here.");
    status("agents-status", a.name + " is selected. Rules, matches and the Handshake below are now for " + a.name + ".", "ok");
    return loadAgent();
  }
  function agentAction(btn) {
    var li = btn.closest(".cc-agent"), id = Number(li.getAttribute("data-id"));
    var a = agents.filter(function (x) { return x.id === id; })[0];
    if (!a) return;
    var act = btn.getAttribute("data-a");
    if (act === "pick") return pick(id);
    if (act === "rename") {
      return LT.prompt("Rename " + a.name, "Give this AI shopper a name you will recognize.", "New name", a.name).then(function (name) {
        if (!name) return;
        return LT.api("POST", "/api/agents/" + id + "/update", { name: name.slice(0, 60) }).then(function () {
          status("agents-status", "Renamed to " + name + ".", "ok");
          return loadAgents().then(function () { if (agent && agent.id === id) refreshPass(); });
        });
      }).catch(function (e) { status("agents-status", "Couldn’t rename: " + errText(e), "err"); });
    }
    if (act === "off") {
      var next = a.mode === "off" ? "autopilot" : "off";
      return LT.api("PUT", "/api/shopper/prefs", { agent_id: id, mode: next }).then(function () {
        status("agents-status", next === "off" ? a.name + " is Off: it can’t buy anything until you switch it on." : a.name + " is on Autopilot again.", next === "off" ? "err" : "ok");
        return refreshAll();
      }).catch(function (e) { status("agents-status", errText(e), "err"); });
    }
    if (act === "resume") return resumeAgent(a, "agents-status");
    if (act === "kill") {
      return LT.confirm("Stop " + a.name + "?", "This revokes " + LT.poss(a.name) + " Pass at every store and cancels its purchases still waiting to pay, along with payouts a store has not collected yet (those show as Refunded). Your other AI shoppers keep working.", "Yes, stop " + a.name, true).then(function (ok) {
        if (!ok) return;
        return LT.api("POST", "/api/agents/" + id + "/kill", {}).then(function () {
          status("agents-status", a.name + " was stopped. Its Pass is revoked at every store.", "err");
          return refreshAll();
        });
      }).catch(function (e) { status("agents-status", "Stop failed: " + errText(e), "err"); });
    }
  }
  function addAgent(btn) {
    var preset = btn.getAttribute("data-preset");
    btn.disabled = true;
    status("agents-status", "Adding…");
    LT.api("POST", "/api/shopper/agents", { preset: preset }).then(function (r) {
      return loadAgents().then(function () { return pick(r.agent.id); }).then(function () {
        status("agents-status", r.agent.name + " joined with its own signed Pass and is selected. New AI shoppers start at trust 40 (a heuristic), so picky stores may say no at first.", "ok");
      });
    }).catch(function (e) { status("agents-status", errText(e), "err"); }).then(function () { btn.disabled = false; });
  }
  function refreshAll() {
    return loadAgents().then(function () {
      return LT.api("GET", "/api/shopper/prefs?agent_id=" + agent.id);
    }).then(function (p) {
      prefs = p;
      fillForm();
      refreshSide();
      return Promise.all([refreshPass(), runSearch(true)]);
    });
  }

  // ---------- plain-English rules ----------
  var compiled = null;
  function plainGo() {
    var text = $("plain").value.trim();
    if (!text) { $("plain").focus(); return; }
    $("plain-go").disabled = true;
    $("plain-out").innerHTML = "<p>Reading your words…</p>";
    LT.api("POST", "/api/rules/compile", { text: text }).then(function (r) {
      // Only the rules the text actually mentioned are kept; everything else stays as the agent has it.
      var all = r.rules || {}, given = r.set || Object.keys(all);
      compiled = {};
      given.forEach(function (k) { if (k in all) compiled[k] = all[k]; });
      var lines = r.plain_set || r.plain || [];
      var src = r.source === "claude" ? "Translated by AI. Every purchase is still checked by plain code, never by the AI." :
        "Read by the built-in rule reader (the AI is not connected here). Check each line.";
      $("plain-out").innerHTML = '<div class="cc-note" style="margin-top:8px !important"><b>This is what it understood and will change:</b><ul style="margin:6px 0">' +
        (lines.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") || "<li>Nothing it could turn into a rule.</li>") + "</ul>" +
        (Object.keys(compiled).length ? '<p class="cc-fine">Only these will change. Every other rule for ' + esc(agent.name) + " stays as it is.</p>" : "") +
        ((r.notes || []).length ? '<p class="cc-fine">' + esc(r.notes.join(" ")) + "</p>" : "") + '<p class="cc-fine">' + esc(src) + "</p>" +
        (Object.keys(compiled).length ? '<button type="button" class="lt-btn lt-btn--primary" id="plain-apply">Apply to ' + esc(agent.name) + "</button>" : "") + "</div>";
    }).catch(function (e) { $("plain-out").innerHTML = "<p>" + esc(e.status === 404 ? "Plain-English rules aren’t available on this server." : errText(e)) + "</p>"; })
      .then(function () { $("plain-go").disabled = false; });
  }
  function plainApply() {
    if (!compiled) return;
    LT.api("GET", "/api/agents/" + agent.id + "/rules").then(function (cur) {
      var merged = Object.assign({}, cur.rules || {}, compiled);   // only the rules you mentioned change
      return LT.api("POST", "/api/agents/" + agent.id + "/rules", { rules: merged });
    }).then(function () { return LT.api("POST", "/api/agents/" + agent.id + "/pass", {}); })
      .then(function () {
        $("plain-out").innerHTML = '<p class="cc-status is-ok">Applied to ' + esc(agent.name) + ". Its Pass was re-issued with the new rules.</p>";
        $("plain").value = "";
        compiled = null;
        return refreshAll();
      }).catch(function (e) { $("plain-out").insertAdjacentHTML("beforeend", "<p>" + esc("Not applied: " + errText(e)) + "</p>"); });
  }

  // ---------- where your money went ----------
  function bars(title, map) {
    var keys = Object.keys(map).sort(function (a, b) { return map[b] - map[a]; });
    var max = keys.length ? map[keys[0]] : 1;
    return '<div><h3 class="cc-h3">' + esc(title) + '</h3><ul class="cc-bars">' + keys.map(function (k) {
      var w = Math.max(2, Math.round(map[k] / max * 100));
      return "<li><span>" + esc(k) + '</span><svg viewBox="0 0 100 14" preserveAspectRatio="none" aria-hidden="true"><rect width="100" height="14" rx="7" fill="#D9E6E3"/><rect width="' + w + '" height="14" rx="7" fill="#1FB89A"/></svg><b>' + usd(map[k]) + "</b></li>";
    }).join("") + "</ul></div>";
  }
  function moneyWent() {
    return LT.api("GET", "/api/handshakes?limit=200&agent_id=" + agent.id).then(function (r) {
      var byStore = {}, byCat = {}, total = 0, killed = 0, nKilled = 0;
      (r.handshakes || []).forEach(function (h) {
        if (h.order && h.executed && (h.order.status === "cancelled" || /RETURNED/.test(String(h.order.status)))) { killed += Number(h.order.amount || (h.item || {}).amount || 0); nKilled++; return; }
        if (!h.order || !h.executed) return;
        var amt = Number(h.order.amount || (h.item || {}).amount || 0);
        var s = (h.store || {}).name || "store", c = (h.item || {}).category || "other";
        byStore[s] = (byStore[s] || 0) + amt;
        byCat[c] = (byCat[c] || 0) + amt;
        total += amt;
      });
      $("money").innerHTML = total ? bars("By store", byStore) + bars("By category", byCat) +
        '<p class="cc-fine" style="grid-column:1/-1;margin:0">' + usd(total) + " in kept purchases." + (nKilled ? " Canceled by Stop all: " + usd(killed) + " (" + nKilled + " payout" + (nKilled === 1 ? "" : "s") + "), left out of the totals." : "") + "</p>"
        : '<p class="cc-empty" style="grid-column:1/-1">No paid purchases for ' + esc(agent.name) + " yet." + (nKilled ? " Canceled by Stop all: " + usd(killed) + "." : "") + "</p>";
    }).catch(function () {});
  }

  // ---------- start ----------
  function loadAgent() {
    return Promise.all([
      LT.api("GET", "/api/shopper/prefs?agent_id=" + agent.id),
      stores.length ? Promise.resolve({ stores: stores }) : LT.api("GET", "/api/stores")
    ]).then(function (r) {
      prefs = r[0];
      stores = r[1].stores || [];
      fillForm();
      refreshSide();
      return Promise.all([refreshPass(), runSearch(true)]);
    });
  }
  function load(force) {
    return LT.scout(force).then(function (a) {
      agent = { id: a.id, name: a.name };
      return loadAgents();
    }).then(function () {
      var want = remembered();
      if (want && agents.some(function (x) { return x.id === want; })) {
        agent = { id: want, name: agents.filter(function (x) { return x.id === want; })[0].name };
        renderAgents();
        setNames();
      }
      return loadAgent();
    });
  }

  function init() {
    bindForm();
    bindPassDesign();
    bindSearch();
    bindFilters();
    bindReceipts();
    $("auto-go").addEventListener("click", autoGo);
    $("mode-q").addEventListener("change", function (e) {
      var m = document.querySelector('input[name="mode"][value="' + e.target.value + '"]');
      if (m) { m.checked = true; saveMode(); }
    });
    $("kill").addEventListener("click", function () { kill(this); });
    $("kill-dock").addEventListener("click", function () { kill(this); });
    $("resume").addEventListener("click", function () { if (agent) resumeAgent(agent, "kill-status"); });
    $("reset").addEventListener("click", reset);
    $("agents").addEventListener("click", function (e) { var b = e.target.closest("button[data-a]"); if (b) agentAction(b); });
    Array.prototype.forEach.call(document.querySelectorAll("[data-preset]"), function (b) { b.addEventListener("click", function () { addAgent(b); }); });
    $("plain-go").addEventListener("click", plainGo);
    $("plain-out").addEventListener("click", function (e) { if (e.target.id === "plain-apply") plainApply(); });
    SLIDERS.forEach(function (k) { LT.syncSlider($(k)); });
    LT.idleHandshake($("hs-panel"), "Pick a match above and press Check both sides or Buy. The result shows up here.");
    load().catch(function (e) {
      $("pass").innerHTML = "<p>" + esc(errText(e)) + "</p>";
      status("find-status", errText(e), "err");
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
