/* Settings > "Alerts to Zapier / webhooks". Vanilla JS, separate from app.js on purpose.
   Lists, adds, tests and deletes outbound webhooks through /api/webhooks. */
(function () {
  "use strict";
  var box = document.getElementById("hooks-panel");
  if (!box) return;
  var $ = function (id) { return document.getElementById(id); };
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function api(method, path, body) {
    var opts = { method: method, headers: { "Content-Type": "application/json" }, credentials: "same-origin" };
    var tok = null;
    try { tok = sessionStorage.getItem("lt_admin"); } catch (e) { tok = null; }
    if (tok) opts.headers["X-Admin-Token"] = tok;
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || ("Request failed (" + r.status + ")")); e.status = r.status; throw e; }
        return j;
      });
    });
  }
  function say(msg, bad) {
    var el = $("hk-msg");
    el.textContent = msg || "";
    el.className = "small " + (bad ? "bad" : "muted");
  }
  function when(ts) { return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }); }

  function render(d) {
    var hooks = d.webhooks || [];
    $("hk-list").innerHTML = hooks.length
      ? '<div class="table-wrap"><table class="plain mtable"><thead><tr><th>Sends to</th><th>Events</th><th></th></tr></thead><tbody>' +
        hooks.map(function (h) {
          return '<tr><td data-k="Sends to">' + (h.label ? esc(h.label) + "<br>" : "") + '<code class="small" style="word-break:break-all">' + esc(h.url) +
            '</code></td><td data-k="Events">' + esc(h.events.length ? h.events.join(", ") : "all events") +
            '</td><td data-k="Actions"><button type="button" class="btn ghost" data-test="' + h.id + '">Test</button> ' +
            '<button type="button" class="btn ghost" data-del="' + h.id + '">Delete</button></td></tr>';
        }).join("") + "</tbody></table></div>"
      : '<div class="empty">No webhooks yet. Add a Zapier Catch Hook URL to get a text or email when a purchase needs you.</div>';
    var log = (d.deliveries || []).slice(0, 8);
    $("hk-log").innerHTML = log.length
      ? "<h3>Recent deliveries</h3><ul class=\"small muted\">" + log.map(function (x) {
          return "<li>" + esc(when(x.ts)) + " &middot; " + esc(x.event) + " &middot; try " + x.attempt + " &middot; " +
            (x.ok ? "delivered (" + x.status + ")" : "failed: " + esc(x.error || x.status)) + "</li>";
        }).join("") + "</ul>"
      : "";
  }
  function load() {
    return api("GET", "/api/webhooks").then(render).catch(function (e) {
      if (e.status === 404) { box.hidden = true; return; }
      if (e.status !== 401) say(e.message, true);
    });
  }

  $("hk-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var url = $("hk-url").value.trim();
    if (!url) { say("Paste your Zapier Catch Hook URL first.", true); $("hk-url").focus(); return; }
    var events = $("hk-approvals").checked ? ["spend.needs_approval", "agent.killed", "stop_all", "loop_guard.paused"] : [];
    api("POST", "/api/webhooks", { url: url, label: $("hk-label").value.trim(), events: events }).then(function (r) {
      $("hk-url").value = ""; $("hk-label").value = "";
      $("hk-secret").hidden = false;
      $("hk-secret-val").textContent = r.secret;
      say("Saved. Press Test to send a sample event.");
      return load();
    }).catch(function (e) { say(e.message, true); });
  });
  $("hk-list").addEventListener("click", function (ev) {
    var t = ev.target;
    if (t.getAttribute("data-test")) {
      say("Sending a test event...");
      api("POST", "/api/webhooks/" + t.getAttribute("data-test") + "/test", {}).then(function (r) {
        say(r.ok ? "Delivered (HTTP " + r.status + "). Check your Zap." : "Not delivered: " + (r.error || r.status), !r.ok);
        return load();
      }).catch(function (e) { say(e.message, true); });
    } else if (t.getAttribute("data-del")) {
      api("DELETE", "/api/webhooks/" + t.getAttribute("data-del")).then(function () { say("Deleted."); return load(); })
        .catch(function (e) { say(e.message, true); });
    }
  });
  window.addEventListener("hashchange", function () { if ((location.hash || "").indexOf("#settings") === 0) load(); });
  load();
})();
