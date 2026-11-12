/* Sign in (optional). Loaded by ask.js on every page. Does nothing unless the server says accounts are on
   (GET /api/account -> enabled). The password goes only to POST /api/account/signin or /signup on this site. */
(function () {
  "use strict";
  var doc = document, state = null, dlg = null, shown = 0, f = {};
  function el(tag, cls, text) { var n = doc.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function call(method, path, body) {
    return fetch(path, { method: method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.error || "That didn't work. Please try again."); return j; }); });
  }
  function bar() {
    var b = doc.querySelector(".lta-talkbar-in");
    if (b) return b;
    var hd = doc.querySelector(".lt-header");
    if (!hd) return null;
    var wrap = el("div", "lta-talkbar"); b = el("div", "lta-talkbar-in");
    wrap.appendChild(b); hd.parentNode.insertBefore(wrap, hd);
    return b;
  }
  function draw() {
    var b = bar();
    if (!b || !state || !state.enabled) return;
    var old = b.querySelector(".lta-acct");
    if (old) old.remove();
    var box = el("span", "lta-acct");
    if (state.signed_in) {
      box.appendChild(el("span", "lta-acct-who", (state.role === "store" ? "Store: " : "Shopper: ") + (state.email || "signed in")));
      var out = el("button", "lta-talk lta-talk-human", "Sign out");
      out.type = "button";
      out.addEventListener("click", function () {
        out.disabled = true;
        call("POST", "/api/account/signout", {}).then(function () { location.reload(); }, function () { out.disabled = false; });
      });
      box.appendChild(out);
    } else {
      var inb = el("button", "lta-talk lta-talk-human", "Sign in");
      inb.type = "button";
      inb.addEventListener("click", open);
      box.appendChild(inb);
    }
    b.appendChild(box);
  }
  function build() {
    dlg = el("dialog", "lta-acct-dlg");
    dlg.setAttribute("aria-labelledby", "lta-acct-h");
    var form = el("form", "lta-acct-form");
    form.noValidate = true;
    var h = el("h2", "lta-acct-h", "Sign in to Lowtides"); h.id = "lta-acct-h";
    var p = el("p", "lta-acct-p", "Keep your own dashboard: your rules, your receipts and your store settings are saved to your account. The demo still works without signing in.");
    var role = el("fieldset", "lta-acct-role");
    role.appendChild(el("legend", null, "I am a"));
    [["shopper", "Shopper"], ["store", "Store"]].forEach(function (r, i) {
      var l = el("label"), inp = el("input");
      inp.type = "radio"; inp.name = "lta-role"; inp.value = r[0]; inp.checked = i === 0;
      l.appendChild(inp); l.appendChild(doc.createTextNode(" " + r[1]));
      role.appendChild(l);
    });
    var l1 = el("label", "lta-acct-l", "Email"); l1.setAttribute("for", "lta-acct-email");
    f.email = el("input", "lta-input"); f.email.type = "email"; f.email.id = "lta-acct-email"; f.email.autocomplete = "email"; f.email.required = true; f.email.maxLength = 200;
    var l2 = el("label", "lta-acct-l", "Password (8 characters or more)"); l2.setAttribute("for", "lta-acct-pw");
    f.pw = el("input", "lta-input"); f.pw.type = "password"; f.pw.id = "lta-acct-pw"; f.pw.autocomplete = "current-password"; f.pw.required = true; f.pw.minLength = 8; f.pw.maxLength = 200;
    f.honey = el("input", "lta-honey"); f.honey.type = "text"; f.honey.name = "_honey"; f.honey.tabIndex = -1; f.honey.autocomplete = "off"; f.honey.setAttribute("aria-hidden", "true");
    f.said = el("p", "lta-acct-said"); f.said.setAttribute("role", "status");
    var row = el("div", "lta-acct-row");
    f.go = el("button", "lta-btn lta-btn-go", "Sign in"); f.go.type = "submit";
    f.make = el("button", "lta-btn", "Create account"); f.make.type = "button";
    var no = el("button", "lta-btn", "Cancel"); no.type = "button";
    row.appendChild(f.go); row.appendChild(f.make); row.appendChild(no);
    var fine = el("p", "lta-acct-fine", "Your password is checked by our sign-in provider and is never stored by Lowtides. Never type a card number here. ");
    var pl = el("a", null, "Privacy"); pl.href = "/privacy/"; fine.appendChild(pl);
    [h, p, role, l1, f.email, l2, f.pw, f.honey, f.said, row, fine].forEach(function (n) { form.appendChild(n); });
    dlg.appendChild(form);
    doc.body.appendChild(dlg);
    function send(path) {
      if (f.honey.value) return;
      if (Date.now() - shown < 3000) { f.said.textContent = "One moment, then press it again."; return; }
      if (!f.email.value.trim() || f.pw.value.length < 8) { f.said.textContent = "Type your email and a password of at least 8 characters."; return; }
      var r = dlg.querySelector('input[name="lta-role"]:checked');
      f.go.disabled = f.make.disabled = true;
      f.said.textContent = "Working...";
      call("POST", path, { email: f.email.value.trim(), password: f.pw.value, role: r ? r.value : "shopper" }).then(function (out) {
        f.pw.value = "";
        if (out.signed_in) { location.href = out.go || "/shop/"; return; }
        f.said.textContent = out.said || "Check your email, then sign in.";
      }, function (e) { f.said.textContent = e.message; }).then(function () { f.go.disabled = f.make.disabled = false; });
    }
    form.addEventListener("submit", function (ev) { ev.preventDefault(); send("/api/account/signin"); });
    f.make.addEventListener("click", function () { send("/api/account/signup"); });
    no.addEventListener("click", function () { dlg.close(); });
  }
  function open() {
    if (!dlg) build();
    f.said.textContent = "";
    shown = Date.now();
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    f.email.focus();
  }
  function start() {
    call("GET", "/api/account").then(function (s) { state = s; draw(); }, function () { /* accounts off or unreachable: the page works as before */ });
  }
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start); else start();
  window.LowtidesAccount = { open: open };
})();
