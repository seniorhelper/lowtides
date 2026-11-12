/* /merchants/ — the store control center. Uses /cc.js (window.LT) and AG Grid community (loaded on this page only). */
(function () {
  "use strict";
  var LT = window.LT, $ = LT.$, esc = LT.esc, usd = LT.usd;
  var stores = [], cur = null, draft = null, scope = "all", catalog = [], grids = {};

  function errText(e) { return (e && e.message) || "Something went wrong. Please try again."; }
  function status(id, text, kind) { var el = $(id); el.textContent = text || ""; el.className = "cc-status" + (kind ? " is-" + kind : ""); }
  function signed() { return stores.filter(function (s) { return s.policy_signed; }); }
  function store(id) { return stores.filter(function (s) { return s.id === id; })[0]; }

  // ---------- store picker ----------
  function renderPicker() {
    $("stores").innerHTML = signed().map(function (s) {
      var on = cur && s.id === cur.id;
      return '<button type="button" class="cc-store" role="radio" aria-checked="' + on + '" tabindex="' + (on ? 0 : -1) + '" data-id="' + esc(s.id) + '"><b>' + esc(s.name) + "</b><small>" + LT.wbr(s.domain) + " · demo store</small>" + LT.gpBadge(s.guardpooch && s.guardpooch.verdict) + "</button>";
    }).join("");
  }
  function pick(id, focus) {
    cur = store(id);
    if (!cur) return;
    draft = JSON.parse(JSON.stringify(cur.policy));
    renderPicker();
    if (focus) { var b = document.querySelector('.cc-store[data-id="' + id + '"]'); if (b) b.focus(); }
    fillPolicy();
    renderPub();
    fillTestItems();
    $("test-out").innerHTML = "";
    status("pol-status", "");
    status("smode-status", "");
    status("opp-status", "");
    loadNumbers();
  }

  // ---------- policy editor ----------
  function allCats() {
    var set = {};
    signed().forEach(function (s) { (s.policy.categories || []).forEach(function (c) { set[c] = 1; }); });
    return Object.keys(set).sort();
  }
  function fillMode() {
    var r = document.querySelector('input[name="smode"][value="' + (draft.mode || "auto") + '"]');
    if (r) r.checked = true;
    Array.prototype.forEach.call(document.querySelectorAll(".smode-name"), function (el) { el.textContent = cur.name; });
  }
  var SMODE_SAID = {
    auto: "Full Autopilot (auto-accept) is on: AI orders inside your rules go through. Signed and published.",
    review: "AI Assist (review every order) is on: each AI order now waits for a person. Signed and published.",
    closed: "Off (closed to AI shoppers): every AI order is refused with a plain reason. Signed and published."
  };
  function saveMode() {
    var v = (document.querySelector('input[name="smode"]:checked') || {}).value;
    status("smode-status", "Signing…");
    LT.api("PUT", "/api/stores/" + encodeURIComponent(cur.id) + "/policy", { mode: v }).then(function (s) {
      stores = stores.map(function (x) { return x.id === s.id ? s : x; });
      cur = s;
      draft.mode = s.policy.mode;
      renderPub(true);
      status("smode-status", SMODE_SAID[s.policy.mode] || "Saved.", s.policy.mode === "closed" ? "err" : "ok");
      loadOpps();
    }).catch(function (e) { status("smode-status", "Not saved: " + errText(e), "err"); fillMode(); });
  }
  function fillPolicy() {
    fillMode();
    $("max_order_no_human").value = draft.max_order_no_human;
    $("min_trust").value = draft.min_trust;
    LT.syncSlider($("max_order_no_human"));
    LT.syncSlider($("min_trust"));
    $("require_evidence").checked = !!draft.require_evidence;
    $("cats").innerHTML = allCats().map(function (c) {
      return '<button type="button" class="cc-chip" data-cat="' + esc(c) + '" aria-pressed="' + ((draft.categories || []).indexOf(c) >= 0) + '">' + esc(c) + "</button>";
    }).join("");
    renderBlocked();
  }
  function renderBlocked() {
    var b = draft.blocked_agents || [];
    $("blocked").innerHTML = b.length ? b.map(function (n) {
      return '<button type="button" class="cc-chip cc-chip--no" data-unblock="' + esc(n) + '" aria-label="Unblock ' + esc(n) + '">' + esc(n) + "</button>";
    }).join("") : '<span class="cc-fine">Nobody blocked.</span>';
  }
  function dirty() { status("pol-status", "Unsaved changes. Press Sign and publish."); }
  function bindPolicy() {
    ["max_order_no_human", "min_trust"].forEach(function (k) {
      $(k).addEventListener("input", function () { LT.syncSlider(this); draft[k] = Number(this.value); dirty(); });
    });
    $("require_evidence").addEventListener("change", function () { draft.require_evidence = this.checked; dirty(); });
    $("cats").addEventListener("click", function (e) {
      var b = e.target.closest("[data-cat]"); if (!b) return;
      var c = b.getAttribute("data-cat"), l = (draft.categories || []).slice(), i = l.indexOf(c);
      if (i >= 0) l.splice(i, 1); else l.push(c);
      draft.categories = l;
      b.setAttribute("aria-pressed", String(i < 0));
      dirty();
    });
    $("blocked").addEventListener("click", function (e) {
      var b = e.target.closest("[data-unblock]"); if (!b) return;
      draft.blocked_agents = (draft.blocked_agents || []).filter(function (x) { return x !== b.getAttribute("data-unblock"); });
      renderBlocked(); dirty(); $("block-in").focus();
    });
    function add() {
      var v = $("block-in").value.trim().toLowerCase(); if (!v) return;
      if ((draft.blocked_agents || []).indexOf(v) < 0) draft.blocked_agents = (draft.blocked_agents || []).concat([v]);
      $("block-in").value = ""; renderBlocked(); dirty();
    }
    $("block-add").addEventListener("click", add);
    $("block-in").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); add(); } });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="smode"]'), function (r) { r.addEventListener("change", saveMode); });
    $("pol").addEventListener("submit", function (e) {
      e.preventDefault();
      if (!cur) return;
      $("publish").disabled = true;
      status("pol-status", "Signing…");
      LT.api("PUT", "/api/stores/" + encodeURIComponent(cur.id) + "/policy", {
        max_order_no_human: Number(draft.max_order_no_human), min_trust: Number(draft.min_trust), categories: draft.categories || [],
        require_evidence: !!draft.require_evidence, blocked_agents: draft.blocked_agents || []
      }).then(function (s) {
        stores = stores.map(function (x) { return x.id === s.id ? s : x; });
        cur = s;
        draft = JSON.parse(JSON.stringify(s.policy));
        fillPolicy();
        renderPub(true);
        status("pol-status", "Signed and published. AI shoppers now get these rules.", "ok");
        loadOpps();
      }).catch(function (e) { status("pol-status", "Not published: " + errText(e), "err"); })
        .then(function () { $("publish").disabled = false; });
    });
    $("stores").addEventListener("click", function (e) { var b = e.target.closest(".cc-store"); if (b) pick(b.getAttribute("data-id")); });
    $("stores").addEventListener("keydown", function (e) {
      var d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!d || !cur) return;
      e.preventDefault();
      var ids = signed().map(function (s) { return s.id; });
      pick(ids[(ids.indexOf(cur.id) + d + ids.length) % ids.length], true);
    });
  }
  function renderPub(fresh) {
    var p = cur.policy, gp = cur.guardpooch || {};
    $("pub").innerHTML =
      '<p style="margin:0 0 10px">' + LT.sigPill(cur.policy_signed ? "match" : "no_match", cur.policy_signed ? "Signed and published" : "Not signed") + "</p>" +
      "<p>Signed with Ed25519, key <span class=\"cc-mono\">" + esc(p.kid || "") + "</span>, updated " + esc(new Date(p.updated_at).toLocaleString()) + (fresh ? " (just now)" : "") + ".</p>" +
      '<p class="cc-policy-link"><a href="' + esc(cur.policy_url) + '" target="_blank" rel="noopener">Public policy file</a><span class="cc-mono cc-fine">' + esc(cur.policy_url) + "</span></p>" +
      "<p>Mode: <span class=\"cc-mode-tag cc-mode-tag--" + esc(p.mode || "auto") + "\">" + esc(LT.MODE_WORD[p.mode || "auto"]) + "</span></p>" +
      "<p style=\"margin:0\">GuardPooch says about your store: " + LT.gpBadge(gp.verdict) + " " + esc((gp.reasons || []).join(" ")) + "</p>";
  }

  // ---------- test with Scout ----------
  function fillTestItems() {
    $("test-item").innerHTML = catalog.filter(function (it) { return it.store_id === cur.id; }).sort(function (a, b) { return a.price - b.price; }).map(function (it) {
      return '<option value="' + esc(it.id) + '">' + esc(it.title) + " · " + usd(it.price) + " · " + esc(it.category) + "</option>";
    }).join("");
  }
  function test() {
    var item = $("test-item").value;
    if (!item) return;
    $("test-out").innerHTML = "<p>Checking both sides…</p>";
    var a;
    LT.scout().then(function (ag) {
      a = ag;
      return LT.api("POST", "/api/handshake", { agent_id: a.id, store_id: cur.id, item_id: item, execute: false });
    }).then(function (h) {
      var notes = [];
      h.store_checks.concat(h.shopper_checks).forEach(function (c) { if (c.level !== "ok") notes.push((h.store_checks.indexOf(c) >= 0 ? "Your rules: " : "Scout’s rules: ") + c.detail); });
      var min = cur.policy.min_trust;
      $("test-out").innerHTML = LT.sigPill(h.signal) + '<p class="cc-tb-line" style="margin-top:10px !important">' + LT.shopperBadge(a.id, (h.agent || {}).name || a.name || "Scout", (h.trust || {}).score, min, cur.name) + "</p>" + '<ul style="margin:10px 0">' + (notes.length ? notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") : "<li>Every check passed on both sides.</li>") + "</ul>" +
        '<p class="cc-fine" style="margin:0">A real check, no money moved. <a href="/handshake/">See both sides in full</a> · <a href="/api/handshakes/' + encodeURIComponent(h.id) + '/proof" download="lowtides-proof-' + esc(h.id) + '.json">Proof pack</a></p>';
      $("test-out").setAttribute("tabindex", "-1");
      $("test-out").focus({ preventScroll: true });          // keyboard and screen-reader users land on the result
      loadNumbers();
    }).catch(function (e) { $("test-out").innerHTML = "<p>" + esc(errText(e)) + "</p>"; });
  }

  // ---------- opportunities ----------
  function loadOpps() {
    if (!cur) return;
    return LT.api("GET", "/api/merchant/opportunities?store_id=" + encodeURIComponent(cur.id)).then(function (o) {
      var f = o.findings || [];
      $("opp-list").innerHTML = f.length ? f.map(function (x, i) {
        return '<li class="cc-opp cc-opp--' + esc(x.kind) + '"><div class="cc-opp-top"><span class="cc-mode-tag ' + (x.kind === "protect" ? "cc-mode-tag--review" : "") + '">' +
          (x.kind === "protect" ? "Protect" : "Grow") + "</span>" + (x.impact_usd != null ? '<span class="cc-opp-usd">' + usd(x.impact_usd) + "</span>" : "") + "</div>" +
          "<h3>" + esc(x.title) + "</h3><p>" + esc(x.detail) + "</p>" +
          (x.action ? '<button type="button" class="lt-btn lt-btn--primary" data-opp="' + i + '">' + esc(x.action.label) + "</button>" : "") + "</li>";
      }).join("") : '<li class="cc-empty">' + esc(o.note || "Nothing to suggest yet.") + "</li>";
      $("opp-list")._findings = f;
    }).catch(function (e) { $("opp-list").innerHTML = '<li class="cc-empty">' + esc(errText(e)) + "</li>"; });
  }
  function applyOpp(btn) {
    var f = ($("opp-list")._findings || [])[Number(btn.getAttribute("data-opp"))];
    if (!f || !f.action) return;
    btn.disabled = true;
    status("opp-status", "Signing…");
    LT.api("PUT", "/api/stores/" + encodeURIComponent(cur.id) + "/policy", f.action.policy_patch).then(function (s) {
      stores = stores.map(function (x) { return x.id === s.id ? s : x; });
      cur = s;
      draft = JSON.parse(JSON.stringify(s.policy));
      fillPolicy();
      renderPub(true);
      status("opp-status", "Applied: " + f.title + ". Your Store Policy was re-signed and published.", "ok");
      fillMode();
      loadOpps();
    }).catch(function (e) { btn.disabled = false; status("opp-status", "Not applied: " + errText(e), "err"); });
  }

  // ---------- KPIs ----------
  function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
  function rate(v, have) { return have ? LT.pct(v) : "—"; }
  function renderKpis(k) {
    var have = (k.handshakes || 0) > 0, ord = (k.orders || 0) > 0;
    $("kpi-store").textContent = scope === "all" ? "all demo stores" : cur.name;
    var tiles = [
      ["Orders", String(k.orders || 0), "placed by AI shoppers", ""],
      ["Captured", usd(k.captured_usd), "PayPal sandbox", ""],
      ["Approval rate", rate(k.approval_rate, have), "both said yes", "good"],
      ["Held for a person", rate(k.held_rate, have), "needed a yes", ""],
      ["Refused", rate(k.refused_rate, have), "no money moved", ""],
      ["Refund rate", rate(k.refund_rate, ord), "of paid orders", k.refund_rate > 0 ? "bad" : ""],
      ["Average order", ord ? usd(k.avg_order) : "—", "", ""],
      ["Proof coverage", rate(k.proof_coverage, ord), "orders with a proof pack", "good"],
      ["Revoked Pass blocked", String(k.revoked_blocked || 0), "after Stop all", ""],
      ["Scam store blocked", String(k.scam_blocked || 0), "by GuardPooch", ""],
      ["Returning AI shoppers", String(k.returning_shoppers || 0), (k.shoppers_with_orders ? "of " + k.shoppers_with_orders + " with orders: 2+ kept orders" : "shoppers with 2+ kept orders"), ""],
      ["Kept-order rate", rate(k.kept_order_rate, ord), "kept, not refunded (a rough satisfaction proxy)", "good"],
      ["Shopper satisfaction", k.ratings ? LT.pct(k.satisfaction_rate) : "\u2014", k.ratings ? k.rated_up + " of " + k.ratings + " ratings were thumbs up" : "no ratings yet", "good"],
      ["Net captured", usd(k.net_captured_usd), k.refunded_usd ? usd(k.refunded_usd) + " refunded" : "after refunds", ""],
      ["Bad orders stopped", String(k.bad_orders_stopped || 0), k.bad_orders_stopped ? usd(k.bad_orders_stopped_usd) + " never charged" : "refused before any money moved", ""]
    ];
    $("kpis").innerHTML = tiles.map(function (t) {
      return '<div class="cc-kpi' + (t[3] && have ? " cc-kpi--" + t[3] : "") + '"><dt>' + esc(t[0]) + "</dt><dd>" + esc(t[1]) + (t[2] ? "<small>" + esc(t[2]) + "</small>" : "") + "</dd></div>";
    }).join("");
    $("kpi-empty").textContent = have ? "" : "No AI orders yet — press “Test it with Scout” or open the shopper page, and the numbers fill in. (Look-only checks never count.)";
    $("kpi-actions").hidden = have;
    var n = [];
    if (!have) n.push("Nothing to worry about yet: no AI orders.");
    else {
      n.push(plural(k.handshakes, "AI shopper visit") + ", " + plural(k.orders || 0, "order") + ", " + usd(k.captured_usd) + " captured in PayPal sandbox test money" + (k.refund_rate > 0 ? " (some paid orders were later canceled or refunded, for example by a Stop all)" : "") + ".");
      if (k.by_signal && k.by_signal.needs_yes) n.push(plural(k.by_signal.needs_yes, "order") + " waited for a person because they broke a soft limit, so you never took money a person didn’t okay.");
      if (k.by_signal && k.by_signal.no_match) n.push(plural(k.by_signal.no_match, "visit") + " refused before any money moved" + (k.revoked_blocked ? ", " + k.revoked_blocked + " of them from a revoked Pass" : "") + (k.scam_blocked ? ", " + k.scam_blocked + " at a lookalike scam store" : "") + ".");
      if (ord) n.push(LT.pct(k.proof_coverage) + " of paid orders come with a proof pack you can show in a dispute.");
    }
    $("kpi-note").innerHTML = "<b>What this means for you:</b> " + esc(n.join(" "));
  }

  // ---------- chart (dependency-free SVG, stacked by outcome) ----------
  function renderChart(days) {
    var el = $("chart");
    if (!days || !days.length) { el.innerHTML = '<div class="cc-empty" style="height:220px;display:grid;place-items:center">No outcomes yet.</div>'; return; }
    days = days.slice(-14);
    var W = 640, H = 220, padL = 32, padB = 28, padT = 10;
    var max = Math.max.apply(null, days.map(function (d) { return d.match + d.needs_yes + d.no_match; })) || 1;
    var bw = Math.min(56, (W - padL - 10) / days.length * 0.6), step = (W - padL - 10) / days.length;
    var y = function (v) { return (H - padB) - v / max * (H - padB - padT); };
    var cols = { match: "#1FB89A", needs_yes: "#E39A2D", no_match: "#D9483B" };
    var g = '<line x1="' + padL + '" y1="' + (H - padB) + '" x2="' + W + '" y2="' + (H - padB) + '" stroke="#D9E6E3"/>' +
      '<text x="' + (padL - 6) + '" y="' + (y(max) + 4) + '" text-anchor="end" font-size="11" fill="#4E6468">' + max + "</text>" +
      '<text x="' + (padL - 6) + '" y="' + (H - padB + 4) + '" text-anchor="end" font-size="11" fill="#4E6468">0</text>';
    var summary = [];
    days.forEach(function (d, i) {
      var x = padL + i * step + (step - bw) / 2, base = 0;
      ["match", "needs_yes", "no_match"].forEach(function (k) {
        var v = d[k] || 0; if (!v) return;
        g += '<rect x="' + x.toFixed(1) + '" y="' + y(base + v).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + (y(base) - y(base + v)).toFixed(1) + '" fill="' + cols[k] + '" rx="3"><title>' + esc(d.day) + ": " + v + " " + LT.SIG[k].short + "</title></rect>";
        base += v;
      });
      g += '<text x="' + (x + bw / 2).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle" font-size="11" fill="#4E6468">' + esc(d.day.slice(5)) + "</text>";
      summary.push(d.day + ": " + d.match + " yes, " + d.needs_yes + " needed a yes, " + d.no_match + " refused");
    });
    el.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="' + esc("Outcomes by day. " + summary.join(". ")) + '">' + g + "</svg>";
  }

  // ---------- grids ----------
  // The number, a bar, and the same three-level badge the shopper page uses, measured against THIS store's minimum.
  function trustCell(p) {
    var v = Number(p.value || 0), min = cur && cur.policy ? cur.policy.min_trust : 0, lv = LT.shopperLevel(v, min);
    var c = lv.k === "trusted" ? "#1FB89A" : lv.k === "careful" ? "#E39A2D" : "#D9483B";
    var d = document.createElement("span");
    d.className = "cc-bar cc-cell-tb";
    d.innerHTML = '<b>' + v + '</b><i style="--w:' + v + "%;--bar-c:" + c + '"></i>' + LT.shopperBadge(p.data.agent_id, p.data.name, v, min, cur ? cur.name : "your store", true);
    return d;
  }
  function trustText(p) { var min = cur && cur.policy ? cur.policy.min_trust : 0; return Number(p.value || 0) + " \u00b7 " + LT.shopperLevel(p.value, min).w; }
  function sigCell(p) {
    var s = document.createElement("span");
    s.className = "cc-cellsig sig--" + p.value;
    s.textContent = (LT.SIG[p.value] || {}).short || p.value;
    return s;
  }
  function proofCell(p) {
    var a = document.createElement("a");
    a.href = "/api/handshakes/" + encodeURIComponent(p.value) + "/proof";
    a.setAttribute("download", "lowtides-proof-" + p.value + ".json");
    a.textContent = "Proof";
    a.setAttribute("aria-label", "Proof pack for " + (p.data.item || "order"));
    return a;
  }
  var moneyFmt = function (p) { return usd(p.value); };
  // AG Grid is a big download, so it is fetched only when a table is close to the screen. Until then (and if
  // the download fails) the tables are plain, readable HTML tables with the same data.
  var gridState = "idle", lastGrid = null;
  function loadGrid() {
    if (gridState !== "idle") return;
    gridState = "loading";
    var sc = document.createElement("script");
    sc.src = "https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/dist/ag-grid-community.min.js";
    sc.onload = function () {
      gridState = "ready";
      if (lastGrid && window.agGrid && agGrid.createGrid) {
        ["grid-agents", "grid-hs"].forEach(function (id) { var el = $(id); el.innerHTML = ""; el.style.height = el.getAttribute("data-h") || ""; });
        renderGrids(lastGrid[0], lastGrid[1]);
      }
    };
    sc.onerror = function () { gridState = "failed"; };
    document.head.appendChild(sc);
  }
  function watchGrids() {
    var first = $("grid-agents");
    if (!first) return;
    if (!("IntersectionObserver" in window)) { loadGrid(); return; }
    var io = new IntersectionObserver(function (entries) {
      if (entries.some(function (e) { return e.isIntersecting; })) { io.disconnect(); loadGrid(); }
    }, { rootMargin: "400px" });
    io.observe(first);
    io.observe($("grid-hs"));
  }
  // plain-HTML fallback: same cells as the AG Grid ones (pill wording, trust badge text, working Proof link)
  var fallback = {};
  function plainCell(c, r) {
    var v = r[c.field];
    if (c.field === "signal") return '<span class="cc-cellsig sig--' + esc(v) + '">' + esc((LT.SIG[v] || {}).short || v) + "</span>";
    if (c.field === "id") return '<a href="/api/handshakes/' + encodeURIComponent(v) + '/proof" download="lowtides-proof-' + esc(v) + '.json" aria-label="Proof pack for ' + esc(r.item || "order") + '">Proof</a>';
    if (c.field === "trust") return esc(trustText({ value: v }));
    return esc(c.valueFormatter ? c.valueFormatter({ value: v }) : v);
  }
  function plainText(c, r) {
    var v = r[c.field];
    if (c.field === "signal") return (LT.SIG[v] || {}).short || v;
    if (c.field === "id") return location.origin + "/api/handshakes/" + encodeURIComponent(v) + "/proof";
    if (c.field === "trust") return trustText({ value: v });
    return c.valueFormatter ? c.valueFormatter({ value: v }) : v;
  }
  function drawPlain(id) {
    var f = fallback[id], el = $(id), q = (f.q || "").toLowerCase();
    var shown = f.rows.filter(function (r) { return !q || f.cols.some(function (c) { return String(plainText(c, r)).toLowerCase().indexOf(q) >= 0; }); });
    f.shown = shown;
    el.innerHTML = shown.length ? '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse"><thead><tr>' + f.cols.map(function (c) { return '<th scope="col" style="text-align:left;padding:6px">' + esc(c.headerName || c.field) + "</th>"; }).join("") + "</tr></thead><tbody>" +
      shown.map(function (r) { return "<tr>" + f.cols.map(function (c) { return '<td style="padding:6px;border-top:1px solid #D9E6E3">' + plainCell(c, r) + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table></div>"
      : '<div class="cc-empty">' + esc(q ? "No rows match your filter." : f.empty) + "</div>";
  }
  function plainCsv(id, name) {
    var f = fallback[id]; if (!f) return;
    var q = function (v) { v = String(v == null ? "" : v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    var lines = [f.cols.map(function (c) { return q(c.headerName || c.field); }).join(",")].concat((f.shown || f.rows).map(function (r) { return f.cols.map(function (c) { return q(plainText(c, r)); }).join(","); }));
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv" }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
  }
  function grid(id, cols, rows, emptyText) {
    var el = $(id);
    if (!(window.agGrid && agGrid.createGrid)) {   // not loaded yet, or the CDN is unreachable
      if (!el.hasAttribute("data-h")) el.setAttribute("data-h", el.style.height || "");
      el.style.height = "auto";
      var old = fallback[id];
      fallback[id] = { cols: cols, rows: rows, empty: emptyText, q: old ? old.q : $(id === "grid-hs" ? "hs-q" : "ag-q").value };
      drawPlain(id);
      return;
    }
    delete fallback[id];
    if (grids[id]) { grids[id].setGridOption("rowData", rows); return; }
    grids[id] = agGrid.createGrid(el, {
      columnDefs: cols, rowData: rows, defaultColDef: { sortable: true, filter: true, resizable: true, minWidth: 90 },
      overlayNoRowsTemplate: '<span style="padding:12px">' + esc(emptyText) + "</span>", pagination: rows.length > 50, paginationPageSize: 50,
      autoSizeStrategy: { type: "fitGridWidth" }
    });
  }
  function renderGrids(k, hss) {
    lastGrid = [k, hss];
    grid("grid-agents", [
      { field: "name", headerName: "AI shopper", minWidth: 150 },
      { field: "trust", headerName: "Trust (heuristic)", cellRenderer: trustCell, valueFormatter: trustText, minWidth: 300, filter: "agNumberColumnFilter" },
      { field: "orders", headerName: "Orders", type: "numericColumn" },
      { field: "captured_usd", headerName: "Captured", type: "numericColumn", valueFormatter: moneyFmt },
      { field: "refunds", headerName: "Refunds", type: "numericColumn" },
      { field: "refused", headerName: "Refused", type: "numericColumn" }
    ], k.by_agent || [], "No AI shoppers yet.");
    grid("grid-hs", [
      { field: "when", headerName: "When", minWidth: 150 },
      { field: "agent", headerName: "AI shopper", minWidth: 110 },
      { field: "store", headerName: "Store", minWidth: 140 },
      { field: "item", headerName: "Item", minWidth: 170 },
      { field: "amount", headerName: "Amount", type: "numericColumn", valueFormatter: moneyFmt },
      { field: "signal", headerName: "Outcome", cellRenderer: sigCell, valueFormatter: function (p) { return (LT.SIG[p.value] || {}).short || p.value; }, getQuickFilterText: function (p) { return (LT.SIG[p.value] || {}).short || p.value; }, minWidth: 150 },
      { field: "paypal", headerName: "PayPal sandbox id", minWidth: 170 },
      { field: "id", headerName: "Proof", cellRenderer: proofCell, valueFormatter: function (p) { return location.origin + "/api/handshakes/" + encodeURIComponent(p.value) + "/proof"; }, sortable: false, filter: false, minWidth: 90 }
    ], (hss || []).filter(function (h) { return h.executed !== false; }).map(function (h) {
      return { id: h.id, when: new Date(h.at).toLocaleString(), agent: (h.agent || {}).name, store: (h.store || {}).name, item: (h.item || {}).title,
        amount: (h.item || {}).amount, signal: h.signal, paypal: h.order ? h.order.paypal_order_id + (h.order.simulated ? " (simulated)" : "") : (h.approval_id ? "held: " + h.approval_id : "") };
    }), "No AI orders yet.");
  }
  function loadNumbers() {
    if (!cur) return;
    loadOpps();
    var q = scope === "all" ? "" : "store_id=" + encodeURIComponent(cur.id);
    return Promise.all([LT.api("GET", "/api/merchant/kpis?" + q), LT.api("GET", "/api/handshakes?limit=100&" + q)]).then(function (r) {
      renderKpis(r[0]);
      renderChart(r[0].by_day);
      renderGrids(r[0], r[1].handshakes);
    }).catch(function (e) { $("kpi-empty").textContent = errText(e); });
  }

  function init() {
    watchGrids();
    bindPolicy();
    $("test-go").addEventListener("click", test);
    $("opp-list").addEventListener("click", function (e) { var b = e.target.closest("button[data-opp]"); if (b) applyOpp(b); });
    $("scope-store").addEventListener("click", function () { scope = "store"; this.setAttribute("aria-pressed", "true"); $("scope-all").setAttribute("aria-pressed", "false"); loadNumbers(); });
    $("scope-all").addEventListener("click", function () { scope = "all"; this.setAttribute("aria-pressed", "true"); $("scope-store").setAttribute("aria-pressed", "false"); loadNumbers(); });
    $("ag-q").addEventListener("input", function () { if (grids["grid-agents"]) grids["grid-agents"].setGridOption("quickFilterText", this.value); else if (fallback["grid-agents"]) { fallback["grid-agents"].q = this.value; drawPlain("grid-agents"); } });
    $("hs-q").addEventListener("input", function () { if (grids["grid-hs"]) grids["grid-hs"].setGridOption("quickFilterText", this.value); else if (fallback["grid-hs"]) { fallback["grid-hs"].q = this.value; drawPlain("grid-hs"); } });
    $("ag-csv").addEventListener("click", function () { if (grids["grid-agents"]) grids["grid-agents"].exportDataAsCsv({ fileName: "lowtides-ai-shoppers.csv" }); else plainCsv("grid-agents", "lowtides-ai-shoppers.csv"); });
    $("hs-csv").addEventListener("click", function () { if (grids["grid-hs"]) grids["grid-hs"].exportDataAsCsv({ fileName: "lowtides-ai-orders.csv", columnKeys: ["when", "agent", "store", "item", "amount", "signal", "paypal", "id"] }); else plainCsv("grid-hs", "lowtides-ai-orders.csv"); });
    Promise.all([LT.api("GET", "/api/stores"), LT.api("GET", "/api/catalog")]).then(function (r) {
      stores = r[0].stores || [];
      catalog = r[1].items || [];
      var first = signed()[0];
      if (first) pick(first.id);
    }).catch(function (e) { status("pol-status", errText(e), "err"); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
