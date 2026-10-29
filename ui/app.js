/* Lowtides dashboard. Vanilla JS, no build step. */
(function () {
  "use strict";

  var S = null;            // last /api/state
  var grid = null;         // AG Grid instance
  var lastAgent = null;    // newly created agent for the connect panel
  var codeKind = "py-openai";

  // ---------- helpers ----------
  function $(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function money(x) {
    x = Number(x || 0);
    var sign = x < 0 ? "-" : "";
    x = Math.abs(x);
    if (x !== 0 && x < 0.01) return sign + "<$0.01";
    return sign + "$" + x.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function money4(x) {
    x = Number(x || 0);
    if (x === 0) return "$0";
    var sign = x < 0 ? "-" : "";
    var a = Math.abs(x);
    if (a < 0.0001) return sign + "<$0.0001";
    if (a < 0.01) return sign + "$" + a.toFixed(4);
    return money(x);
  }
  function when(ts) {
    var d = new Date(ts * 1000);
    var today = new Date();
    var same = d.toDateString() === today.toDateString();
    return same ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
                : d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function flash(msg, bad) {
    var f = $("flash");
    f.textContent = msg;
    f.className = "flash" + (bad ? " bad" : "");
    f.hidden = false;
    clearTimeout(flash.t);
    flash.t = setTimeout(function () { f.hidden = true; }, 6000);
  }
  function api(method, path, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" }, credentials: "same-origin" };
    var tok = sessionStorage.getItem("lt_admin");
    if (tok) opts.headers["X-Admin-Token"] = tok;
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(function (r) {
      if (r.status === 401 && path.indexOf("/api/") === 0) { showLogin(); throw new Error("Password required"); }
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          var err = new Error(j.error || ("Request failed (" + r.status + ")"));
          err.status = r.status;
          throw err;
        }
        return j;
      });
    });
  }
  function fail(e) { if (e && e.message !== "Password required") flash(e.message || String(e), true); }

  // Features that depend on newer backend endpoints. A 404 turns the feature off
  // and hides its UI quietly instead of showing an error.
  var FEAT = { spend: true, forecast: true, audit: true, merchants: true, rules: true };
  function soft(name, quiet) {
    return function (e) {
      if (e && (e.status === 404 || e.status === 405 || e.status === 501)) { FEAT[name] = false; applyFeatures(); return null; }
      if (!quiet) fail(e);
      return null;
    };
  }
  var RM = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function clock(d) {
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).replace(/\s?AM$/i, " am").replace(/\s?PM$/i, " pm");
  }
  // Count a money figure up or down to its new value.
  function tweenMoney(el, to, fmt) {
    fmt = fmt || money;
    var from = Number(el.dataset.v);
    el.dataset.v = to;
    if (RM.matches || isNaN(from) || from === to) { el.textContent = fmt(to); return; }
    var t0 = null, dur = 700;
    function step(t) {
      if (t0 === null) t0 = t;
      var k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      el.textContent = fmt(from + (to - from) * e);
      if (k < 1 && Number(el.dataset.v) === to) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  // ---------- login ----------
  function showLogin() { $("login").hidden = false; $("login-pw").focus(); }
  $("login-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var pw = $("login-pw").value;
    fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: pw }) })
      .then(function (r) {
        if (!r.ok) { $("login-err").textContent = "That password didn't work."; return; }
        sessionStorage.setItem("lt_admin", pw);
        $("login").hidden = true;
        refresh();
      });
  });

  // ---------- tabs ----------
  function currentTab() {
    var h = (location.hash || "#home").slice(1).split("?")[0];
    if (h === "approvals" && !FEAT.spend) return "home";
    return ["home", "approvals", "tasks", "wallet", "clients", "ledger", "settings"].indexOf(h) >= 0 ? h : "home";
  }
  function showTab() {
    var t = currentTab();
    document.querySelectorAll(".tab").forEach(function (s) { s.hidden = s.id !== "tab-" + t; });
    document.querySelectorAll(".tabs a").forEach(function (a) {
      var on = a.dataset.tab === t;
      a.classList.toggle("on", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    var q = location.hash.split("?")[1] || "";
    if (q.indexOf("paid=1") >= 0) flash("Payment received. Your wallet is topped up.");
    if (q.indexOf("held=1") >= 0) flash("Hold approved on PayPal. The job can start.");
    if (q.indexOf("canceled=1") >= 0) flash("PayPal checkout was canceled. Nothing was charged.", true);
    var m = q.match(/msg=([^&]+)/);
    if (m) flash(decodeURIComponent(m[1]), true);
    if (t === "ledger") loadLedger();
    if (t === "approvals") { loadPending(); loadLog(); }
    if (t === "settings") loadMerchants();
  }
  window.addEventListener("hashchange", showTab);

  // ---------- mode ----------
  document.querySelectorAll(".mode-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      api("POST", "/api/policy", { mode: b.dataset.mode }).then(refresh).catch(fail);
    });
  });

  // ---------- STOP ALL ----------
  $("stop-all").addEventListener("click", function () {
    askKill(null);
  });

  // ---------- render ----------
  function demoBanner() {
    var b = $("demo-banner");
    if (!S.public_demo) { if (b) b.hidden = true; return; }
    if (!b) {
      b = document.createElement("div");
      b.id = "demo-banner";
      b.className = "demo-banner";
      b.setAttribute("role", "note");
      b.innerHTML = '<span><strong>Public demo.</strong> PayPal <strong>sandbox</strong> (test money) and simulated AI models, so nothing here costs ' +
        'real money. Anyone can press the buttons. A runaway agent is already loaded: check <a href="#approvals">Approvals</a>, open an ' +
        "agent's <strong>Rules</strong>, or hit <strong>KILL</strong>.</span>" +
        '<button type="button" class="btn ghost" id="demo-reset">Reset demo</button>';
      var top = document.querySelector("header.top");
      top.parentNode.insertBefore(b, top.nextSibling);
      $("demo-reset").addEventListener("click", function () {
        var btn = $("demo-reset");
        btn.disabled = true;
        btn.textContent = "Resetting...";
        api("POST", "/api/demo/reset", {}).then(function () {
          flash("Demo reset. The runaway-agent story is loaded again.");
          return refresh();
        }).catch(function (e) { flash(String(e.message || e), true); })
          .then(function () { btn.disabled = false; btn.textContent = "Reset demo"; });
      });
    }
    b.hidden = false;
  }

  // Hero: money Lowtides kept from leaving today (counted server-side from real records).
  function renderProtected() {
    var p = S.protected || {};
    var stopped = Number(p.stopped || 0);
    tweenMoney($("prot-today"), stopped);
    var chips = [];
    if (p.scam_held) chips.push(["", plural(p.scam_held, "scam payment held", "scam payments held")]);
    if (p.blocked) chips.push(["", plural(p.blocked, "over-limit purchase blocked", "over-limit purchases blocked")]);
    if (p.cancelled) chips.push(["", plural(p.cancelled, "payout pulled back", "payouts pulled back")]);
    if (p.loops) chips.push(["", plural(p.loops, "runaway loop stopped", "runaway loops stopped")]);
    if (p.waiting) chips.push(["wait", '<a href="#approvals">' + esc(money(p.waiting_amount)) + " waiting for you</a>"]);
    var any = stopped > 0 || p.loops || p.waiting;
    $("prot-label").textContent = stopped > 0 ? "stopped before it left" : (any ? "no money needed stopping" : "nothing needed stopping today");
    if (!chips.length) chips.push(["calm", "Every purchase stayed inside your rules"]);
    $("prot-chips").innerHTML = chips.map(function (c) {
      return "<li" + (c[0] ? ' class="' + c[0] + '"' : "") + ">" + (c[0] === "wait" ? c[1] : esc(c[1])) + "</li>";
    }).join("");
  }

  function render() {
    if (!S) return;
    document.title = S.app;
    $("app-name").textContent = S.app === "Lowtides" ? "lowtides" : S.app;  // brand wordmark is lowercase
    document.body.classList.toggle("biz", S.mode === "business");
    document.querySelectorAll(".mode-btn").forEach(function (b) {
      var on = b.dataset.mode === S.mode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderProtected();
    var t = S.today.totals;
    tweenMoney($("saved-today"), Number(t.saved || 0));
    $("saved-sub").textContent = "Would have cost " + money(t.baseline) + ". You paid " + money(t.cost) + "." +
      (t.baseline > 0 ? " That's " + S.today.percent_saved + "% less." : "");
    var lines = t.calls ? S.today.lines : ["No calls yet today. Connect an agent or press Watch it work."];
    $("receipt-lines").innerHTML = lines.map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("");
    var w = S.week.totals;
    $("week-line").textContent = w.calls ? ("Last 7 days: " + w.calls + " calls, saved " + money(w.saved) + " (" + S.week.percent_saved + "%).") : "";
    var sim = S.agents.some(function (a) { return a.simulated; });
    $("foot-line").textContent = S.app + " " + S.version + "." + (sim ? " Rows from the demo agent use a simulated model and are labeled demo." : "") +
      (S.admin_protected || S.public_demo ? "" : " This dashboard has no password yet. Set LOWTIDES_ADMIN_TOKEN before putting it online.");
    demoBanner();
    $("heart-line").hidden = !S.config.show_signature_heart;

    $("setup").hidden = S.agents.length > 0;
    renderAgents();
    renderTide();
    renderEvents();
    renderSelects();
    renderTasks();
    renderWallets();
    renderClients();
    renderSettings();
  }

  function renderAgents() {
    var box = $("agents");
    if (!S.agents.length) { box.innerHTML = '<div class="empty">No agents yet. Use the three steps above to add your first one.</div>'; return; }
    box.innerHTML = S.agents.map(function (a) {
      var pct = a.daily_budget ? Math.min(100, a.spent_today / a.daily_budget * 100) : 100;
      var cls = pct >= 100 ? "full" : (pct >= 80 ? "hot" : "");
      var status = a.status === "on" ? "Running" : (a.status === "paused" ? "Paused by the loop guard" : "Stopped");
      var btn = a.status === "on"
        ? '<button class="btn stop sm" data-act="stop" data-id="' + a.id + '">STOP</button>'
        : (a.status === "paused"
          ? '<button class="btn warn sm" data-act="resume" data-id="' + a.id + '">Resume</button>'
          : '<button class="btn go sm" data-act="start" data-id="' + a.id + '">Start</button>');
      var wallet = a.use_wallet ? " Wallet " + money(a.balance) + "." : "";
      var nm = esc(a.name);
      var kill = a.status === "stopped" ? "" :
        '<button class="btn kill sm" data-act="kill" data-id="' + a.id + '" aria-label="Kill ' + nm + '">KILL</button>';
      var rules = FEAT.rules ? '<button class="btn ghost sm" data-act="rules" data-id="' + a.id + '" aria-label="Rules for ' + nm + '">Rules</button>' : "";
      return '<div class="agent">' +
        '<div class="light ' + a.light + '" role="img" aria-label="' + esc(a.light) + ' light"><i></i><i></i><i></i></div>' +
        '<div><div class="agent-name">' + esc(a.name) + (a.simulated ? '<span class="tag">demo</span>' : "") + '</div>' +
        '<div class="agent-meta">' + esc(status) + ". Spent " + money4(a.spent_today) + " of " + money(a.daily_budget) + " today." + esc(wallet) + "</div>" +
        '<div class="bar ' + cls + '"><b style="width:' + pct.toFixed(1) + '%"></b></div></div>' +
        '<div class="agent-actions">' + btn + kill + rules +
        '<button class="btn ghost sm" data-act="key" data-id="' + a.id + '">Connect</button>' +
        '<button class="btn ghost sm" data-act="edit" data-id="' + a.id + '">Allowance</button>' +
        '<button class="btn ghost sm" data-act="wallet" data-id="' + a.id + '">' + (a.use_wallet ? "Wallet off" : "Pay from wallet") + "</button>" +
        "</div></div>";
    }).join("");
  }

  $("agents").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-act]");
    if (!b) return;
    var id = Number(b.dataset.id);
    var a = S.agents.find(function (x) { return x.id === id; });
    var act = b.dataset.act;
    if (act === "stop" || act === "start" || act === "resume") {
      api("POST", "/api/agents/" + id + "/" + act).then(refresh).catch(fail);
    } else if (act === "key") {
      lastAgent = a; showConnect();
    } else if (act === "edit") {
      var v = prompt("Daily allowance in dollars for " + a.name + ":", a.daily_budget);
      if (v === null) return;
      var t = prompt("Most one job can spend, in dollars:", a.task_budget);
      if (t === null) return;
      api("POST", "/api/agents/" + id + "/update", { daily_budget: Number(v), task_budget: Number(t) })
        .then(function () { flash("Allowance saved."); refresh(); }).catch(fail);
    } else if (act === "wallet") {
      api("POST", "/api/agents/" + id + "/update", { use_wallet: !a.use_wallet }).then(refresh).catch(fail);
    } else if (act === "kill") {
      askKill(a, b);
    } else if (act === "rules") {
      openRules(a, b);
    }
  });

  function renderEvents() {
    var ev = S.events || [];
    $("events").innerHTML = ev.length ? ev.map(function (e) {
      return "<li><time>" + esc(when(e.ts)) + "</time><span>" + esc(e.message) + "</span></li>";
    }).join("") : '<li class="none"><span class="muted">Nothing yet. Budget hits, loops, holds and payments show up here.</span></li>';
  }

  function agentOptions(sel) {
    return S.agents.map(function (a) {
      return '<option value="' + a.id + '"' + (sel === a.id ? " selected" : "") + ">" + esc(a.name) + "</option>";
    }).join("");
  }
  function renderSelects() {
    ["try-agent", "t-agent"].forEach(function (id) {
      var el = $(id), v = Number(el.value);
      el.innerHTML = agentOptions(v || (S.agents[0] && S.agents[0].id));
    });
    var tm = $("try-model"), v = tm.value;
    var names = Object.keys(S.config.models);
    tm.innerHTML = names.map(function (n) {
      return '<option value="' + esc(n) + '"' + (n === v ? " selected" : "") + ">" + esc(n) + "</option>";
    }).join("");
    if (!v) {
      var anth = (S.config.providers || {}).anthropic || {};
      var pick = names.indexOf("claude-opus-5-5") >= 0 && anth.key_set ? "claude-opus-5-5" : (names.indexOf("sim-large") >= 0 ? "sim-large" : names[0]);
      if (pick) tm.value = pick;
    }
  }

  // ---------- setup & connect ----------
  $("setup-budget").addEventListener("input", function () { $("setup-budget-val").textContent = "$" + this.value; });
  $("setup-go").addEventListener("click", function () { createAgent($("setup-name").value, Number($("setup-budget").value)); });
  $("add-agent").addEventListener("click", function () {
    var n = prompt("Name your agent:");
    if (!n) return;
    var b = prompt("Daily allowance in dollars:", "5");
    if (b === null) return;
    createAgent(n, Number(b));
  });
  function createAgent(name, daily) {
    if (!name || !name.trim()) { flash("Give your agent a name first.", true); return; }
    api("POST", "/api/agents", { name: name.trim(), daily_budget: daily, task_budget: Math.max(0.5, daily / 5) })
      .then(function (r) { lastAgent = r.agent; flash(name + " is on and governed."); return refresh().then(showConnect); })
      .catch(fail);
  }
  function showConnect() {
    if (!lastAgent) return;
    $("connect").hidden = false;
    $("c-base").textContent = location.origin + "/v1";
    $("c-key").textContent = lastAgent.key;
    renderCode();
    $("connect").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  $("connect-done").addEventListener("click", function () { $("connect").hidden = true; });
  document.querySelectorAll(".code-tabs button").forEach(function (b) {
    b.addEventListener("click", function () {
      codeKind = b.dataset.code;
      document.querySelectorAll(".code-tabs button").forEach(function (x) { x.classList.toggle("on", x === b); });
      renderCode();
    });
  });
  function renderCode() {
    var key = lastAgent ? lastAgent.key : "YOUR_LOWTIDES_KEY";
    var o = location.origin;
    var c = {
      "py-openai": 'from openai import OpenAI\n\nclient = OpenAI(base_url="' + o + '/v1", api_key="' + key + '")\nreply = client.chat.completions.create(\n    model="sim-large",  # or any model on your Models list\n    messages=[{"role": "user", "content": "Hello!"}],\n    extra_headers={"X-Lowtides-Task": "my-job-1"},  # optional: group calls into a job\n)\nprint(reply.choices[0].message.content)',
      "py-anthropic": 'import anthropic\n\nclient = anthropic.Anthropic(base_url="' + o + '", api_key="' + key + '")\nreply = client.messages.create(\n    model="claude-opus-5-5",\n    max_tokens=1024,\n    messages=[{"role": "user", "content": "Hello!"}],\n)\nprint(reply.content[0].text)',
      "curl": 'curl ' + o + '/v1/chat/completions \\\n  -H "Authorization: Bearer ' + key + '" \\\n  -H "Content-Type: application/json" \\\n  -d \'{"model":"sim-large","messages":[{"role":"user","content":"Hello!"}]}\''
    };
    $("code-box").textContent = c[codeKind];
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-copy]");
    if (!b) return;
    var text = $(b.dataset.copy).textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () {
      b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1500);
    }).catch(function () { prompt("Copy this:", text); });
  });

  // ---------- demo & try ----------
  $("run-demo").addEventListener("click", function () {
    var b = this; b.disabled = true; b.textContent = "Running…";
    api("POST", "/api/demo", {}).then(function (r) {
      flash("Demo done: " + r.results.length + " calls ran through the real Lowtides pipeline on a simulated model.");
      return refresh();
    }).catch(fail).then(function () { b.disabled = false; b.textContent = "Watch it work (demo)"; });
  });
  $("run-loop").addEventListener("click", function () {
    api("POST", "/api/demo", { loop: true }).then(function (r) {
      flash(r.agent_status === "paused" ? "The loop guard paused the demo agent after it repeated itself. Press Resume to let it run again."
                                        : "The demo agent didn't trip the loop guard this time.");
      return refresh();
    }).catch(fail);
  });
  $("try-go").addEventListener("click", sendTry);
  $("try-prompt").addEventListener("keydown", function (e) { if (e.key === "Enter") sendTry(); });
  function sendTry() {
    var aid = Number($("try-agent").value), p = $("try-prompt").value.trim();
    if (!aid) { flash("Add an agent first.", true); return; }
    if (!p) { flash("Type a question first.", true); return; }
    var b = $("try-go"); b.disabled = true;
    api("POST", "/api/try", { agent_id: aid, prompt: p, model: $("try-model").value }).then(function (r) {
      var h = r.headers || {}, out = $("try-out");
      out.hidden = false;
      if (r.status === 200) {
        out.innerHTML = '<div class="meta">Answered by ' + esc(h["X-Lowtides-Model-Used"]) + ". Cost " + esc(money4(Number(h["X-Lowtides-Cost"]))) +
          ". Saved " + esc(money4(Number(h["X-Lowtides-Saved"]))) + ".</div><div>" + esc(r.text) + "</div>";
      } else {
        out.innerHTML = '<div class="meta">Stopped by your rules.</div><div>' + esc(r.text) + "</div>";
      }
      return refresh();
    }).catch(fail).then(function () { b.disabled = false; });
  }

  // ---------- tasks ----------
  $("t-quote").addEventListener("click", function () {
    var aid = Number($("t-agent").value);
    if (!aid) { flash("Add an agent first.", true); return; }
    var body = { agent_id: aid, label: $("t-label").value.trim(), description: $("t-desc").value.trim() };
    var cap = $("t-budget").value;
    if (cap) body.budget = Number(cap);
    api("POST", "/api/tasks", body).then(function (r) {
      var q = r.quote, el = $("t-result");
      el.hidden = false;
      el.innerHTML =
        '<div class="q-cell"><div class="k">Low</div><div class="v">' + esc(money4(q.low)) + "</div></div>" +
        '<div class="q-cell"><div class="k">Likely</div><div class="v">' + esc(money4(q.likely)) + "</div></div>" +
        '<div class="q-cell"><div class="k">High</div><div class="v">' + esc(money4(q.high)) + "</div></div>" +
        '<button class="btn go" data-hold="' + esc(r.task_id) + '">Hold ' + esc(money(Math.max(r.budget, 1))) + " with PayPal</button>" +
        '<p class="muted small q-basis">Based on ' + esc(q.basis) + ". Job ID: <code>" + esc(r.task_id) + "</code>. Budget cap " + esc(money4(r.budget)) + ".</p>";
      return refresh();
    }).catch(fail);
  });
  document.addEventListener("click", function (ev) {
    var h = ev.target.closest("button[data-hold]");
    if (h) {
      if (!S.paypal_ready) { flash("PayPal isn't connected yet. Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET, then restart.", true); return; }
      api("POST", "/api/tasks/" + encodeURIComponent(h.dataset.hold) + "/hold").then(function (r) {
        if (r.approve_url) location.href = r.approve_url;
      }).catch(fail);
      return;
    }
    var s = ev.target.closest("button[data-task-act]");
    if (s) {
      api("POST", "/api/tasks/" + encodeURIComponent(s.dataset.id) + "/" + s.dataset.taskAct).then(function (r) {
        if (s.dataset.taskAct === "settle") {
          flash("Job closed. Cost " + money4(r.spent) + ". Charged " + money(r.charged) + ". Released " + money(r.released) + ".");
        }
        return refresh();
      }).catch(fail);
    }
  });
  function renderTasks() {
    var rows = S.tasks || [];
    if (!rows.length) { $("task-list").innerHTML = '<div class="empty">No jobs yet. Get a quote above to start one.</div>'; return; }
    $("task-list").innerHTML = '<div class="table-wrap"><table class="plain"><thead><tr><th>Job</th><th>Agent</th><th class="num">Quote (likely)</th><th class="num">Budget</th><th>PayPal</th><th>Status</th><th></th></tr></thead><tbody>' +
      rows.map(function (t) {
        var a = S.agents.find(function (x) { return x.id === t.agent_id; });
        var acts = "";
        if (t.status === "open") {
          if (!t.hold_auth_id && t.status === "open") acts += '<button class="btn ghost sm" data-hold="' + esc(t.id) + '">Hold</button> ';
          acts += '<button class="btn go sm" data-task-act="settle" data-id="' + esc(t.id) + '">Finish &amp; pay</button>';
          if (t.hold_auth_id) acts += ' <button class="btn ghost sm" data-task-act="cancel" data-id="' + esc(t.id) + '">Release hold</button>';
        }
        var pp = t.paypal_status ? esc(t.paypal_status.replace(/_/g, " ").toLowerCase()) + (t.hold_amount ? " (" + money(t.hold_amount) + ")" : "") : "none";
        if (t.captured_amount) pp += ", charged " + money(t.captured_amount);
        return "<tr><td>" + esc(t.label || "(no name)") + '<br><code class="small">' + esc(t.id) + "</code></td><td>" + esc(a ? a.name : "") +
          '</td><td class="num">' + esc(money4(t.quote_likely)) + '</td><td class="num">' + esc(money4(t.budget)) + "</td><td>" + pp +
          "</td><td>" + esc(t.status) + "</td><td>" + acts + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  // ---------- wallet ----------
  function renderWallets() {
    if (!S.agents.length) { $("wallets").innerHTML = '<div class="empty">Add an agent first.</div>'; return; }
    $("wallets").innerHTML = '<div class="table-wrap"><table class="plain"><thead><tr><th>Agent</th><th class="num">Balance</th><th>Pays from wallet</th><th>Top up with PayPal</th></tr></thead><tbody>' +
      S.agents.map(function (a) {
        return "<tr><td>" + esc(a.name) + '</td><td class="num">' + esc(money4(a.balance)) + "</td><td>" + (a.use_wallet ? "Yes" : "No") + "</td><td>" +
          [5, 10, 25].map(function (n) { return '<button class="btn ghost sm" data-topup="' + a.id + '" data-amt="' + n + '">$' + n + "</button>"; }).join(" ") +
          "</td></tr>";
      }).join("") + "</tbody></table></div>" +
      (S.paypal_ready ? "" : '<p class="muted small">PayPal isn\'t connected yet. Add your sandbox keys (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET) and restart Lowtides.</p>');
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-topup]");
    if (!b) return;
    if (!S.paypal_ready) { flash("PayPal isn't connected yet.", true); return; }
    api("POST", "/api/wallet/topup", { agent_id: Number(b.dataset.topup), amount: Number(b.dataset.amt) }).then(function (r) {
      if (r.approve_url) location.href = r.approve_url;
    }).catch(fail);
  });

  // ---------- clients ----------
  $("cl-add").addEventListener("click", function () {
    api("POST", "/api/clients", { name: $("cl-name").value, email: $("cl-email").value, markup_percent: Number($("cl-markup").value || 0) })
      .then(function () { flash("Client added. Assign agents to them below."); $("cl-name").value = ""; $("cl-email").value = ""; return refresh(); })
      .catch(fail);
  });
  function renderClients() {
    var cl = S.clients || [];
    $("client-list").innerHTML = cl.map(function (c) {
      var mine = S.agents.filter(function (a) { return a.client_id === c.id; });
      var others = S.agents.filter(function (a) { return a.client_id !== c.id; });
      return '<div class="panel"><h3 style="margin-top:0">' + esc(c.name) + "</h3><p class=\"muted small\">" + esc(c.email || "No billing email yet") +
        ". Markup " + esc(c.markup_percent) + "%. Agents: " + (mine.length ? mine.map(function (a) { return esc(a.name); }).join(", ") : "none") + ".</p>" +
        '<div class="row-actions">' +
        (others.length ? '<select data-assign="' + c.id + '" aria-label="Assign an agent"><option value="">Assign an agent…</option>' +
          others.map(function (a) { return '<option value="' + a.id + '">' + esc(a.name) + "</option>"; }).join("") + "</select>" : "") +
        '<button class="btn ghost sm" data-inv="' + c.id + '" data-preview="1">Preview last 30 days</button>' +
        '<button class="btn go sm" data-inv="' + c.id + '">Send PayPal invoice</button></div><div class="inv-out" id="inv-' + c.id + '"></div></div>';
    }).join("");
    var inv = S.invoices || [];
    $("invoice-list").innerHTML = inv.length ? '<div class="table-wrap"><table class="plain"><thead><tr><th>Client</th><th class="num">Amount</th><th>Status</th><th>PayPal ID</th></tr></thead><tbody>' +
      inv.map(function (i) {
        return "<tr><td>" + esc(i.client_name) + '</td><td class="num">' + esc(money(i.amount)) + "</td><td>" + esc(i.status) + "</td><td><code>" + esc(i.paypal_invoice_id) + "</code></td></tr>";
      }).join("") + "</tbody></table></div>" : '<div class="empty">No invoices sent yet.</div>';
  }
  document.addEventListener("change", function (ev) {
    var s = ev.target.closest("select[data-assign]");
    if (!s || !s.value) return;
    api("POST", "/api/agents/" + s.value + "/update", { client_id: Number(s.dataset.assign) }).then(refresh).catch(fail);
  });
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-inv]");
    if (!b) return;
    var cid = b.dataset.inv, preview = !!b.dataset.preview;
    if (!preview && !S.paypal_ready) { flash("PayPal isn't connected yet.", true); return; }
    if (!preview && !confirm("Create and send a PayPal invoice to this client for the last 30 days?")) return;
    api("POST", "/api/clients/" + cid + "/invoice", { days: 30, preview: preview, send: !preview }).then(function (r) {
      var out = $("inv-" + cid);
      if (preview) out.innerHTML = '<p class="small"><strong>' + esc(money(r.amount)) + "</strong> for " + esc(r.period) + ". " + esc(r.receipt) + "</p>";
      else { flash("Invoice " + r.status.toLowerCase() + ": " + money(r.amount) + "."); return refresh(); }
    }).catch(fail);
  });

  // ---------- ledger ----------
  function loadLedger() {
    api("GET", "/api/ledger?limit=1000").then(function (r) {
      var rows = r.rows.map(function (x) {
        return {
          when: new Date(x.ts * 1000).toLocaleString(), agent: x.agent_name, task: x.task_id || "",
          asked: x.requested_model, used: x.used_model, how: x.source === "model" ? (x.escalations ? "moved up " + x.escalations + "x" : "model") : x.source,
          tokens_in: x.input_tokens + x.cache_read_tokens + x.cache_write_tokens, tokens_out: x.output_tokens,
          cost: x.cost, would_have: x.baseline_cost, saved: x.baseline_cost - x.cost,
          estimated: x.usage_estimated ? "yes" : "", note: x.note
        };
      });
      var el = $("grid");
      if (window.agGrid && agGrid.createGrid) {
        var moneyFmt = function (p) { return money4(p.value); };
        var cols = [
          { field: "when", width: 170 }, { field: "agent" }, { field: "task", width: 120 },
          { field: "asked", headerName: "Asked for" }, { field: "used", headerName: "Answered by" }, { field: "how", width: 120 },
          { field: "tokens_in", headerName: "Tokens in", type: "numericColumn", width: 110 },
          { field: "tokens_out", headerName: "Tokens out", type: "numericColumn", width: 110 },
          { field: "cost", headerName: "Paid", type: "numericColumn", valueFormatter: moneyFmt, width: 110 },
          { field: "would_have", headerName: "Would have cost", type: "numericColumn", valueFormatter: moneyFmt, width: 140 },
          { field: "saved", headerName: "Saved (+/-)", type: "numericColumn", valueFormatter: moneyFmt, width: 110 },
          { field: "estimated", headerName: "Estimated", width: 100 }, { field: "note", flex: 1, minWidth: 260 }
        ];
        if (grid) { grid.setGridOption("rowData", rows); return; }
        grid = agGrid.createGrid(el, { columnDefs: cols, rowData: rows, defaultColDef: { sortable: true, filter: true, resizable: true },
          pagination: true, paginationPageSize: 50, paginationPageSizeSelector: [50, 100, 500] });
      } else {
        el.style.height = "auto";
        el.innerHTML = '<div class="table-wrap"><table class="plain"><thead><tr><th>When</th><th>Agent</th><th>Asked for</th><th>Answered by</th><th class="num">Paid</th><th class="num">Would have cost</th><th class="num">Saved</th><th>Note</th></tr></thead><tbody>' +
          rows.slice(0, 300).map(function (x) {
            return "<tr><td>" + esc(x.when) + "</td><td>" + esc(x.agent) + "</td><td>" + esc(x.asked) + "</td><td>" + esc(x.used) + '</td><td class="num">' + esc(money4(x.cost)) +
              '</td><td class="num">' + esc(money4(x.would_have)) + '</td><td class="num">' + esc(money4(x.saved)) + "</td><td>" + esc(x.note) + "</td></tr>";
          }).join("") + "</tbody></table></div>";
      }
    }).catch(fail);
  }

  // ---------- settings ----------
  var POLICY = [
    ["trim_context", "bool", "Trim old tool output", "Shortens big old tool results the agent keeps re-reading. Recent turns are never touched."],
    ["auto_prompt_cache", "bool", "Add prompt caching", "Marks long, unchanging instructions as cacheable on Claude so re-reads cost about a tenth."],
    ["answer_memory", "bool", "Answer memory", "Repeat questions are answered free. Tool calls are never reused."],
    ["allow_downgrade", "bool", "Use cheaper models for easy steps", "Never goes above the model your agent asked for."],
    ["verify_and_escalate", "bool", "Check answers and move up when weak", "If a cheap answer is empty, unsure or broken JSON, try the next model up."],
    ["default_daily_budget", "num", "Default daily allowance ($)", "Used for new agents."],
    ["default_task_budget", "num", "Default job cap ($)", "Most one job can spend if no quote cap is set."],
    ["loop_max_repeats", "num", "Loop guard: repeats before pausing", "Same request this many times in the window pauses the agent. 0 turns it off."],
    ["loop_window_seconds", "num", "Loop guard window (seconds)", "How far back the loop guard looks."],
    ["max_calls_per_minute", "num", "Speed limit (calls per minute)", "Calls over the limit are refused with a retry message, so a burst can't pile up costs."],
    ["trim_keep_recent_turns", "num", "Recent turns kept whole", "These messages are never trimmed."],
    ["max_output_tokens_cap", "num", "Longest answer allowed (tokens)", "Stops one runaway answer from eating the budget."],
    ["near_duplicate_threshold", "num", "Near-duplicate match (0 to 1)", "How close a short question must be to reuse an answer. 1 means exact only."],
    ["shadow_sample_rate", "num", "Quality spot-checks (0 to 1)", "Share of cheaper answers re-run on the model asked for, to prove quality. Costs extra and is shown as extra."]
  ];
  function renderSettings() {
    var p = S.config.policy;
    $("policy").innerHTML = POLICY.map(function (row) {
      var k = row[0], v = p[k];
      if (row[1] === "bool") {
        return '<label class="pol"><input type="checkbox" data-pol="' + k + '"' + (v ? " checked" : "") + '><span class="t">' + esc(row[2]) + '</span><span class="d">' + esc(row[3]) + "</span></label>";
      }
      return '<label class="pol num"><span class="t">' + esc(row[2]) + '</span><input type="number" step="any" min="0" data-pol="' + k + '" value="' + esc(v) + '"><span class="d">' + esc(row[3]) + "</span></label>";
    }).join("") +
      '<label class="pol num"><span class="t">Routing</span><select data-pol="routing">' +
      ["cascade", "route", "off"].map(function (m) { return '<option value="' + m + '"' + (p.routing === m ? " selected" : "") + ">" + m + "</option>"; }).join("") +
      '</select><span class="d">Cascade: cheapest likely fit, then climb. Route: one pick, no climbing. Off: always the model asked for.</span></label>';

    var models = S.config.models;
    $("models").innerHTML = '<div class="table-wrap"><table class="plain"><thead><tr><th>Model</th><th>Provider</th><th>Ladder</th><th>Tier</th><th class="num">Input $</th><th class="num">Output $</th><th class="num">Cache read $</th><th></th></tr></thead><tbody>' +
      Object.keys(models).map(function (n) {
        var m = models[n];
        return "<tr><td>" + esc(n) + (m.note ? '<br><span class="muted small">' + esc(m.note) + "</span>" : "") + "</td><td>" + esc(m.provider) + "</td><td>" + esc(m.family) + "</td><td>" + esc(m.tier) +
          '</td><td class="num">' + esc(m.input) + '</td><td class="num">' + esc(m.output) + '</td><td class="num">' + esc(m.cache_read == null ? "" : m.cache_read) +
          '</td><td><button class="btn ghost sm" data-del-model="' + esc(n) + '">Remove</button></td></tr>';
      }).join("") + "</tbody></table></div>";
    var provs = S.config.providers;
    $("m-provider").innerHTML = Object.keys(provs).map(function (n) { return '<option value="' + esc(n) + '">' + esc(n) + "</option>"; }).join("");
    $("providers").innerHTML = '<div class="table-wrap"><table class="plain"><thead><tr><th>Name</th><th>Type</th><th>Address</th><th>Key</th></tr></thead><tbody>' +
      Object.keys(provs).map(function (n) {
        var p2 = provs[n];
        var key = p2.type === "simulator" ? "not needed (demo)" : (p2.api_key_env ? (p2.key_set ? "set (" + p2.api_key_env + ")" : "missing: set " + p2.api_key_env) : "none needed");
        return "<tr><td>" + esc(n) + "</td><td>" + esc(p2.type) + "</td><td>" + esc(p2.base_url || "") + "</td><td>" + esc(key) + "</td></tr>";
      }).join("") + '<tr><td>PayPal</td><td>payments</td><td>' + esc(S.config.paypal.env) + "</td><td>" + (S.paypal_ready ? "set" : "missing: set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET") + "</td></tr></tbody></table></div>";
  }
  $("policy-save").addEventListener("click", function () {
    var body = {};
    document.querySelectorAll("[data-pol]").forEach(function (el) {
      var k = el.dataset.pol;
      if (el.type === "checkbox") body[k] = el.checked;
      else if (el.tagName === "SELECT") body[k] = el.value;
      else body[k] = Number(el.value);
    });
    api("POST", "/api/policy", body).then(function () { flash("Rules saved."); return refresh(); }).catch(fail);
  });
  $("m-save").addEventListener("click", function () {
    var body = { name: $("m-name").value.trim(), provider: $("m-provider").value, family: $("m-family").value.trim() || $("m-provider").value,
      tier: Number($("m-tier").value), input: Number($("m-in").value || 0), output: Number($("m-out").value || 0) };
    api("POST", "/api/models", body).then(function () { flash("Model saved."); return refresh(); }).catch(fail);
  });
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-del-model]");
    if (!b) return;
    if (!confirm("Remove " + b.dataset.delModel + " from your price list?")) return;
    api("DELETE", "/api/models/" + encodeURIComponent(b.dataset.delModel)).then(refresh).catch(fail);
  });
  $("p-save").addEventListener("click", function () {
    api("POST", "/api/providers", { name: $("p-name").value.trim(), type: "openai", base_url: $("p-url").value.trim(), api_key_env: $("p-env").value.trim() })
      .then(function () { flash("Connection saved. Add its models below so Lowtides can price them."); return refresh(); }).catch(fail);
  });
  $("mem-clear").addEventListener("click", function () {
    api("POST", "/api/memory/clear", {}).then(function () { flash("All remembered answers are forgotten."); }).catch(fail);
  });

  // ---------- feature visibility ----------
  function applyFeatures() {
    $("approvals-link").hidden = !FEAT.spend;
    if (!FEAT.spend) { $("appr-banner").hidden = true; if ((location.hash || "").indexOf("#approvals") === 0) location.hash = "#home"; }
    $("merchants-panel").hidden = !FEAT.merchants;
    if (!FEAT.audit) $("audit-line").hidden = true;
    if (!FEAT.forecast) FC = {};
    if (S) { renderAgents(); renderTide(); }
  }

  // ---------- tide chart ----------
  // Each agent gets a dot field: 12 columns, 8 rows of "water" under one amber
  // limit row. Lit sea-glass dots rise from the bottom as the day's spend grows.
  var FC = {};               // forecast by agent id
  var COLS = 12, ROWS = 8, STEP = 20;
  var SVGNS = "http://www.w3.org/2000/svg";

  function tideCard(id) {
    var card = document.createElement("article");
    card.className = "tcard";
    card.dataset.id = id;
    var svg = '<svg class="tsvg" viewBox="0 0 240 184" aria-hidden="true" focusable="false">' +
      '<line class="t-limit-line" x1="2" y1="10" x2="238" y2="10"></line>';
    for (var c = 0; c < COLS; c++) svg += '<circle class="t-lim" cx="' + (c * STEP + 10) + '" cy="10" r="5.5"></circle>';
    for (var r = 0; r < ROWS; r++) {
      for (var c2 = 0; c2 < COLS; c2++) {
        svg += '<circle class="t-dot" data-r="' + r + '" data-c="' + c2 + '" style="--c:' + c2 + '" cx="' + (c2 * STEP + 10) + '" cy="' + (170 - r * STEP) + '" r="5.5"></circle>';
      }
    }
    svg += '<g class="t-pace" hidden><line x1="0" y1="0" x2="240" y2="0"></line></g></svg>';
    card.innerHTML =
      '<header class="tcard-head"><h3 class="tcard-name"></h3><span class="tstat"></span></header>' +
      '<div class="tfield">' + svg + "</div>" +
      '<p class="tnums"><span class="tspent" data-v="">$0.00</span> <span class="tof"></span></p>' +
      '<p class="tpace"></p>';
    return card;
  }

  function tideState(a) {
    var f = FC[a.id];
    var spent = f ? Number(f.spent_today || 0) : Number(a.spent_today || 0);
    var limit = f && f.limit ? Number(f.limit) : Number(a.daily_budget || 0);
    var pct = limit > 0 ? spent / limit : (spent > 0 ? 1 : 0);
    var paceText = "", pace = null;
    if (f) {
      var proj = Number(f.projected_today || 0);
      pace = limit > 0 ? proj / limit : null;
      if (pct >= 1) paceText = "At the limit. New spend is refused until tomorrow.";
      else if (a.status !== "on") paceText = "Not spending while it's " + (a.status === "paused" ? "paused." : "stopped.");
      else if (f.minutes_to_limit != null && f.minutes_to_limit >= 0) {
        var at = new Date(Date.now() + f.minutes_to_limit * 60000);
        paceText = at.toDateString() === new Date().toDateString()
          ? "At this pace it hits the limit at " + clock(at) + "."
          : "At this pace it stays under the limit today.";
      } else if (spent === 0 && proj === 0) paceText = "No spend yet today.";
      else paceText = "At this pace it ends the day near " + money(proj) + ", under the limit.";
    }
    return { spent: spent, limit: limit, pct: pct, pace: pace, paceText: paceText };
  }

  function renderTide() {
    var box = $("tide-grid");
    if (!S.agents.length) {
      box.innerHTML = '<p class="tide-empty">No agents yet. Add one below and its tide shows up here.</p>';
      return;
    }
    var empty = box.querySelector(".tide-empty");
    if (empty) empty.remove();
    var keep = {};
    S.agents.forEach(function (a, i) {
      keep[a.id] = true;
      var card = box.querySelector('.tcard[data-id="' + a.id + '"]');
      if (!card) { card = tideCard(a.id); }
      if (box.children[i] !== card) box.insertBefore(card, box.children[i] || null);
      updateTideCard(card, a, tideState(a));
    });
    Array.prototype.slice.call(box.querySelectorAll(".tcard")).forEach(function (c) { if (!keep[c.dataset.id]) c.remove(); });
  }

  function updateTideCard(card, a, t) {
    card.querySelector(".tcard-name").textContent = a.name;
    var st = card.querySelector(".tstat");
    var label = a.status === "on" ? "Running" : (a.status === "paused" ? "Paused" : "Stopped");
    st.textContent = label;
    st.className = "tstat " + a.status;
    var over = t.pct >= 1, warn = t.pct >= 0.8;
    card.classList.toggle("warn", warn && !over);
    card.classList.toggle("over", over);
    tweenMoney(card.querySelector(".tspent"), t.spent, money4);
    card.querySelector(".tof").textContent = "of " + money(t.limit) + " limit · " + Math.round(Math.min(t.pct, 9.99) * 100) + "%";
    var pt = card.querySelector(".tpace");
    pt.textContent = t.paceText;
    pt.hidden = !t.paceText;

    // Fill the field: whole rows from the bottom, then the partial top row left to right.
    var level = Math.max(0, Math.min(1, t.pct)) * ROWS;
    var full = Math.floor(level), part = Math.round((level - full) * COLS);
    if (t.spent > 0 && full === 0 && part === 0) part = 1; // any spend shows at least one dot
    var dots = card.querySelectorAll(".t-dot"), changed = [];
    for (var i = 0; i < dots.length; i++) {
      var d = dots[i], r = Number(d.dataset.r), c = Number(d.dataset.c);
      var on = r < full || (r === full && c < part);
      if (d.classList.contains("on") !== on) changed.push({ d: d, on: on, k: r * COLS + c });
      d.classList.toggle("hi", r >= ROWS - 2);
    }
    var rising = changed.length && changed[0].on;
    changed.sort(function (x, y) { return rising ? x.k - y.k : y.k - x.k; });
    var stepMs = changed.length ? Math.min(25, 900 / changed.length) : 0;
    changed.forEach(function (x, n) {
      x.d.style.transitionDelay = RM.matches ? "0ms" : Math.round(n * stepMs) + "ms";
      x.d.classList.toggle("on", x.on);
    });

    var pace = card.querySelector(".t-pace");
    if (t.pace == null || t.pct >= 1) { pace.setAttribute("hidden", ""); }
    else {
      pace.removeAttribute("hidden");
      var y = 180 - Math.max(0, Math.min(1, t.pace)) * 160;
      pace.style.transform = "translateY(" + Math.max(20, y).toFixed(1) + "px)";
      pace.classList.toggle("past", t.pace >= 1);
    }
  }

  function loadForecast() {
    if (!FEAT.forecast) return Promise.resolve();
    return api("GET", "/api/forecast").then(function (r) {
      var m = {};
      ((r && r.agents) || []).forEach(function (f) { m[f.id] = f; });
      FC = m;
    }).catch(soft("forecast", true));
  }

  // ---------- approvals ----------
  var PEND = [], pendSig = null, lastCount = -1;
  var FLAGS = {
    new_merchant: ["New merchant", "warn", "?"],
    possible_injection: ["Possible prompt injection", "bad", "!"],
    duplicate: ["Looks like a duplicate", "warn", "="],
    unusual_amount: ["Unusual amount", "warn", "~"]
  };
  function flagChip(f) {
    var m = FLAGS[f] || [String(f).replace(/_/g, " "), "warn", "!"];
    return '<li class="chip ' + m[1] + '"><span class="chip-ic" aria-hidden="true">' + esc(m[2]) + "</span>" + esc(m[0]) + "</li>";
  }
  function flagList(flags) {
    return flags && flags.length ? '<ul class="chips" aria-label="Flags">' + flags.map(flagChip).join("") + "</ul>" : "";
  }
  function reasonList(reasons) {
    return reasons && reasons.length ? '<ul class="reasons">' + reasons.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>" : "";
  }

  // The contract nests payout info; the store row has it flat. Accept both.
  function payoutOf(x) {
    var p = x.payout || {};
    return { simulated: !!(p.simulated || x.simulated), status: p.status || x.payout_status || "" };
  }
  function loadPending() {
    if (!FEAT.spend) return Promise.resolve();
    return api("GET", "/api/spend?status=pending&limit=100").then(function (r) {
      if (r) renderPending(r.requests || []);
    }).catch(soft("spend", true));
  }

  function renderPending(list) {
    PEND = list;
    var n = list.length;
    var badge = $("appr-badge");
    badge.hidden = !n;
    badge.innerHTML = n ? esc(n) + '<span class="sr"> waiting</span>' : "";
    if (n !== lastCount) {
      lastCount = n;
      $("appr-banner").hidden = !n;
      $("appr-banner-text").innerHTML = n
        ? "<strong>" + esc(plural(n, "purchase is", "purchases are")) + " waiting for your approval.</strong> " +
          esc(list[0].agent_name || "An agent") + " wants to pay " + esc(money(list[0].amount)) + " to " + esc(list[0].merchant || "a merchant") + (n > 1 ? ", and more." : ".")
        : "";
    }
    var sig = list.map(function (x) { return x.id + ":" + x.status; }).join(",");
    if (sig === pendSig) return;
    pendSig = sig;
    var box = $("appr-list");
    var focusId = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.spend : null;
    var focusAct = focusId ? document.activeElement.dataset.do : null;
    if (!n) {
      box.innerHTML = '<div class="appr-empty"><span class="ok-ic" aria-hidden="true">&#10003;</span><div><strong>Nothing is waiting.</strong><br><span class="muted">New requests show up here within a few seconds.</span></div></div>';
      return;
    }
    box.innerHTML = list.map(function (x) {
      var inj = (x.flags || []).indexOf("possible_injection") >= 0;
      return '<article class="appr' + (inj ? " risky" : "") + '" aria-labelledby="ap-h-' + esc(x.id) + '">' +
        '<div class="appr-main">' +
          '<p class="appr-who"><strong>' + esc(x.agent_name || "Agent") + "</strong> wants to buy" + (x.ts ? ' <time class="muted">' + esc(when(x.ts)) + "</time>" : "") + "</p>" +
          '<h3 class="appr-amt" id="ap-h-' + esc(x.id) + '">' + esc(money(x.amount)) + ' <span class="appr-to">to ' + esc(x.merchant || "unknown merchant") + "</span></h3>" +
          (x.item ? '<p class="appr-item">' + esc(x.item) + (x.category ? ' <span class="muted">(' + esc(x.category) + ")</span>" : "") + "</p>" : "") +
          (x.reason ? '<p class="appr-why"><span class="k">Agent\'s reason:</span> ' + esc(x.reason) + "</p>" : "") +
          flagList(x.flags) +
          (x.reasons && x.reasons.length ? '<div class="appr-reasons"><span class="k">Why Lowtides asked you:</span>' + reasonList(x.reasons) + "</div>" : "") +
        "</div>" +
        '<div class="appr-btns">' +
          '<button type="button" class="btn go big" data-spend="' + esc(x.id) + '" data-do="approve">Approve <span class="sr">' + esc(money(x.amount)) + " to " + esc(x.merchant) + "</span></button>" +
          '<button type="button" class="btn deny big" data-spend="' + esc(x.id) + '" data-do="deny">Deny <span class="sr">' + esc(money(x.amount)) + " to " + esc(x.merchant) + "</span></button>" +
        "</div></article>";
    }).join("");
    if (focusId) {
      var again = box.querySelector('button[data-spend="' + focusId + '"][data-do="' + focusAct + '"]');
      if (again) again.focus();
    }
  }

  $("appr-list").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-spend]");
    if (!b) return;
    var id = b.dataset.spend, act = b.dataset.do;
    var x = PEND.find(function (p) { return String(p.id) === id; }) || {};
    var card = b.closest(".appr");
    card.querySelectorAll("button").forEach(function (btn) { btn.disabled = true; });
    var body = act === "deny" ? { note: "Denied from the dashboard." } : {};
    api("POST", "/api/spend/" + encodeURIComponent(id) + "/" + act, body).then(function (r) {
      r = r || {};
      var amt = money(r.amount != null ? r.amount : x.amount), to = r.merchant || x.merchant || "the merchant";
      if (act === "approve") {
        var p = payoutOf(r);
        flash("Approved. " + (r.status === "paid" ? "Paid " : "Paying ") + amt + " to " + to +
          (p.simulated ? " (simulated payout: no PayPal keys set)." : " through PayPal."));
      } else {
        flash("Denied. " + amt + " to " + to + " will not be paid.");
      }
      card.classList.add("gone");
      setTimeout(function () {
        pendSig = null;
        loadPending().then(function () {
          // Keep keyboard users in the list: move focus to the next request, or the list itself.
          var list = $("appr-list"), f = list.querySelector("button[data-do='approve']");
          if (f) { f.focus(); return; }
          list.setAttribute("tabindex", "-1");
          list.focus();
        });
        loadLog();
      }, RM.matches ? 0 : 250);
    }).catch(function (e) {
      card.querySelectorAll("button").forEach(function (btn) { btn.disabled = false; });
      if (e && e.message === "Password required") return;
      // Show why it couldn't go through right on the card (for example a merchant with no PayPal email).
      var msg = card.querySelector(".appr-err");
      if (!msg) { msg = document.createElement("p"); msg.className = "appr-err"; msg.setAttribute("role", "alert"); card.querySelector(".appr-main").appendChild(msg); }
      msg.textContent = (act === "approve" ? "Couldn't approve: " : "Couldn't deny: ") + (e && e.message ? e.message : "please try again.");
    });
  });

  var STATUS = {
    paid: ["Paid", "ok", "&#10003;"], approved: ["Approved", "ok", "&#10003;"], pending: ["Waiting for you", "warn", "&#8230;"],
    denied: ["Denied", "bad", "&#10005;"], cancelled: ["Cancelled", "neutral", "&#8211;"], failed: ["Payment failed", "bad", "!"]
  };
  function decidedBy(x) {
    if (!x.decided_by) return "";
    var d = String(x.decided_by).toLowerCase();
    if (d === "rules") return "Decided by your rules.";
    if (d.indexOf("kill") >= 0 || d.indexOf("stop") >= 0) return "Ended by the kill switch.";
    if (/dashboard|person|admin|owner|human|you/.test(d)) return "Decided by you on the dashboard.";
    return "Decided by: " + x.decided_by + ".";
  }
  var DECISION = { approved: "Lowtides approved it under your rules", needs_approval: "Lowtides asked you", denied: "Lowtides denied it" };
  var logSig = null;
  function loadLog() {
    if (!FEAT.spend) return Promise.resolve();
    loadAudit();
    return api("GET", "/api/spend?status=all&limit=50").then(function (r) {
      if (!r) return;
      var list = r.requests || [];
      var sig = list.map(function (x) { return x.id + ":" + x.status; }).join(",");
      if (sig === logSig) return;
      logSig = sig;
      $("decision-log").innerHTML = list.length ? list.map(function (x) {
        var s = STATUS[x.status] || [x.status, "neutral", "&#8226;"];
        var p = payoutOf(x);
        return '<li class="dl"><div class="dl-top">' +
          '<span class="pill ' + s[1] + '"><span aria-hidden="true">' + s[2] + "</span> " + esc(s[0]) + "</span>" +
          '<span class="dl-amt">' + esc(money(x.amount)) + "</span>" +
          '<span class="dl-what"><strong>' + esc(x.agent_name || "Agent") + "</strong> to " + esc(x.merchant || "unknown") + (x.item ? ": " + esc(x.item) : "") + "</span>" +
          (x.ts ? '<time class="muted small">' + esc(when(x.ts)) + "</time>" : "") +
          "</div>" +
          '<p class="dl-dec small">' + esc(DECISION[x.decision] || "") + (DECISION[x.decision] ? ". " : "") + esc(decidedBy(x)) +
          (p.simulated ? ' <span class="tag">simulated payout</span>' : "") +
          ' <a class="dl-ev" href="/api/spend/' + encodeURIComponent(x.id) + '/evidence" download="lowtides-evidence-' + esc(x.id) + '.json">Download evidence<span class="sr"> for ' + esc(money(x.amount)) + " to " + esc(x.merchant) + "</span></a></p>" +
          flagList(x.flags) + reasonList(x.reasons) + "</li>";
      }).join("") : '<li class="dl none muted">No purchase requests yet. When an agent calls <code>POST /v1/spend</code>, the decision shows up here.</li>';
    }).catch(soft("spend", true));
  }
  function loadAudit() {
    if (!FEAT.audit) return;
    api("GET", "/api/audit/verify").then(function (r) {
      if (!r) return;
      var el = $("audit-line");
      el.hidden = false;
      if (r.ok) {
        el.className = "audit-line ok";
        el.innerHTML = '<span aria-hidden="true">&#10003;</span> Audit log verified (' + esc(plural(Number(r.count || 0), "entry", "entries")) + ")";
      } else {
        el.className = "audit-line bad";
        el.innerHTML = '<span aria-hidden="true">&#10005;</span> Audit log check failed' + (r.broken_at != null ? " at entry " + esc(r.broken_at) : "") + ". Something was changed after it was written.";
      }
    }).catch(soft("audit", true));
  }

  // ---------- kill switch ----------
  var killTarget = null, killReturn = null;
  function askKill(a, from) {
    killTarget = a;
    killReturn = from || $("stop-all");
    $("kill-h").textContent = a ? "Kill " + a.name + "?" : "Stop every agent?";
    $("kill-text").textContent = (a ? "This stops " + a.name + " right now. " : "This stops all your agents right now. ") +
      "Lowtides denies " + (a ? "its" : "their") + " waiting purchases, voids open PayPal holds and cancels payouts nobody has claimed yet. You can start " + (a ? "it" : "them") + " again later.";
    $("kill-yes").textContent = a ? "Kill it now" : "Stop all now";
    var dlg = $("kill-dlg");
    if (dlg.showModal) { dlg.showModal(); $("kill-yes").focus(); }
    else if (confirm($("kill-h").textContent + " " + $("kill-text").textContent)) doKill();
  }
  $("kill-dlg").addEventListener("close", function () { if (killReturn && document.body.contains(killReturn)) killReturn.focus(); });
  $("kill-yes").addEventListener("click", doKill);
  function killSummary(r) {
    var bits = [];
    if (r.denied) bits.push("denied " + plural(r.denied, "pending purchase", "pending purchases"));
    if (r.holds_voided) bits.push("voided " + plural(r.holds_voided, "PayPal hold", "PayPal holds"));
    if (r.payouts_cancelled) bits.push("cancelled " + plural(r.payouts_cancelled, "payout", "payouts"));
    if (!bits.length) return "Nothing else was waiting.";
    var s = bits.length > 1 ? bits.slice(0, -1).join(", ") + " and " + bits[bits.length - 1] : bits[0];
    return s.charAt(0).toUpperCase() + s.slice(1) + ".";
  }
  function doKill() {
    var a = killTarget, dlg = $("kill-dlg");
    $("kill-yes").disabled = true;
    var p = a
      ? api("POST", "/api/agents/" + a.id + "/kill").catch(function (e) {
          if (e && e.status === 404) return api("POST", "/api/agents/" + a.id + "/stop").then(function () { return { stopped: true, fallback: true }; });
          throw e;
        })
      : api("POST", "/api/stop-all");
    p.then(function (r) {
      r = r || {};
      var head = a ? "Stopped " + a.name + "." : "Every agent is stopped.";
      var probs = (r.problems || []).filter(Boolean);
      flash(head + (r.fallback ? "" : (r.denied != null || r.holds_voided != null || r.payouts_cancelled != null ? " " + killSummary(r) : "")) +
        (probs.length ? " Needs a look: " + probs.join(" ") : ""), probs.length > 0);
      if (dlg.open) dlg.close();
      pendSig = null; logSig = null;
      return refresh();
    }).catch(fail).then(function () { $("kill-yes").disabled = false; });
  }

  // ---------- rules in plain English ----------
  var rulesAgent = null, draftRules = null, rulesReturn = null;
  function moneyRule(v) { return money(Number(v)); }
  function hour(h) { h = Number(h); var ap = h >= 12 && h < 24 ? "pm" : "am"; var x = h % 12 || 12; return x + " " + ap; }
  function humanRules(r) {
    r = r || {};
    var out = [], done = {};
    function add(k, text) { done[k] = true; if (text) out.push(text); }
    if (r.daily_purchase_limit != null) add("daily_purchase_limit", "Spends at most " + moneyRule(r.daily_purchase_limit) + " a day on purchases.");
    if (r.max_per_purchase != null) add("max_per_purchase", "No single purchase over " + moneyRule(r.max_per_purchase) + ".");
    if (r.approval_over != null) add("approval_over", Number(r.approval_over) <= 0 ? "Asks you before every purchase." : "Asks you before any purchase over " + moneyRule(r.approval_over) + ".");
    if (r.allowed_categories && r.allowed_categories.length) add("allowed_categories", "Only buys: " + r.allowed_categories.join(", ") + ".");
    else done.allowed_categories = true;
    if (r.blocked_categories && r.blocked_categories.length) add("blocked_categories", "Never buys: " + r.blocked_categories.join(", ") + ".");
    else done.blocked_categories = true;
    if (r.blocked_merchants && r.blocked_merchants.length) add("blocked_merchants", "Never pays: " + r.blocked_merchants.join(", ") + ".");
    else done.blocked_merchants = true;
    if (r.new_merchant_needs_approval != null) add("new_merchant_needs_approval", r.new_merchant_needs_approval
      ? "Asks you before paying a merchant that isn't on your list."
      : "May pay merchants that aren't on your list without asking (riskier).");
    if (r.hours && r.hours.start != null) add("hours", "Only buys between " + hour(r.hours.start) + " and " + hour(r.hours.end) + ".");
    else done.hours = true;
    if (r.max_purchases_per_hour != null) add("max_purchases_per_hour", "At most " + plural(Number(r.max_purchases_per_hour), "purchase", "purchases") + " an hour.");
    Object.keys(r).forEach(function (k) {
      if (!done[k] && r[k] != null && r[k] !== "") out.push(k.replace(/_/g, " ") + ": " + (typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k]));
    });
    return out;
  }
  function ruleItems(r, plain) {
    var h = plain && plain.length ? plain : humanRules(r);
    return h.length ? h.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") : '<li class="muted">No purchase rules set yet.</li>';
  }
  function openRules(a, from) {
    rulesAgent = a; draftRules = null; rulesReturn = from || null;
    $("rules-h").textContent = "Rules for " + a.name;
    $("rules-preview").hidden = true;
    $("rules-text").value = "";
    $("rules-now").innerHTML = '<li class="muted">Loading…</li>';
    var dlg = $("rules-dlg");
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    $("rules-text").focus();
    api("GET", "/api/agents/" + a.id + "/rules").then(function (r) {
      $("rules-now").innerHTML = ruleItems((r && r.rules) || {}, r && r.plain);
    }).catch(function (e) {
      if (e && e.status === 404) {
        FEAT.rules = false; if (dlg.open) dlg.close(); applyFeatures();
        return;
      }
      $("rules-now").innerHTML = '<li class="muted">Couldn\'t load the current rules.</li>';
    });
  }
  $("rules-close").addEventListener("click", function () { $("rules-dlg").close(); });
  $("rules-dlg").addEventListener("close", function () { if (rulesReturn && document.body.contains(rulesReturn)) rulesReturn.focus(); });
  $("rules-translate").addEventListener("click", function () {
    var text = $("rules-text").value.trim();
    if (!text) { flash("Type your rules first, like: max $50 a day, ask me over $20.", true); $("rules-text").focus(); return; }
    var b = this; b.disabled = true; b.textContent = "Translating…";
    api("POST", "/api/rules/compile", { text: text }).then(function (r) {
      r = r || {};
      draftRules = r.rules || {};
      $("rules-new").innerHTML = ruleItems(draftRules, r.plain);
      var src = $("rules-source");
      if (r.source === "claude") { src.className = "rules-source claude"; src.textContent = "Translated by Claude. Claude only writes the rules: every purchase is checked by plain code, never by the AI."; }
      else { src.className = "rules-source offline"; src.textContent = "Translated by the built-in offline parser (no Claude key set). Check each line."; }
      $("rules-notes").innerHTML = (r.notes || []).map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("");
      $("rules-preview").hidden = false;
      $("rules-preview").querySelector("h3").setAttribute("tabindex", "-1");
      $("rules-preview").querySelector("h3").focus();
    }).catch(function (e) {
      if (e && e.status === 404) flash("Plain-English rules aren't available on this server yet.");
      else fail(e);
    }).then(function () { b.disabled = false; b.textContent = "Translate"; });
  });
  $("rules-save").addEventListener("click", function () {
    if (!rulesAgent || !draftRules) return;
    var b = this; b.disabled = true;
    api("POST", "/api/agents/" + rulesAgent.id + "/rules", { rules: draftRules }).then(function (r) {
      $("rules-now").innerHTML = ruleItems((r && r.rules) || draftRules, r && r.plain);
      $("rules-preview").hidden = true;
      $("rules-text").value = "";
      flash("Rules saved for " + rulesAgent.name + ".");
      $("rules-dlg").close();
    }).catch(fail).then(function () { b.disabled = false; });
  });

  // ---------- merchants ----------
  function loadMerchants() {
    if (!FEAT.merchants) return;
    api("GET", "/api/merchants").then(function (r) {
      if (!r) return;
      var list = Array.isArray(r) ? r : (r.merchants || []);
      var hasTrust = list.some(function (m) { return m.trusted != null; });
      $("merchant-list").innerHTML = list.length
        ? '<div class="table-wrap"><table class="plain mtable"><thead><tr><th>Merchant</th><th>Paid to (PayPal)</th><th>Category</th>' + (hasTrust ? "<th>Trusted</th>" : "") + "</tr></thead><tbody>" +
          list.map(function (m) {
            return '<tr><td data-k="Merchant">' + esc(m.name) + '</td><td data-k="Paid to"><code>' + esc(m.paypal_email || "not set") + '</code></td><td data-k="Category">' + esc(m.category || "any") +
              "</td>" + (hasTrust ? '<td data-k="Trusted">' + (m.trusted ? "Yes" : "No") + "</td>" : "") + "</tr>";
          }).join("") + "</tbody></table></div>"
        : '<div class="empty">No merchants yet. Until you add some, every purchase needs your approval.</div>';
    }).catch(soft("merchants", true));
  }
  $("merchant-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var name = $("mc-name").value.trim(), email = $("mc-email").value.trim(), cat = $("mc-cat").value.trim();
    if (!name) { flash("Give the merchant a name.", true); $("mc-name").focus(); return; }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { flash("Add the PayPal email that should get paid, like payments@acme.example.", true); $("mc-email").focus(); return; }
    api("POST", "/api/merchants", { name: name, paypal_email: email, category: cat }).then(function () {
      flash(name + " is on your merchant list.");
      $("mc-name").value = ""; $("mc-email").value = ""; $("mc-cat").value = "";
      loadMerchants();
    }).catch(function (e) {
      if (e && e.status === 404) { FEAT.merchants = false; applyFeatures(); return; }
      fail(e);
    });
  });

  // ---------- loop ----------
  var rulesProbed = false;
  function refresh() {
    return Promise.all([
      api("GET", "/api/state"),
      loadForecast()
    ]).then(function (res) {
      S = res[0]; render();
      if (!rulesProbed && FEAT.rules && S.agents.length) {
        rulesProbed = true;
        api("GET", "/api/agents/" + S.agents[0].id + "/rules").catch(soft("rules", true));
      }
    }).catch(fail);
  }
  showTab();
  refresh();
  loadPending();
  setInterval(function () { if (!document.hidden && currentTab() === "home") refresh(); }, 5000);
  setInterval(function () {
    if (document.hidden) return;
    loadPending();
    if (currentTab() === "approvals") loadLog();
  }, 3000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) loadPending(); });
})();
