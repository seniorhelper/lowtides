/* Lowtides Pass in the dashboard: a "Pass" button on each agent card and a small dialog
   (status, expiry, copy, open the verifier, revoke). Self-contained and additive: it adds
   its button next to the agent's other controls and never touches app.js state. */
(function () {
  "use strict";
  var box = document.getElementById("agents");
  if (!box) return;

  function api(method, path) {
    var h = { "Content-Type": "application/json" };
    try { var t = sessionStorage.getItem("lt_admin"); if (t) h["X-Admin-Token"] = t; } catch (e) { /* ignore */ }
    var opts = { method: method, headers: h, credentials: "same-origin" };
    if (method === "POST") opts.body = "{}";
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || ("Request failed (" + r.status + ")")); e.status = r.status; throw e; }
        return j;
      });
    });
  }
  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text) n.textContent = text;
    return n;
  }
  function when(iso) {
    var d = new Date(iso);
    return iso && !isNaN(d) ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "unknown";
  }

  // ---- the dialog --------------------------------------------------------------------
  var dlg = el("dialog", { id: "pass-dlg", "class": "modal", "aria-labelledby": "pass-h" });
  dlg.innerHTML =
    '<div class="modal-in">' +
    '<div class="modal-top"><h2 id="pass-h">Lowtides Pass</h2><button type="button" class="btn ghost sm" id="pass-close">Close</button></div>' +
    '<p class="muted small">A signed spending permission this agent shows merchants and other agents. They can check it, and Stop all revokes it instantly.</p>' +
    '<p id="pass-status" role="status" aria-live="polite"></p>' +
    '<p id="pass-exp" class="muted small"></p>' +
    '<div class="modal-actions">' +
    '<button type="button" class="btn go" id="pass-copy">Copy pass</button>' +
    '<a class="btn ghost" id="pass-open" href="/pass/" target="_blank" rel="noopener">Open verifier</a>' +
    '<button type="button" class="btn stop" id="pass-revoke">Revoke pass</button>' +
    '<button type="button" class="btn ghost" id="pass-issue" hidden>Issue a new pass</button>' +
    "</div></div>";
  document.body.appendChild(dlg);
  var $ = function (id) { return document.getElementById(id); };
  var agentId = null, token = "", back = null;

  function show(r) {
    var p = r && r.pass;
    token = (p && p.token) || "";
    var st = $("pass-status");
    if (!p) {
      st.textContent = "No pass yet. This agent gets one when it is running.";
      $("pass-exp").textContent = "";
    } else if (p.valid) {
      st.textContent = "VALID. Merchants checking it see a green Valid.";
    } else if (p.revoked) {
      st.textContent = "REVOKED. " + (p.revoked_reason || "");
    } else {
      st.textContent = "EXPIRED. The agent gets a fresh one next time it asks.";
    }
    st.style.fontWeight = "700";
    st.style.color = p && p.valid ? "var(--sign-2, #0B6656)" : "var(--stop-ink, #B3261E)";
    if (p) $("pass-exp").textContent = "Issued " + when(p.issued_at) + ". Expires " + when(p.expires_at) + ". Pass ID " + (p.jti || "") + ".";
    $("pass-copy").disabled = !token;
    $("pass-open").href = "/pass/" + (token ? "#" + token : "");
    $("pass-revoke").hidden = !(p && p.valid);
    $("pass-issue").hidden = !(p && !p.valid && r.agent_status === "on" && p.owner_revoked);
  }
  function load(method, path) {
    $("pass-status").textContent = "Loading…";
    return api(method, path).then(show).catch(function (e) {
      $("pass-status").textContent = e.message || "Couldn't load the pass.";
    });
  }
  function open(id, name, from) {
    agentId = id; back = from;
    $("pass-h").textContent = "Lowtides Pass for " + name;
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    $("pass-close").focus();
    load("GET", "/api/agents/" + id + "/pass");
  }
  $("pass-close").addEventListener("click", function () { dlg.close ? dlg.close() : dlg.removeAttribute("open"); });
  dlg.addEventListener("close", function () { if (back && document.body.contains(back)) back.focus(); });
  $("pass-copy").addEventListener("click", function () {
    if (!token) return;
    var done = function () { $("pass-copy").textContent = "Copied"; setTimeout(function () { $("pass-copy").textContent = "Copy pass"; }, 1500); };
    var byHand = function () { window.LT.copy({ title: "Copy this pass", label: "Lowtides Pass", text: token }); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(token).then(done, byHand);
    else byHand();
  });
  $("pass-revoke").addEventListener("click", function () {
    window.LT.confirm({ tone: "stop", title: "Revoke this agent's pass?",
      body: "Merchants and agents checking it will see REVOKED. The agent itself keeps running.",
      ok: "Revoke pass", cancel: "Keep it valid" }).then(function (yes) {
      if (yes) load("POST", "/api/agents/" + agentId + "/pass/revoke");
    });
  });
  $("pass-issue").addEventListener("click", function () { load("POST", "/api/agents/" + agentId + "/pass"); });

  // ---- the button on each agent card ----------------------------------------------------
  function addButtons() {
    box.querySelectorAll(".agent-actions").forEach(function (row) {
      if (row.querySelector("[data-pass]")) return;
      var any = row.querySelector("button[data-id]");
      if (!any) return;
      var card = row.closest(".agent");
      var nameEl = card && card.querySelector(".agent-name");
      var name = nameEl ? (nameEl.firstChild && nameEl.firstChild.nodeType === 3 ? nameEl.firstChild.nodeValue : nameEl.textContent) : "this agent";
      var b = el("button", { type: "button", "class": "btn ghost sm", "data-pass": any.getAttribute("data-id"),
                             "aria-label": "Lowtides Pass for " + name }, "Pass");
      var after = row.querySelector('[data-act="rules"]') || row.querySelector('[data-act="kill"]') || any;
      after.insertAdjacentElement("afterend", b);
    });
  }
  box.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-pass]");
    if (!b) return;
    open(Number(b.getAttribute("data-pass")), (b.getAttribute("aria-label") || "").replace("Lowtides Pass for ", ""), b);
  });
  new MutationObserver(addButtons).observe(box, { childList: true, subtree: true });
  addButtons();
})();
