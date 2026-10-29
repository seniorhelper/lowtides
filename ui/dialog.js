/* Lowtides in-app dialogs: replaces the browser's confirm() and prompt() with a modal in
   the app's own style. Accessible: a real modal <dialog> (the page behind is inert), Tab
   stays inside it, Esc closes it, and focus goes back to whatever opened it.

     LT.ask({ title, body, actions: [{ id, label, cls, focus }], tone })  -> Promise<id | null>
     LT.confirm({ title, body, ok, cancel, tone })                         -> Promise<boolean>
     LT.form({ title, body, ok, fields: [{ id, label, type, value, ... }], check }) -> Promise<values | null>
     LT.copy({ title, text })                                              -> Promise<void>
   Esc, the backdrop's Close button and every non-ok action resolve to null / false. */
(function () {
  "use strict";
  var LT = window.LT = window.LT || {};
  var seq = 0;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function focusables(root) {
    return Array.prototype.filter.call(
      root.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"),
      function (x) { return !x.disabled && !x.hidden && x.offsetParent !== null; });
  }

  // Core: build a dialog, show it, resolve with what the person picked.
  function open(opts, fill) {
    return new Promise(function (resolve) {
      var back = document.activeElement;
      var id = "lt-dlg-" + (++seq);
      var d = el("dialog", "modal lt-dlg" + (opts.tone ? " tone-" + opts.tone : ""));
      d.setAttribute("aria-labelledby", id + "-h");
      var inner = el("form", "modal-in");
      inner.method = "dialog";
      inner.noValidate = true;
      var h = el("h2", "lt-dlg-h", opts.title || "Are you sure?");
      h.id = id + "-h";
      inner.appendChild(h);
      if (opts.body) {
        var p = el("p", "lt-dlg-body", opts.body);
        p.id = id + "-d";
        d.setAttribute("aria-describedby", p.id);
        inner.appendChild(p);
      }
      var done = false, result = null;
      function finish(v) {
        if (done) return;
        done = true; result = v;
        if (d.open && d.close) d.close(); else onClosed();
      }
      function onClosed() {
        d.remove();
        if (back && back.focus && document.body.contains(back)) back.focus();
        resolve(result);
      }
      var api = { dialog: d, form: inner, finish: finish, id: id };
      var first = fill(api) || null;
      d.appendChild(inner);
      document.body.appendChild(d);

      d.addEventListener("close", function () { done = true; onClosed(); });
      d.addEventListener("cancel", function (ev) { ev.preventDefault(); finish(null); });  // Esc
      d.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape") { ev.preventDefault(); finish(null); return; }
        if (ev.key !== "Tab") return;
        var f = focusables(d);
        if (!f.length) return;
        var a = f[0], z = f[f.length - 1];
        if (ev.shiftKey && document.activeElement === a) { ev.preventDefault(); z.focus(); }
        else if (!ev.shiftKey && document.activeElement === z) { ev.preventDefault(); a.focus(); }
      });
      inner.addEventListener("submit", function (ev) { ev.preventDefault(); if (api.submit) api.submit(); });

      if (d.showModal) d.showModal(); else { d.setAttribute("open", ""); d.classList.add("lt-dlg-fallback"); }
      (first || focusables(d)[0] || h).focus();
    });
  }

  LT.ask = function (opts) {
    return open(opts, function (api) {
      var row = el("div", "modal-actions");
      var focus = null;
      (opts.actions || []).forEach(function (a) {
        var b = el("button", "btn big " + (a.cls || "ghost"), a.label);
        b.type = "button";
        b.dataset.choice = a.id;
        b.addEventListener("click", function () { api.finish(a.id); });
        row.appendChild(b);
        if (a.focus) focus = b;
      });
      api.form.appendChild(row);
      return focus;
    });
  };

  LT.confirm = function (opts) {
    var okFirst = opts.primary !== "cancel";
    var ok = { id: "ok", label: opts.ok || "OK", cls: opts.okCls || (opts.tone === "stop" ? "stop" : "go"), focus: okFirst };
    var no = { id: "cancel", label: opts.cancel || "Cancel", cls: opts.cancelCls || "ghost", focus: !okFirst };
    return LT.ask({ title: opts.title, body: opts.body, tone: opts.tone,
                    actions: okFirst ? [ok, no] : [no, ok] }).then(function (v) { return v === "ok"; });
  };

  LT.form = function (opts) {
    return open(opts, function (api) {
      var inputs = {};
      var grid = el("div", "lt-dlg-fields");
      (opts.fields || []).forEach(function (f) {
        var lab = el("label", null, f.label);
        var inp = el(f.type === "textarea" ? "textarea" : "input");
        if (f.type !== "textarea") inp.type = f.type || "text";
        inp.id = api.id + "-" + f.id;
        inp.name = f.id;
        if (f.value != null) inp.value = f.value;
        ["min", "max", "step", "placeholder", "inputmode", "autocomplete"].forEach(function (k) { if (f[k] != null) inp.setAttribute(k, f[k]); });
        if (f.readonly) inp.readOnly = true;
        lab.htmlFor = inp.id;
        lab.appendChild(inp);
        grid.appendChild(lab);
        inputs[f.id] = inp;
      });
      api.form.appendChild(grid);
      var err = el("p", "lt-dlg-err");
      err.setAttribute("role", "alert");
      err.hidden = true;
      api.form.appendChild(err);
      var row = el("div", "modal-actions");
      var okb = null;
      if (opts.ok !== false) {
        okb = el("button", "btn big " + (opts.okCls || "go"), opts.ok || "Save");
        okb.type = "submit";
        row.appendChild(okb);
      }
      var no = el("button", "btn big ghost", opts.cancel || "Cancel");
      no.type = "button";
      no.addEventListener("click", function () { api.finish(null); });
      row.appendChild(no);
      api.form.appendChild(row);
      api.submit = function () {
        if (!okb) return;
        var vals = {};
        Object.keys(inputs).forEach(function (k) { vals[k] = inputs[k].value; });
        var problem = opts.check ? opts.check(vals) : null;
        if (problem) {
          err.textContent = problem.message || problem;
          err.hidden = false;
          var bad = inputs[problem.field] || inputs[Object.keys(inputs)[0]];
          if (bad) { bad.setAttribute("aria-invalid", "true"); bad.focus(); }
          return;
        }
        api.finish(vals);
      };
      var firstInput = inputs[(opts.fields[0] || {}).id];
      if (firstInput && firstInput.readOnly) setTimeout(function () { firstInput.select(); }, 0);
      return firstInput || okb;
    });
  };

  // Clipboard fallback: show the text selected, ready to copy by hand.
  LT.copy = function (opts) {
    return LT.form({ title: opts.title || "Copy this", body: opts.body || "Your browser blocked automatic copying. The text is selected: press Ctrl+C (or Cmd+C).",
                     ok: false, cancel: "Done",
                     fields: [{ id: "text", label: opts.label || "Text", type: "textarea", value: opts.text, readonly: true }] })
      .then(function () {});
  };
})();
