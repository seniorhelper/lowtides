/* Lowtides Action Muzzle (/shop/#muzzle). Vanilla JS, no dependencies.
   Talks to /api/muzzle. All server text goes in with textContent, never innerHTML. */
(function () {
  "use strict";

  var doc = document;
  var API = "/api/muzzle";
  var UNAVAILABLE = "Action Muzzle isn’t available on this server right now.";
  var RULE_HELP = "Pick whether your AI may do this by itself, must ask you first, or may never do it.";
  var LEVELS = [["allow", "Allowed"], ["ask", "Ask me first"], ["never", "Never"]];
  var SIGNALS = { match: "Both said yes", needs_yes: "Needs your yes", no_match: "No. Nothing happened." };
  var SIDES = [["you", "Your side"], ["them", "Their side"], ["guard", "Watchdog"]];
  var MARKS = { pass: "ok", ask: "ask", fail: "no" };
  var STATUS_WORDS = {
    done: "Done.", waiting: "Waiting for your yes.", blocked: "Blocked. Nothing happened.",
    denied: "You said no. Nothing happened.", frozen: "Frozen. Held until you release it."
  };
  var RECENT_MAX = 10;

  var state = null;       // last full state from the server
  var shown = [];         // actions tried in this visit, newest first (card 2)
  var ruleRows = {};      // type -> { fieldset, radios: {level: input}, note }
  var ruleKey = "";
  var sampleKey = null;
  var loading = null;
  var $ = {};

  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function clear(n) { while (n && n.firstChild) n.removeChild(n.firstChild); }
  function str(v) { return v == null ? "" : String(v); }
  function slug(v) { return str(v).replace(/[^A-Za-z0-9_-]/g, "_"); }

  function api(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(API + path, opts).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = null; }
        if (!res.ok) {
          throw new Error(data && typeof data.error === "string" && data.error ? data.error : "That did not work. Please try again.");
        }
        if (!data || typeof data !== "object") throw new Error("The server sent an answer this page could not read.");
        return data;
      });
    }, function () {
      throw new Error("Could not reach the server. Please try again.");
    });
  }

  function say(node, text, kind) {
    if (!node) return;
    node.textContent = text || "";
    node.classList.remove("is-ok", "is-err");
    if (text && kind) node.classList.add(kind === "err" ? "is-err" : "is-ok");
  }

  // Disable a button while its request runs.
  function busy(btn, run) {
    if (btn.disabled) return Promise.resolve();
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    var done = function () { btn.disabled = false; btn.removeAttribute("aria-busy"); };
    var p;
    try { p = Promise.resolve(run()); } catch (e) { p = Promise.reject(e); }
    return p.then(done, done);
  }

  /* ---------- one action card (used for results and for recent actions) ---------- */
  function actionCard(a, compact) {
    a = a || {};
    var li = el("li", "mz-act" + (compact ? " mz-act--compact" : ""));
    if (a.id != null) li.setAttribute("data-id", str(a.id));

    var top = el("div", "mz-act-top");
    var sigClass = Object.prototype.hasOwnProperty.call(SIGNALS, a.signal) ? " sig--" + a.signal : "";
    top.appendChild(el("span", "sig" + sigClass, SIGNALS[a.signal] || STATUS_WORDS[a.status] || "Checked"));
    if (compact && typeof a.ts === "number" && isFinite(a.ts)) {
      var when = new Date(a.ts * 1000);
      if (!isNaN(when.getTime())) {
        top.appendChild(el("span", "mz-act-when", when.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })));
      }
    }
    li.appendChild(top);

    li.appendChild(el("p", "mz-act-head", a.headline || STATUS_WORDS[a.status] || ""));

    var cp = a.counterparty && typeof a.counterparty === "object" ? a.counterparty.name || a.counterparty.id : a.counterparty;
    var what = el("p", "mz-act-what");
    if (cp) what.appendChild(el("b", "", cp));
    if (cp && a.summary) what.appendChild(doc.createTextNode(": "));
    if (a.summary) what.appendChild(doc.createTextNode(str(a.summary)));
    if (!what.firstChild && a.label) what.textContent = str(a.label);
    if (what.firstChild) li.appendChild(what);

    var checks = Array.isArray(a.checks) ? a.checks : [];
    if (checks.length) {
      var groups = el("div", "mz-checks");
      SIDES.forEach(function (side, i) {
        var mine = checks.filter(function (c) {
          var s = c && c.side;
          if (s === side[0]) return true;
          return i === SIDES.length - 1 && s !== "you" && s !== "them";   // unknown side: show under Watchdog
        });
        if (!mine.length) return;
        var g = el("div", "mz-check-group");
        g.appendChild(el("p", "mz-check-title", side[1]));
        var ul = el("ul", "mz-check-list");
        mine.forEach(function (c) {
          var level = Object.prototype.hasOwnProperty.call(MARKS, c.level) ? c.level : "ask";
          var row = el("li", "mz-check");
          row.appendChild(el("span", "mz-mark mz-mark--" + level, MARKS[level]));
          var txt = el("span", "mz-check-text");
          txt.appendChild(el("b", "", c.name || "Check"));
          if (c.detail) txt.appendChild(el("span", "", c.detail));
          row.appendChild(txt);
          ul.appendChild(row);
        });
        g.appendChild(ul);
        groups.appendChild(g);
      });
      if (compact) {
        var det = el("details", "mz-act-more");
        det.appendChild(el("summary", "", "Show the checks"));
        det.appendChild(groups);
        li.appendChild(det);
      } else {
        li.appendChild(groups);
      }
    }

    if (a.result && a.result.confirmation != null && a.result.confirmation !== "") {
      var conf = el("p", "mz-act-conf", "Confirmation: ");
      conf.appendChild(el("b", "cc-mono", a.result.confirmation));
      li.appendChild(conf);
    }

    if (a.status === "waiting" && a.id != null) {
      var acts = el("div", "cc-inline mz-act-btns");
      var yes = el("button", "lt-btn lt-btn--primary", "Yes, do it");
      var no = el("button", "lt-btn lt-btn--ghost", "No, don’t");
      yes.type = "button"; no.type = "button";
      var note = el("p", "cc-status mz-act-status");
      note.setAttribute("role", "status");
      var decide = function (approve) {
        say(note, "");
        yes.disabled = true; no.disabled = true;
        api("POST", "/decide", { id: a.id, approve: approve }).then(function (fresh) {
          putAction(fresh);
          var next = actionCard(fresh, compact);
          if (li.parentNode) li.parentNode.replaceChild(next, li);
          next.setAttribute("tabindex", "-1");
          try { next.focus({ preventScroll: true }); } catch (e) { next.focus(); }
          refresh(true);
        }, function (err) {
          yes.disabled = false; no.disabled = false;
          say(note, err.message, "err");
        });
      };
      yes.addEventListener("click", function () { decide(true); });
      no.addEventListener("click", function () { decide(false); });
      acts.appendChild(yes); acts.appendChild(no);
      li.appendChild(acts);
      li.appendChild(note);
    }

    if (a.simulated) li.appendChild(el("p", "cc-fine mz-act-demo", "Demo: nothing real was contacted."));

    // A business the watchdog refused (a lookalike, or no signed policy) can be reported. The panel lives in /shop/shop.js.
    var fake = a.signal === "no_match" && a.id != null && checks.some(function (c) { return c && c.side === "guard" && c.level === "fail"; });
    if (fake && window.LowtidesReport) window.LowtidesReport.mount(li, "action", str(a.id));
    return li;
  }

  // Keep our copies of an action up to date wherever it is held.
  function putAction(a) {
    if (!a || a.id == null) return;
    var i;
    for (i = 0; i < shown.length; i++) if (shown[i].id === a.id) shown[i] = a;
    if (state && Array.isArray(state.actions)) {
      for (i = 0; i < state.actions.length; i++) if (state.actions[i] && state.actions[i].id === a.id) state.actions[i] = a;
    }
  }

  /* ---------- render pieces ---------- */
  function renderTask() {
    var t = state && state.task;
    if (!t) { $.scope.textContent = "No job set yet."; return; }
    var words = (t.scope && t.scope.summary) || t.text || "";
    words = str(words).replace(/[.\s]+$/, "");
    $.scope.textContent = words ? "The job: " + words + ". Anything else, it has to ask you." : "No job set yet.";
  }

  function renderSamples() {
    var list = state && Array.isArray(state.samples) ? state.samples : [];
    var key = JSON.stringify(list);
    if (key === sampleKey) return;   // same examples: keep the buttons (and keyboard focus) as they are
    sampleKey = key;
    clear($.samples);
    if (!list.length) {
      $.samples.appendChild(el("p", "cc-fine", "No examples to try right now."));
      return;
    }
    list.forEach(function (s) {
      if (!s) return;
      var b = el("button", "lt-btn lt-btn--ghost mz-sample");
      b.type = "button";
      b.appendChild(el("span", "mz-sample-label", s.label || s.summary || "Try this"));
      if (s.hint) b.appendChild(el("span", "mz-sample-hint", s.hint));
      b.addEventListener("click", function () {
        say($.tryStatus, "");
        busy(b, function () {
          return api("POST", "/action", {
            action_type: s.action_type, counterparty: s.counterparty,
            params: s.params && typeof s.params === "object" ? s.params : {}, summary: s.summary
          }).then(function (a) {
            shown.unshift(a);
            $.results.insertBefore(actionCard(a, false), $.results.firstChild);
            $.resultsWrap.hidden = false;
            return refresh(true);
          }, function (err) { say($.tryStatus, err.message, "err"); });
        });
      });
      $.samples.appendChild(b);
    });
  }

  function renderResults() {
    // After a refresh, show the server's newest copy of anything already on screen.
    var byId = {};
    (state && Array.isArray(state.actions) ? state.actions : []).forEach(function (a) { if (a && a.id != null) byId[a.id] = a; });
    var active = doc.activeElement;
    if (active && $.results.contains(active)) return;   // do not pull a card out from under the person using it
    clear($.results);
    shown = shown.map(function (a) { return byId[a.id] || a; });
    shown.forEach(function (a) { $.results.appendChild(actionCard(a, false)); });
    $.resultsWrap.hidden = !shown.length;
  }

  function renderRules() {
    var types = state && Array.isArray(state.types) ? state.types.filter(function (t) { return t && t.type != null; }) : [];
    var key = types.map(function (t) { return t.type + "\u0001" + t.label + "\u0001" + (t.undoable === false ? 0 : 1); }).join("\u0002");
    if (key !== ruleKey) {          // build the rows once; later renders only move the checked dot
      ruleKey = key;
      ruleRows = {};
      clear($.rules);
      if (!types.length) $.rules.appendChild(el("p", "cc-fine", "No actions to set rules for yet."));
      types.forEach(function (t) {
        var name = t.label || t.type, id = "mz-r-" + slug(t.type);
        var fs = el("fieldset", "mz-rule");
        fs.setAttribute("data-help", RULE_HELP);
        fs.appendChild(el("legend", "cc-sr", "Rule for: " + name));
        var row = el("div", "mz-rule-in");
        var text = el("div", "mz-rule-text");
        var lab = el("p", "mz-rule-label", name);
        lab.setAttribute("aria-hidden", "true");
        text.appendChild(lab);
        if (t.undoable === false) text.appendChild(el("p", "cc-fine mz-rule-note", "Can’t be undone, so asking first is safest."));
        row.appendChild(text);
        var opts = el("div", "mz-opts"), radios = {};
        LEVELS.forEach(function (lv) {
          var l = el("label", "mz-opt");
          var r = el("input");
          r.type = "radio"; r.name = id; r.value = lv[0]; r.id = id + "-" + lv[0];
          r.addEventListener("change", function () { if (r.checked) saveLevel(t.type, lv[0], fs); });
          radios[lv[0]] = r;
          l.appendChild(r);
          l.appendChild(el("span", "", lv[1]));
          opts.appendChild(l);
        });
        row.appendChild(opts);
        fs.appendChild(row);
        $.rules.appendChild(fs);
        ruleRows[t.type] = { fieldset: fs, radios: radios };
      });
    }
    types.forEach(function (t) {
      var r = ruleRows[t.type];
      if (!r) return;
      LEVELS.forEach(function (lv) { r.radios[lv[0]].checked = t.level === lv[0]; });
    });
  }

  function saveLevel(type, level, fs) {
    var levels = {};
    levels[type] = level;
    var radios = fs.querySelectorAll("input");
    var lock = function (on) { [].forEach.call(radios, function (r) { r.disabled = on; }); };
    var back = doc.activeElement && fs.contains(doc.activeElement) ? doc.activeElement : null;
    say($.rulesStatus, "");
    lock(true);
    api("POST", "/rules", { levels: levels }).then(function (fresh) {
      lock(false);
      state = fresh;
      render();
      say($.rulesStatus, "Saved.", "ok");
    }, function (err) {
      lock(false);
      renderRules();               // put the dot back where the server still has it
      say($.rulesStatus, err.message, "err");
    }).then(function () {
      if (back && doc.body.contains(back)) {
        var now = fs.querySelector("input:checked") || back;
        try { now.focus({ preventScroll: true }); } catch (e) { now.focus(); }
      }
    });
  }

  function renderStop() {
    if (doc.activeElement !== $.word) $.word.value = str(state && state.stop_word);
    var frozen = !!(state && state.frozen);
    $.banner.hidden = !frozen;
    $.freeze.textContent = frozen ? "Release the freeze" : "Freeze everything now";
    $.freeze.className = "lt-btn lt-btn--lg mz-freeze " + (frozen ? "lt-btn--primary" : "lt-btn--danger");
    $.freeze.setAttribute("data-frozen", frozen ? "1" : "0");
  }

  function renderRecent() {
    clear($.recent);
    var onScreen = {};
    shown.forEach(function (a) { if (a && a.id != null) onScreen[a.id] = true; });
    var list = (state && Array.isArray(state.actions) ? state.actions.slice() : []).filter(function (a) {
      return a && !onScreen[a.id];
    });
    list.sort(function (a, b) { return (Number(b.ts) || 0) - (Number(a.ts) || 0); });
    list.slice(0, RECENT_MAX).forEach(function (a) { $.recent.appendChild(actionCard(a, true)); });
    $.recentNone.hidden = list.length > 0;
    $.recentNone.textContent = shown.length ? "Nothing older to show." : "Nothing yet. Try one of the examples above.";
  }

  function render() {
    if (!state) return;
    $.load.hidden = true;
    $.body.hidden = false;
    renderTask();
    renderSamples();
    renderResults();
    renderRules();
    renderStop();
    renderRecent();
  }

  function fail() {
    state = null;
    $.body.hidden = true;
    $.banner.hidden = true;
    $.load.hidden = false;
    $.load.textContent = UNAVAILABLE;
  }

  // quiet = keep what is on screen if the refresh itself fails.
  function refresh(quiet) {
    if (!$.root) return Promise.resolve();
    if (loading) return loading;
    loading = api("GET", "").then(function (fresh) {
      loading = null;
      state = fresh;
      render();
    }, function () {
      loading = null;
      if (!(quiet === true && state)) fail();
    });
    return loading;
  }

  /* ---------- wiring ---------- */
  function init() {
    $.root = doc.getElementById("muzzle");
    if (!$.root) return;
    var ids = {
      load: "mz-load", body: "mz-body", banner: "mz-banner",
      taskForm: "mz-task-form", taskIn: "mz-task", taskGo: "mz-task-go", taskStatus: "mz-task-status", scope: "mz-scope",
      samples: "mz-samples", tryStatus: "mz-try-status", resultsWrap: "mz-results-wrap", results: "mz-results",
      rules: "mz-rules", rulesStatus: "mz-rules-status",
      wordForm: "mz-word-form", word: "mz-word", wordGo: "mz-word-go", freeze: "mz-freeze", stopStatus: "mz-stop-status",
      recent: "mz-recent", recentNone: "mz-recent-none"
    };
    for (var k in ids) {
      if (!Object.prototype.hasOwnProperty.call(ids, k)) continue;
      $[k] = doc.getElementById(ids[k]);
      if (!$[k]) { $.root = null; return; }   // markup missing: do nothing rather than half work
    }

    $.taskForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = $.taskIn.value.trim();
      if (!text) { say($.taskStatus, "Type the job first.", "err"); $.taskIn.focus(); return; }
      say($.taskStatus, "");
      busy($.taskGo, function () {
        return api("POST", "/task", { text: text }).then(function (out) {
          if (state) state.task = out.task || null;
          renderTask();
          say($.taskStatus, out.said || "Job set.", "ok");
          return refresh(true);
        }, function (err) { say($.taskStatus, err.message, "err"); });
      });
    });

    $.wordForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var word = $.word.value.trim();
      if (!word) { say($.stopStatus, "Type a stop word first.", "err"); $.word.focus(); return; }
      say($.stopStatus, "");
      busy($.wordGo, function () {
        return api("POST", "/rules", { stop_word: word }).then(function (fresh) {
          state = fresh;
          render();
          say($.stopStatus, "Saved.", "ok");
        }, function (err) { say($.stopStatus, err.message, "err"); });
      });
    });

    $.freeze.addEventListener("click", function () {
      var release = $.freeze.getAttribute("data-frozen") === "1";
      say($.stopStatus, "");
      busy($.freeze, function () {
        return api("POST", release ? "/release" : "/freeze", {}).then(function (out) {
          if (state) { state.frozen = !!out.frozen; renderStop(); }
          var said = out.said || (out.frozen ? "Frozen." : "Released.");
          return refresh(true).then(function () { say($.stopStatus, said, "ok"); });
        }, function (err) { say($.stopStatus, err.message, "err"); });
      });
    });

    refresh();
  }

  window.LowtidesMuzzle = { refresh: function () { return refresh(true); } };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();
})();
