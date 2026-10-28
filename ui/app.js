/* CostCop dashboard. Vanilla JS, no build step. */
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
    var tok = sessionStorage.getItem("cc_admin");
    if (tok) opts.headers["X-Admin-Token"] = tok;
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(function (r) {
      if (r.status === 401 && path.indexOf("/api/") === 0) { showLogin(); throw new Error("Password required"); }
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("Request failed (" + r.status + ")"));
        return j;
      });
    });
  }
  function fail(e) { if (e && e.message !== "Password required") flash(e.message || String(e), true); }

  // ---------- login ----------
  function showLogin() { $("login").hidden = false; $("login-pw").focus(); }
  $("login-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var pw = $("login-pw").value;
    fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: pw }) })
      .then(function (r) {
        if (!r.ok) { $("login-err").textContent = "That password didn't work."; return; }
        sessionStorage.setItem("cc_admin", pw);
        $("login").hidden = true;
        refresh();
      });
  });

  // ---------- tabs ----------
  function currentTab() {
    var h = (location.hash || "#home").slice(1).split("?")[0];
    return ["home", "tasks", "wallet", "clients", "ledger", "settings"].indexOf(h) >= 0 ? h : "home";
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
    if (!confirm("Stop every agent right now? They will refuse all calls until you press Start.")) return;
    api("POST", "/api/stop-all").then(function () { flash("Every agent is stopped."); refresh(); }).catch(fail);
  });

  // ---------- render ----------
  function render() {
    if (!S) return;
    document.title = S.app;
    $("app-name").textContent = S.app;
    document.body.classList.toggle("biz", S.mode === "business");
    document.querySelectorAll(".mode-btn").forEach(function (b) {
      var on = b.dataset.mode === S.mode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var t = S.today.totals;
    $("saved-today").textContent = money(t.saved);
    $("saved-sub").textContent = "Would have cost " + money(t.baseline) + ". You paid " + money(t.cost) + "." +
      (t.baseline > 0 ? " That's " + S.today.percent_saved + "% less." : "");
    var lines = t.calls ? S.today.lines : ["No calls yet today. Connect an agent or press Watch it work."];
    $("receipt-lines").innerHTML = lines.map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("");
    var w = S.week.totals;
    $("week-line").textContent = w.calls ? ("Last 7 days: " + w.calls + " calls, saved " + money(w.saved) + " (" + S.week.percent_saved + "%).") : "";
    var sim = S.agents.some(function (a) { return a.simulated; });
    $("foot-line").textContent = S.app + " " + S.version + "." + (sim ? " Rows from the demo agent use a simulated model and are labeled demo." : "") +
      (S.admin_protected ? "" : " This dashboard has no password yet. Set COSTCOP_ADMIN_TOKEN before putting it online.");
    $("heart-line").hidden = !S.config.show_signature_heart;

    $("setup").hidden = S.agents.length > 0;
    renderAgents();
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
      var status = a.status === "on" ? "On patrol" : (a.status === "paused" ? "Paused by the loop cop" : "Stopped");
      var btn = a.status === "on"
        ? '<button class="btn stop sm" data-act="stop" data-id="' + a.id + '">STOP</button>'
        : (a.status === "paused"
          ? '<button class="btn warn sm" data-act="resume" data-id="' + a.id + '">Resume</button>'
          : '<button class="btn go sm" data-act="start" data-id="' + a.id + '">Start</button>');
      var wallet = a.use_wallet ? " Wallet " + money(a.balance) + "." : "";
      return '<div class="agent">' +
        '<div class="light ' + a.light + '" role="img" aria-label="' + esc(a.light) + ' light"><i></i><i></i><i></i></div>' +
        '<div><div class="agent-name">' + esc(a.name) + (a.simulated ? '<span class="tag">demo</span>' : "") + '</div>' +
        '<div class="agent-meta">' + esc(status) + ". Spent " + money4(a.spent_today) + " of " + money(a.daily_budget) + " today." + esc(wallet) + "</div>" +
        '<div class="bar ' + cls + '"><b style="width:' + pct.toFixed(1) + '%"></b></div></div>' +
        '<div class="agent-actions">' + btn +
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
      .then(function (r) { lastAgent = r.agent; flash(name + " is on patrol."); return refresh().then(showConnect); })
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
    var key = lastAgent ? lastAgent.key : "YOUR_COSTCOP_KEY";
    var o = location.origin;
    var c = {
      "py-openai": 'from openai import OpenAI\n\nclient = OpenAI(base_url="' + o + '/v1", api_key="' + key + '")\nreply = client.chat.completions.create(\n    model="sim-large",  # or any model on your Models list\n    messages=[{"role": "user", "content": "Hello!"}],\n    extra_headers={"X-CostCop-Task": "my-job-1"},  # optional: group calls into a job\n)\nprint(reply.choices[0].message.content)',
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
      flash("Demo done: " + r.results.length + " calls ran through the real CostCop pipeline on a simulated model.");
      return refresh();
    }).catch(fail).then(function () { b.disabled = false; b.textContent = "Watch it work (demo)"; });
  });
  $("run-loop").addEventListener("click", function () {
    api("POST", "/api/demo", { loop: true }).then(function (r) {
      flash(r.agent_status === "paused" ? "The loop cop paused the demo agent after it repeated itself. Press Resume to let it run again."
                                        : "The demo agent didn't trip the loop cop this time.");
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
        out.innerHTML = '<div class="meta">Answered by ' + esc(h["X-CostCop-Model-Used"]) + ". Cost " + esc(money4(Number(h["X-CostCop-Cost"]))) +
          ". Saved " + esc(money4(Number(h["X-CostCop-Saved"]))) + ".</div><div>" + esc(r.text) + "</div>";
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
      (S.paypal_ready ? "" : '<p class="muted small">PayPal isn\'t connected yet. Add your sandbox keys (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET) and restart CostCop.</p>');
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
    ["loop_max_repeats", "num", "Loop cop: repeats before pausing", "Same request this many times in the window pauses the agent. 0 turns it off."],
    ["loop_window_seconds", "num", "Loop cop window (seconds)", "How far back the loop cop looks."],
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
      .then(function () { flash("Connection saved. Add its models below so CostCop can price them."); return refresh(); }).catch(fail);
  });
  $("mem-clear").addEventListener("click", function () {
    api("POST", "/api/memory/clear", {}).then(function () { flash("All remembered answers are forgotten."); }).catch(fail);
  });

  // ---------- loop ----------
  function refresh() {
    return api("GET", "/api/state").then(function (s) { S = s; render(); }).catch(fail);
  }
  showTab();
  refresh();
  setInterval(function () { if (!document.hidden && currentTab() === "home") refresh(); }, 5000);
})();
