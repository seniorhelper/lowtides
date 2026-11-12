/* Small helpers for Lowtides pages. Safe to include on any page that uses /brand.css:
   - every [data-lt-slider] gets a live <output> value and a filled track (--lt-val)
   - the landing page's preview Stop all button (#kill-demo) toggles a pressed state */
(function () {
  "use strict";

  function bindSlider(wrap) {
    var input = wrap.querySelector('input[type="range"]');
    var out = wrap.querySelector("output");
    if (!input) return;
    var prefix = wrap.getAttribute("data-prefix") || "";
    var suffix = wrap.getAttribute("data-suffix") || "";
    function update() {
      var min = parseFloat(input.min || "0"), max = parseFloat(input.max || "100"), v = parseFloat(input.value);
      var pct = max > min ? ((v - min) / (max - min)) * 100 : 0;
      input.style.setProperty("--lt-val", pct + "%");
      if (out) out.textContent = prefix + input.value + suffix;
    }
    input.addEventListener("input", update);
    update();
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-lt-slider]"), bindSlider);

    var kill = document.getElementById("kill-demo");
    var msg = document.getElementById("kill-msg");
    if (kill && msg) {
      kill.addEventListener("click", function () {
        var on = kill.getAttribute("aria-pressed") !== "true";
        kill.setAttribute("aria-pressed", on ? "true" : "false");
        msg.textContent = on
          ? "In the demo, this revokes your Pass at every store at once. Press again to reset this preview."
          : "Go on, press it. This one is just a preview.";
      });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

/* Both sides: five numbered spots on the home page's picture of the two control panels. Sample data only. */
(function () {
  var box = document.getElementById("both-sides");
  if (!box) return;
  var STEPS = [
    ["Here you set your limits in plain English. Your AI shopper carries them in a signed Pass.", "/shop/#rules", "Open the shopper page"],
    ["Here you pick how much freedom it gets. Off buys nothing, AI Assist asks you first, Full Autopilot pays a green match by itself.", "/shop/", "Open the shopper page"],
    ["Here the store sets its rules for AI shoppers: whether it takes AI orders, how large, and how much trust it needs.", "/merchants/", "Open the store page"],
    ["Here both say yes. The Handshake checks your limits against the store's rules. Green pays, yellow waits for you, red moves no money.", "/handshake/?tour=1", "Watch a live Handshake"],
    ["Stop all: revokes Scout's Pass at every store at once. You can turn it back on when you're ready.", "/shop/", "Open the shopper page"],
    ["Action Muzzle: it is not just shopping. Tell your AI the job, like booking a table, and it may do only that. Anything extra, it asks you first. Say your stop word and everything freezes.", "/shop/#muzzle", "Try Action Muzzle"]
  ];
  var spots = [].slice.call(box.querySelectorAll(".bs-spot")), cur = 1;
  var step = document.getElementById("bs-step"), text = document.getElementById("bs-text"), open = document.getElementById("bs-open"),
      back = document.getElementById("bs-back"), next = document.getElementById("bs-next");
  function show(n, focus) {
    cur = Math.max(1, Math.min(STEPS.length, n));
    spots.forEach(function (b) {
      var on = Number(b.getAttribute("data-spot")) === cur;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      if (on && focus) b.focus();
    });
    [].forEach.call(box.querySelectorAll("[data-for]"), function (r) { r.classList.toggle("is-on", Number(r.getAttribute("data-for")) === cur); });
    var s = STEPS[cur - 1];
    step.textContent = "Step " + cur + " of " + STEPS.length;
    text.textContent = s[0];
    open.href = s[1];
    open.textContent = s[2];
    back.disabled = cur === 1;
    next.textContent = cur === STEPS.length ? "Start over" : "Next";
  }
  spots.forEach(function (b) { b.addEventListener("click", function () { show(Number(b.getAttribute("data-spot"))); }); });
  back.addEventListener("click", function () { show(cur - 1); });
  next.addEventListener("click", function () { show(cur === STEPS.length ? 1 : cur + 1); });
  show(1);
})();
