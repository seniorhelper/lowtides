/* Ask Lowtides: a floating guide on every page. Zero markup needed: include
     <link rel="stylesheet" href="/ask.css"> and <script src="/ask.js" defer></script>
   and it mounts its own button + panel. It only ever asks the server (POST /api/ask) and shows
   suggestions as cards; nothing changes until the person taps Confirm (POST /api/ask/confirm).
   API:  window.LowtidesAsk.open("question?")  .close()  .toggle()
   Any element with data-ask="Why was it held?" opens the panel with that question when clicked. */
(function () {
  "use strict";
  if (window.LowtidesAsk) return;

  var doc = document;
  var root, fab, panel, log, form, input, sendBtn, chipsBox, micBtn, micLabel, voiceBtn, vrow, vsel, vlabel, stopBtn, statusEl;
  var open = false, busy = false, lastFocus = null, voiceOn = false, rec = null, listening = false;

  // ---- tiny helpers -------------------------------------------------------------------
  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function store(method, key, val) {
    try {
      if (method === "get") return window.localStorage.getItem(key);
      window.localStorage.setItem(key, val);
    } catch (e) { /* private mode or blocked: carry on without remembering */ }
    return null;
  }
  function api(path, body) {
    var h = { "Content-Type": "application/json" };
    try { var t = window.sessionStorage.getItem("lt_admin"); if (t) h["X-Admin-Token"] = t; } catch (e) { /* ignore */ }
    return fetch(path, { method: "POST", headers: h, credentials: "same-origin", body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) {
            var msg = j.error || (r.status === 429 ? "That's a lot of questions at once. Wait a few seconds." : "Something went wrong (" + r.status + ").");
            var e = new Error(msg); e.status = r.status; throw e;
          }
          return j;
        });
      });
  }

  // ---- suggested questions depend on the page -----------------------------------------
  function chipsFor(path) {
    if (/^\/shop/.test(path)) return ["What should I try first?", "Is this store safe?", "What did I spend today?", "Show held purchases", "Set my daily cap to $150", "Why was something blocked?"];
    if (/^\/merchants/.test(path)) return ["How do I get paid as a store?", "What do these numbers mean?", "What is Sign and publish?", "What are Opportunities?", "Is this real money?"];
    if (/^\/handshake/.test(path)) return ["How does the Handshake work?", "What do green, yellow and red mean?", "What is a proof pack?", "What is the trust score?", "Run a handshake with harbor"];
    if (/^\/pass/.test(path)) return ["What is a Pass?", "What happens when I press Stop all?", "How do I change my limits?", "Is this real money?", "What is a proof pack?"];
    if (/^\/app/.test(path)) return ["What did I spend today?", "Show held purchases", "What happens when I press Stop all?", "Why was something blocked?", "Which PayPal APIs does this use?"];
    if (/^\/why/.test(path)) return ["What is Lowtides?", "How does the Handshake work?", "Which PayPal APIs does this use?", "Tell me about the hackathon", "Who made this?"];
    return ["What is Lowtides?", "How does the Handshake work?", "Is this real money?", "Which PayPal APIs does this use?", "Who made this?"];
  }

  // ---- messages -----------------------------------------------------------------------
  function scroll() { log.scrollTop = log.scrollHeight; }
  var chat = [], humanBox = null, humanNote = null, hEmail = null, hMsg = null, hSend = null, hSaid = null, hHoney = null, hShown = 0;

  function addMsg(who, text, meta) {
    var m = el("div", "lta-msg lta-" + who);
    m.appendChild(el("p", "lta-text", text));
    if (meta) {
      var tag = el("p", "lta-meta");
      tag.appendChild(el("span", "lta-tag lta-tag-" + meta.source, meta.source === "claude" ? "answered by AI" : "scripted answer"));
      if (meta.cost > 0) tag.appendChild(el("span", "lta-cost", "$" + meta.cost.toFixed(4)));
      m.appendChild(tag);
    }
    log.appendChild(m);
    scroll();
    return m;
  }

  function addCard(a, after) {
    var card = el("div", "lta-card");
    card.appendChild(el("p", "lta-card-label", a.label));
    var note = el("p", "lta-card-note", "Nothing happens until you tap Confirm.");
    card.appendChild(note);
    var row = el("div", "lta-card-row");
    var ok = el("button", "lta-btn lta-btn-go", "Confirm");
    ok.type = "button";
    var no = el("button", "lta-btn lta-btn-quiet", "Cancel");
    no.type = "button";
    row.appendChild(ok); row.appendChild(no);
    card.appendChild(row);
    function done(msg, good) {
      row.hidden = true;
      note.textContent = msg;
      card.className = "lta-card " + (good ? "lta-card-done" : "lta-card-off");
      scroll();
      if (voiceOn && good) speak(msg);
    }
    ok.addEventListener("click", function () {
      ok.disabled = no.disabled = true;
      ok.textContent = "Working...";
      api("/api/ask/confirm", { action_id: a.id }).then(function (r) {
        done(r.message || "Done.", true);
      }, function (e) {
        done(e.message, false);
      });
    });
    no.addEventListener("click", function () {
      ok.disabled = no.disabled = true;
      api("/api/ask/confirm", { action_id: a.id, cancel: true }).then(function () {
        done("Canceled. Nothing changed.", false);
      }, function () { done("Canceled. Nothing changed.", false); });
    });
    (after || log).appendChild(card);
    return card;
  }

  // ---- voice --------------------------------------------------------------------------
  // Two voices, honestly labeled. Premium: the server reads an answer with a studio voice (POST /api/ask/speak) when the
  // owner has turned it on and the daily cap allows. Browser: the free voices built into the browser, always the fallback.
  var VOICE_KEY = "lt_ask_voice_name";
  var PAGE_WORDS = { shop: "the Shop page", merchants: "the Merchants page", handshake: "the See both sides page",
                     app: "the dashboard", pass: "the Pass page", why: "the Why page" };
  var EMOJI;
  try { EMOJI = new RegExp("[\\u{1F000}-\\u{1FFFF}\\u2190-\\u21FF\\u2300-\\u23FF\\u2460-\\u27BF\\u2900-\\u297F\\u2B00-\\u2BFF\\uFE0F\\u200D\\u00A0]", "gu"); }
  catch (e) { EMOJI = /[←-⇿⌀-⏿①-➿⤀-⥿⬀-⯿️‍ ]|[\uD83C-\uD83F][\uDC00-\uDFFF]/g; }
  var voiceList = [], goodVoices = [], speakToken = 0, premiumOff = false, audioEl = null, audioUrl = null, speakingNow = false;

  // What gets read: no URLs, ids, markup or emoji. "$11.99" stays; "0-100" becomes words. Mirrors spoken_text() on the server.
  function cleanSpeech(t) {
    t = String(t == null ? "" : t);
    t = t.replace(/https?:\/\/\S+|www\.\S+/g, " ").replace(/\(\/\)/g, " ");
    t = t.replace(/(^|[^\w\/])\/([a-z]+)\/?(?![\w.\/?=-])/g, function (m, pre, w) { return pre + " " + (PAGE_WORDS[w] || "") + " "; });
    t = t.replace(/(^|[^\w$])\/[\w.\-?=&#\/]+/g, "$1 ");
    t = t.replace(/\b(?:act|hs|pass|spend|ord|tok|sb|ck)_[A-Za-z0-9_-]{6,}\b|\bSIM-[A-Za-z0-9]+\b|\b[0-9a-f]{12,}\b/g, " ");
    t = t.replace(/\b([a-z0-9-]+)\.example\b/g, "$1");
    t = t.replace(/\b0\s*(?:-|–|to)\s*100\b/g, "zero to one hundred");
    t = t.replace(/(\d)\s*%/g, "$1 percent");
    t = t.replace(/([\s(])\+(?=\d)/g, "$1plus ").replace(/([\s(])-(?=\d)/g, "$1minus ");
    t = t.replace(/\be\.g\./g, "for example").replace(/\bi\.e\./g, "that is").replace(/\bvs\.?(?=\s)/g, "versus");
    t = t.replace(/&/g, " and ").replace(/[—–]/g, ", ").replace(/…/g, ".");
    t = t.replace(EMOJI, " ");
    t = t.replace(/[*_`#~^|<>\[\]{}\\="“”]/g, " ");
    t = t.replace(/\s+/g, " ").trim();
    return t.replace(/\s+([,.;:!?)])/g, "$1").replace(/\(\s+/g, "(");
  }

  // Short pieces, so no single utterance runs long enough to hit Chrome's ~15 second cutoff.
  function splitSentences(t, max) {
    max = max || 160;
    t = String(t || "").replace(/\s+/g, " ").trim();
    var parts = [], cur = "", i;
    for (i = 0; i < t.length; i++) {
      cur += t[i];
      if (/[.!?]/.test(t[i]) && (i + 1 >= t.length || /\s/.test(t[i + 1]))) { parts.push(cur.trim()); cur = ""; }
    }
    if (cur.trim()) parts.push(cur.trim());
    var out = [];
    parts.forEach(function (p) {
      while (p.length > max) {
        var cut = p.lastIndexOf(", ", max);
        if (cut < max * 0.4) cut = p.lastIndexOf(" ", max);
        if (cut <= 0) cut = max;
        out.push(p.slice(0, cut + 1).trim());
        p = p.slice(cut + 1).trim();
      }
      if (p) out.push(p);
    });
    var merged = [];
    out.forEach(function (s) {
      var last = merged[merged.length - 1];
      if (last && (last.length < 24 || s.length < 12) && last.length + s.length + 1 <= max) merged[merged.length - 1] = last + " " + s;
      else merged.push(s);
    });
    return merged;
  }

  // Best English voice: Natural/Neural/Online first, then Google US English, then Samantha/Aria/Jenny/Ava; en-US over other English.
  function nameTier(v) {
    var n = (v && v.name) || "";
    if (/natural|neural|online/i.test(n)) return 100;
    if (/google us english/i.test(n)) return 80;
    if (/samantha|aria|jenny|\bava\b/i.test(n)) return 70;
    return 0;
  }
  function scoreVoice(v) {
    var l = String((v && v.lang) || "").replace("_", "-");
    if (!/^en(-|$)/i.test(l)) return -1;
    var s = nameTier(v);
    if (/^en-us$/i.test(l)) s += 20; else if (/^en-(gb|au|ca|ie|nz|za|in)$/i.test(l)) s += 5;
    return s;
  }
  function rankVoices(list) {
    return (list || []).map(function (v, i) { return { v: v, s: scoreVoice(v), i: i }; })
      .filter(function (x) { return x.s >= 0; })
      .sort(function (a, b) { return b.s - a.s || a.i - b.i; });
  }
  function pickBest(list, savedURI) {
    var ranked = rankVoices(list);
    if (savedURI) {
      for (var i = 0; i < ranked.length; i++) if (ranked[i].v.voiceURI === savedURI || ranked[i].v.name === savedURI) return ranked[i].v;
    }
    return ranked.length ? ranked[0].v : null;
  }
  function currentVoice() { return pickBest(voiceList, store("get", VOICE_KEY)); }
  function refreshVoices() {
    try { voiceList = window.speechSynthesis ? window.speechSynthesis.getVoices() || [] : []; } catch (e) { voiceList = []; }
    goodVoices = rankVoices(voiceList).filter(function (x) { return nameTier(x.v) >= 70; }).map(function (x) { return x.v; });
    if (!vsel) return;
    var best = currentVoice();
    vsel.textContent = "";
    goodVoices.forEach(function (v) {
      var o = el("option", "", v.name.replace(/^Microsoft\s+/, "") + " (" + v.lang + ")");
      o.value = v.voiceURI || v.name;
      if (best && (best.voiceURI === v.voiceURI && best.name === v.name)) o.selected = true;
      vsel.appendChild(o);
    });
    vsel.hidden = goodVoices.length < 2;
    if (vsel.previousSibling && vsel.previousSibling.className === "lta-vlab") vsel.previousSibling.hidden = vsel.hidden;
  }

  // Talk mode: after a question asked by voice, the answer is read aloud and the mic opens again by itself.
  var convo = false, afterSpeak = null;
  function setSpeaking(on, engine) {
    var was = speakingNow;
    speakingNow = !!on;
    if (!on && was && afterSpeak) { var f = afterSpeak; afterSpeak = null; setTimeout(f, 250); }
    if (stopBtn) stopBtn.hidden = !on;
    if (engine && vlabel) {
      vlabel.textContent = engine === "premium" ? "Premium voice" : "Browser voice";
      vlabel.setAttribute("data-engine", engine);
    }
  }
  function releaseAudio() {
    if (audioEl) { audioEl.onended = audioEl.onerror = null; try { audioEl.pause(); } catch (e) { /* ignore */ } audioEl = null; }
    if (audioUrl) { try { URL.revokeObjectURL(audioUrl); } catch (e) { /* ignore */ } audioUrl = null; }
  }
  function stopSpeaking() {
    afterSpeak = null;
    speakToken++;
    try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    releaseAudio();
    setSpeaking(false);
  }

  function speakBrowser(text, token) {
    var synth = window.speechSynthesis;
    var sentences = splitSentences(cleanSpeech(text));
    if (!synth || !sentences.length || typeof SpeechSynthesisUtterance === "undefined") { setSpeaking(false); return; }
    try { synth.cancel(); } catch (e) { /* ignore */ }
    var v = currentVoice();
    sentences.forEach(function (s, i) {
      var u = new SpeechSynthesisUtterance(s);
      if (v) { u.voice = v; u.lang = v.lang; } else { u.lang = "en-US"; }
      u.rate = 0.98;
      u.pitch = 1.0;
      if (i === 0) u.onstart = function () { if (token === speakToken) setSpeaking(true, "browser"); };
      if (i === sentences.length - 1) u.onend = u.onerror = function () { if (token === speakToken) setSpeaking(false); };
      else u.onerror = function () { /* a failed piece must not stall the rest */ };
      synth.speak(u);
    });
    setSpeaking(true, "browser");
  }

  // Consecutive sentences of the RAW answer, up to ~520 characters each (the server cleans and re-checks them).
  function groupChunks(raw) {
    var out = [], cur = "";
    splitSentences(raw, 520).forEach(function (s) {
      if (cur && (cur + " " + s).length > 520) { out.push(cur); cur = s; } else { cur = cur ? cur + " " + s : s; }
    });
    if (cur) out.push(cur);
    return out;
  }
  function postSpeak(text) {
    var h = { "Content-Type": "application/json" };
    try { var t = window.sessionStorage.getItem("lt_admin"); if (t) h["X-Admin-Token"] = t; } catch (e) { /* ignore */ }
    return fetch("/api/ask/speak", { method: "POST", headers: h, credentials: "same-origin", body: JSON.stringify({ text: text }) })
      .then(function (r) {
        var ct = r.headers.get("Content-Type") || "";
        if (r.ok && /^audio\//.test(ct)) return r.blob();
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (j && (j.reason === "not_configured" || j.reason === "off")) premiumOff = true;   // don't ask again this visit
          return null;
        });
      }, function () { return null; });
  }
  function speakPremium(text, token) {
    var chunks = [].concat.apply([], text.map(function (t) { return groupChunks(t); })), i = 0, next = postSpeak(chunks[0] || "");
    function rest() { return chunks.slice(i).join(" "); }
    function step() {
      next.then(function (blob) {
        if (token !== speakToken) return;
        if (!blob) { speakBrowser(rest(), token); return; }
        releaseAudio();
        audioUrl = URL.createObjectURL(blob);
        var a = new Audio(audioUrl);
        audioEl = a;
        next = i + 1 < chunks.length ? postSpeak(chunks[i + 1]) : null;
        a.onended = function () {
          if (token !== speakToken) return;
          releaseAudio();
          i++;
          if (next && i < chunks.length) step(); else setSpeaking(false);
        };
        var fell = false;
        function fall() {                       // error and a rejected play() can both fire: fall back exactly once
          if (fell || token !== speakToken) return;
          fell = true;
          releaseAudio();
          speakBrowser(rest(), token);
        }
        a.onerror = fall;
        setSpeaking(true, "premium");
        var pr = a.play();
        if (pr && pr.catch) pr.catch(fall);
      });
    }
    if (!chunks.length) { setSpeaking(false); return; }
    step();
  }
  function canPremium() { return !premiumOff && window.fetch && window.Audio && window.URL && window.URL.createObjectURL; }
  function speak(text) {                                  // one text, or a list of texts read one after another
    stopSpeaking();
    var parts = (Array.isArray(text) ? text : [text]).map(function (t) { return String(t || ""); }).filter(function (t) { return t.trim(); });
    if (!parts.length) return;
    var token = ++speakToken;
    if (canPremium()) speakPremium(parts, token);
    else speakBrowser(parts.join(" "), token);
  }
  function setVoice(on) {
    voiceOn = !!on;
    voiceBtn.setAttribute("aria-pressed", voiceOn ? "true" : "false");
    voiceBtn.classList.toggle("lta-on", voiceOn);
    if (vrow) vrow.hidden = !voiceOn;
    store("set", "lt_ask_voice", voiceOn ? "1" : "0");
    if (!voiceOn) stopSpeaking();
  }

  // ---- microphone ---------------------------------------------------------------------
  function note(msg) { if (statusEl) { statusEl.textContent = msg || ""; statusEl.hidden = !msg; } }
  function micBlockedByPolicy() {
    try {
      var pp = doc.permissionsPolicy || doc.featurePolicy;
      return !!(pp && pp.allowsFeature && !pp.allowsFeature("microphone"));
    } catch (e) { return false; }
  }
  function micMessage(code) {
    if (code === "not-allowed" || code === "service-not-allowed") return "The microphone is blocked for this site. To fix it: click the lock icon in the address bar, set Microphone to Allow, then tap Talk again, or just type your question.";
    if (code === "audio-capture") return "I can't find a microphone on this device. You can type your question instead.";
    if (code === "network") return "Speech recognition needs an internet connection in this browser. You can type your question instead.";
    if (code === "no-speech") return "I didn't hear anything. Tap Talk and try again, or type your question.";
    return "The microphone didn't work this time. You can type your question instead.";
  }
  function setListening(on) {
    listening = !!on;
    micBtn.setAttribute("aria-pressed", on ? "true" : "false");
    micBtn.classList.toggle("lta-on", !!on);
    micLabel.textContent = on ? "Listening\u2026" : "Talk";
    micBtn.classList.toggle("lta-live", !!on);
    if (statusEl) statusEl.classList.toggle("lta-live", !!on);
    if (on) note(convo && chat.length ? "Listening\u2026 speak now. Ask your next question, or tap the button to stop." : "Listening\u2026 speak now.");
  }
  // ---- talking: one spoken conversation ---------------------------------------------------
  var SAY = {"greet": "Hi, I'm listening. What would you like to know?", "again": "I'm listening. Go ahead.", "still": "I'm still here. Do you have another question?", "pause": "Okay. Tap Talk whenever you want me.", "more": "Want to hear more?", "f1": "Did that answer it?", "f2": "Want me to show you around?", "f3": "Is there anything else you'd like to know?", "next": "Say or type next to keep going, or stop to end.", "tourEnd": "That's the tour. Want me to open the live demo so you can watch it happen?", "opening": "Opening it now."};
  var TOUR = ["Lowtides lets an AI shop for you while you keep the keys. Your AI shopper carries your rules, and every store shows its own.", "First, your limits. On the shopper page you set how much it may spend in a day, how much on one thing, and when it must ask you first.", "Next, the mode. Off buys nothing. AI Assist finds things and asks you before any money moves. Full Autopilot buys by itself, but only when both sides say yes.", "Stores have a control center too. A store chooses whether it takes AI orders, how large, and how much trust it needs.", "Then the Handshake. Your rules and the store's rules check each other. Green pays, yellow waits for you, and red moves no money.", "It is not just shopping. With Action Muzzle you tell your AI the job, like booking a table, and it may do only that. Anything extra, it asks you first, and your stop word freezes everything.", "Last, Stop all. One button stops every AI shopper at every store, and you can turn them back on. Everything here uses PayPal sandbox test money."];
  var greeted = false, nudged = false, tourAt = -1, lastRest = "", pending = "", fups = 0;
  function endTalk() {
    convo = false;
    try { if (listening && rec) rec.abort(); } catch (e) { /* ignore */ }
  }
  // Say something, then open the mic again by itself. If speech never starts, listen anyway.
  function sayAndListen(texts) {
    speak(texts);
    var tk = speakToken, done = false;
    var go = function () { if (done) return; done = true; if (open && convo && !busy && !listening) startListening(); };
    afterSpeak = go;
    if (!speakingNow && !canPremium()) { afterSpeak = null; go(); return; }
    setTimeout(function () { if (!done && tk === speakToken && !speakingNow) { afterSpeak = null; go(); } }, 12000);
  }
  // The guide says a fixed line (shown in the chat too).
  function botSay(texts) {
    texts = texts.filter(function (t) { return t; });
    addMsg("bot", texts.join(" "));
    chat.push({ who: "guide", text: texts.join(" ") });
    if (convo) sayAndListen(texts); else if (voiceOn) speak(texts);
  }
  function tourStep(i) {
    if (i >= TOUR.length - 1) { tourAt = -1; pending = "open"; botSay([TOUR[TOUR.length - 1], SAY.tourEnd]); return; }
    tourAt = i; pending = "";
    botSay([TOUR[i], SAY.next]);
  }
  // Things a person says that the guide handles right here: next, more, stop, give me the tour.
  function localVoice(q) {
    var w = q.toLowerCase().replace(/[^a-z' ]/g, " ").replace(/\s+/g, " ").trim();
    var yes = /^(yes|yeah|yep|yup|sure|ok|okay|please|yes please|go on|go ahead|next|continue|keep going|more|tell me more|sure thing)$/.test(w);
    var no = /^(no|nope|no thanks|no thank you|stop|that's all|that is all|goodbye|bye|i'm done|im done|exit|quit|end|that's enough)$/.test(w);
    if (!yes && !no && !/\b(tour|show me around|walk me through|show me how it works)\b/.test(w)) { tourAt = -1; pending = ""; return false; }
    addMsg("me", q);
    chat.push({ who: "you", text: q });
    if (no) { tourAt = -1; pending = ""; lastRest = ""; var bye = convo ? SAY.pause : "Okay. Ask me anything, any time."; addMsg("bot", bye); if (convo || voiceOn) speak(bye); convo = false; return true; }
    if (!yes) { tourStep(0); return true; }
    if (tourAt >= 0) { tourStep(tourAt + 1); return true; }
    if (pending === "open") { pending = ""; addMsg("bot", SAY.opening); if (convo || voiceOn) speak(SAY.opening); convo = false; setTimeout(function () { location.href = "/handshake/?tour=1"; }, 1600); return true; }
    if (pending === "tour") { tourStep(0); return true; }
    if (pending === "more" && lastRest) { var r = lastRest; lastRest = ""; pending = ""; botSay(/\?\s*$/.test(r) ? [r] : [r, SAY.f3]); return true; }
    botSay([SAY.f3]);
    return true;
  }
  // A spoken answer is short: a few sentences, then ONE follow-up question. The full answer stays in the chat.
  function shape(answer) {
    var sents = splitSentences(String(answer || ""), 400), head = [], n = 0;
    while (sents.length && (head.length < 1 || (head.length < 3 && n + sents[0].length <= 300))) { n += sents[0].length; head.push(sents.shift()); }
    lastRest = sents.join(" ");
    var follow = "";
    if (lastRest) { pending = "more"; follow = SAY.more; }
    else if (/\?\s*$/.test(answer)) { pending = ""; }
    else { var k = fups++ % 3; follow = [SAY.f1, SAY.f2, SAY.f3][k]; pending = k === 1 ? "tour" : ""; }
    return { say: [head.join(" "), follow], follow: follow };
  }
  function startListening() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { convo = false; note("This browser can't listen to speech. Try Chrome or Edge, or just type your question."); return; }
    if (micBlockedByPolicy()) { convo = false; note("This page isn't allowed to use the microphone here. You can type your question instead."); return; }
    stopSpeaking();
    note("");
    var heard = false, failed = false;
    try {
      rec = new SR();
      rec.lang = doc.documentElement.lang || "en-US";
      rec.interimResults = false;
      rec.continuous = false;
      rec.maxAlternatives = 1;
      rec.onstart = function () { setListening(true); };
      rec.onresult = function (ev) {
        for (var i = ev.resultIndex || 0; i < ev.results.length; i++) {
          if (ev.results[i].isFinal) {
            var t = ev.results[i][0] && ev.results[i][0].transcript;
            if (t && t.trim() && !heard) {
              heard = true; nudged = false; note("");
              t = t.trim();
              if (!localVoice(t)) { input.value = t; submit(t, true); }
            }
          }
        }
      };
      rec.onerror = function (ev) {
        if (ev.error === "aborted" || ev.error === "no-speech") return;      // silence is handled when it ends
        failed = true; convo = false; note(micMessage(ev.error));
      };
      rec.onend = function () {
        setListening(false);
        if (heard) return;
        if (failed || !convo) { if (!failed) note(""); return; }
        if (!nudged) { nudged = true; note(SAY.still); sayAndListen(SAY.still); }          // quiet for a while: ask once
        else { convo = false; note(SAY.pause); speak(SAY.pause); }                          // still quiet: pause
      };
      rec.start();
    } catch (e) { convo = false; setListening(false); note(micMessage("")); }
  }
  // ---- optional natural voice: a live call with the "Ask Lowtides" Vapi assistant ---------------------
  // On only when the server says so (GET /api/ask/voice). Any failure switches back to the built-in Talk above.
  var vapiCfg = null, vapiObj = null, vapiLive = false, vapiBroken = false, vapiWait = 0, vapiConnecting = false;
  function vapiFail(why) {
    clearTimeout(vapiWait);
    vapiBroken = true; vapiLive = false; vapiConnecting = false;
    try { if (vapiObj) vapiObj.stop(); } catch (e) { /* ignore */ }
    setListening(false);
    note((why || "The natural voice isn't available right now.") + " I switched to the built-in voice: tap Talk again.");
  }
  function vapiLoad() {
    return new Promise(function (ok, bad) {
      if (vapiObj) return ok(vapiObj);
      var st = doc.createElement("style");
      st.textContent = "#vapi-support-btn, .vapi-btn { display: none !important; }";      // our Talk button runs the call
      doc.head.appendChild(st);
      var sc = doc.createElement("script");
      sc.src = "https://cdn.jsdelivr.net/gh/VapiAI/html-script-tag@latest/dist/assets/index.js";
      sc.async = true;
      sc.onerror = function () { bad(new Error("load")); };
      sc.onload = function () {
        try {
          vapiObj = window.vapiSDK.run({ apiKey: vapiCfg.key, assistant: vapiCfg.assistant, config: { position: "bottom-left", offset: "0px" } });
          if (!vapiObj || typeof vapiObj.start !== "function") throw new Error("no start");
          vapiObj.on("call-start", function () {
            clearTimeout(vapiWait); vapiConnecting = false;
            if (!open) { try { vapiObj.stop(); } catch (e) { /* ignore */ } return; }      // the helper was closed while connecting
            vapiLive = true; setListening(true); note("You're talking with Ask Lowtides (an AI). Speak now. Tap the button to end. Calls end by themselves after 3 minutes."); });
          vapiObj.on("call-end", function () { clearTimeout(vapiWait); vapiLive = false; vapiConnecting = false; setListening(false); note("Call ended. Tap Talk to talk again, or type below."); });
          vapiObj.on("message", function (m) {
            if (!m || m.type !== "transcript" || m.transcriptType !== "final" || !m.transcript) return;
            var mine = m.role === "user";
            addMsg(mine ? "me" : "bot", String(m.transcript));
            chat.push({ who: mine ? "you" : "guide", text: String(m.transcript).slice(0, 1500) });
          });
          vapiObj.on("error", function () { vapiFail(); });
          ok(vapiObj);
        } catch (e) { bad(e); }
      };
      doc.head.appendChild(sc);
    });
  }
  function vapiToggle() {
    if (vapiLive) { try { vapiObj.stop(); } catch (e) { /* ignore */ } return; }
    if (vapiConnecting) return;                    // one call at a time
    vapiConnecting = true;
    stopSpeaking();
    note("Connecting\u2026 allow the microphone if your browser asks.");
    micLabel.textContent = "Connecting\u2026";
    clearTimeout(vapiWait);
    vapiWait = setTimeout(function () { if (!vapiLive) vapiFail("That took too long to connect."); }, 20000);
    vapiLoad().then(function (v) { return v.start(vapiCfg.assistant); }).then(null, function () { vapiFail(); });
  }
  function setupMic() {
    // Which voice does this server use? Asked once the helper is built, and again on a Talk press if that failed.
    var asked = null;
    function whichVoice() {
      if (!window.fetch) return Promise.resolve();
      if (!asked) {
        asked = fetch("/api/ask/voice", { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw new Error("x"); return r.json(); }).then(function (j) {
          if (j && j.vapi && j.vapi.key && j.vapi.assistant) vapiCfg = j.vapi;
        }).then(null, function () { asked = null; });
      }
      return asked;
    }
    whichVoice();
    micBtn.addEventListener("click", function () {
      if (vapiLive || (vapiCfg && !vapiBroken)) { vapiToggle(); return; }
      if (!vapiBroken && !asked) { whichVoice().then(function () { if (vapiCfg) vapiToggle(); else talkBuiltIn(); }); return; }
      talkBuiltIn();
    });
    function talkBuiltIn() {
      (function () {
      if (listening) { convo = false; try { rec.stop(); } catch (e) { /* ignore */ } return; }
      if (convo && speakingNow) { stopSpeaking(); startListening(); return; }     // tapped while the guide talks: it stops and listens
      if (convo) { endTalk(); stopSpeaking(); note(""); return; }
      var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR || micBlockedByPolicy()) { startListening(); return; }   // it explains what is wrong and how to fix it
      convo = true; nudged = false;
      if (!voiceOn) setVoice(true);               // a spoken question gets a spoken answer
      note("Starting\u2026 you\u2019ll hear me first, then speak.");
      sayAndListen(greeted ? SAY.again : SAY.greet);                   // the hello is said once per visit
      greeted = true;
      })();
    }
  }

  // ---- asking -------------------------------------------------------------------------
  function submit(q, spoken) {
    q = (q || "").replace(/\s+/g, " ").trim();
    if (!q || busy) return;
    busy = true;
    stopSpeaking();
    input.value = "";
    sendBtn.disabled = true;
    addMsg("me", q);
    chat.push({ who: "you", text: q });
    log.setAttribute("aria-busy", "true");
    var wait = addMsg("bot", "Thinking...");
    wait.classList.add("lta-wait");
    var hist = chat.slice(-7, -1);
    api("/api/ask", { question: q, page: location.pathname, spoken: !!(spoken && convo), history: hist }).then(function (r) {
      wait.parentNode.removeChild(wait);
      var m = addMsg("bot", r.answer || "I don't have an answer for that.", { source: r.source, cost: Number(r.cost_usd) || 0 });
      if (r.answer) chat.push({ who: "guide", text: r.answer });
      (r.actions || []).forEach(function (a) { addCard(a, null); });
      if (spoken && convo) {
        var sh = shape(r.answer || "");
        if (sh.follow) { addMsg("bot", sh.follow); chat.push({ who: "guide", text: sh.follow }); }
        busy = false;                              // so the mic can reopen as soon as the answer has been spoken
        sayAndListen(sh.say);
      } else if (voiceOn) speak(r.answer || "");
      m.setAttribute("tabindex", "-1");
    }, function (e) {
      wait.parentNode.removeChild(wait);
      addMsg("bot", e.message);
    }).then(function () {
      busy = false;
      sendBtn.disabled = false;
      log.removeAttribute("aria-busy");
      if (open) input.focus();
    });
  }

  // ---- panel open / close / focus trap ------------------------------------------------
  function focusables() {
    var list = panel.querySelectorAll("button:not([disabled]):not([hidden]), input:not([disabled]), a[href]");
    return Array.prototype.filter.call(list, function (n) { return n.offsetParent !== null; });
  }
  function onKey(ev) {
    if (!open) return;
    if (ev.key === "Escape") { ev.preventDefault(); close(); return; }
    if (ev.key !== "Tab") return;
    var f = focusables();
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (ev.shiftKey && (doc.activeElement === first || !panel.contains(doc.activeElement))) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && (doc.activeElement === last || !panel.contains(doc.activeElement))) { ev.preventDefault(); first.focus(); }
  }
  function openPanel(q) {
    mount();
    if (!open) {
      lastFocus = doc.activeElement;
      open = true;
      panel.hidden = false;
      fab.setAttribute("aria-expanded", "true");
      root.classList.add("lta-open");
      if (!log.children.length) {
        // One hello, the same on every page. It is shown, not spoken, until the person presses Talk.
        store("set", "lta_greeted", "1");
        addMsg("bot", "Hi, I'm Ask Lowtides. Press Talk to ask out loud, or type below. Want a quick tour?");
        pending = "tour";
      }
    }
    if (q) submit(q); else input.focus();
  }
  function close() {
    endTalk();
    clearTimeout(vapiWait);
    if (vapiObj && (vapiLive || vapiConnecting)) { try { vapiObj.stop(); } catch (e) { /* ignore */ } }
    vapiConnecting = false;
    if (!open) return;
    open = false;
    panel.hidden = true;
    fab.setAttribute("aria-expanded", "false");
    root.classList.remove("lta-open");
    root.classList.add("lta-tab");                 // tucked away as a tab on the side; one tap brings the chat back
    store("set", "lta_tab", "1");
    stopSpeaking();
    if (listening) { try { rec.stop(); } catch (e) { /* ignore */ } }
    var back = lastFocus && lastFocus !== doc.body && doc.contains(lastFocus) ? lastFocus : fab;
    back.focus();
  }

  // The mascot's pictures, in ONE place. GuardPooch is the watchdog and the face of this helper. To change the art,
  // change these paths; any <img data-mascot="head|full|..."> on a page picks its picture up from here too.
  var MASCOT = {
    head: "/brand/mascot/dog-head.webp",     // small round face: helper button, panel header, side tab
    full: "/brand/mascot/dog-full.webp",     // whole dog: the "Meet GuardPooch" section on the home page
    sit: "/brand/mascot/dog-sit.webp",       // welcome
    alert: "/brand/mascot/dog-alert.webp",   // sniffing / checking a store
    bark: "/brand/mascot/dog-bark.webp",     // warning: a fake store was caught
    happy: "/brand/mascot/dog-happy.webp"    // both said yes
  };
  window.LowtidesMascot = MASCOT;
  function mascots() {
    [].forEach.call(doc.querySelectorAll("img[data-mascot]"), function (im) {
      var src = MASCOT[im.getAttribute("data-mascot")];
      if (src && im.getAttribute("src") !== src) im.setAttribute("src", src);
    });
  }
  function pooch(cls, size) {
    var i = doc.createElement("img");
    i.className = cls; i.src = MASCOT.head; i.alt = ""; i.width = size; i.height = size;
    i.decoding = "async";
    return i;
  }

  // ---- Talk to a human: the message and the chat so far go to a person (POST /api/help) ----
  function showHuman(on) {
    humanBox.hidden = !on;
    log.hidden = chipsBox.hidden = form.hidden = on;
    if (on) {
      humanNote.textContent = "Send a message to a person at Lowtides. They get your message" +
        (chat.length ? " and your chat with the guide so far (" + chat.length + " message" + (chat.length === 1 ? "" : "s") + ")" : "") +
        ", so you don\u2019t have to repeat yourself. Your email is used only to answer this request.";
      hSaid.textContent = "";
      hShown = Date.now();
      hEmail.focus();
    }
  }
  function openHuman() {
    openPanel("");
    showHuman(true);
  }
  function sendHuman(ev) {
    ev.preventDefault();
    hSend.disabled = true;
    hSaid.textContent = "Sending...";
    api("/api/help", { email: hEmail.value.trim(), message: hMsg.value.trim(), page: location.pathname, chat: chat.slice(-30), _honey: hHoney.value, filled_ms: Date.now() - hShown }).then(function (r) {
      hMsg.value = "";
      showHuman(false);
      addMsg("bot", r.said);
      input.focus();
    }, function (e) {
      hSaid.textContent = (e && e.message) || "That didn\u2019t send. Please try again.";
    }).then(function () { hSend.disabled = false; });
  }
  function buildHuman() {
    humanBox = el("form", "lta-human");
    humanBox.hidden = true;
    humanBox.setAttribute("aria-label", "Talk to a human");
    humanBox.appendChild(el("strong", "lta-human-h", "Talk to a human"));
    humanNote = el("p", "lta-human-note");
    humanBox.appendChild(humanNote);
    var l1 = el("label", "lta-human-l", "Your email, for the reply");
    hEmail = el("input", "lta-input");
    hEmail.type = "email"; hEmail.required = true; hEmail.maxLength = 200; hEmail.autocomplete = "email"; hEmail.id = "lta-h-email";
    l1.setAttribute("for", "lta-h-email");
    var l2 = el("label", "lta-human-l", "What do you need?");
    hMsg = el("textarea", "lta-input lta-human-msg");
    hMsg.required = true; hMsg.maxLength = 2000; hMsg.rows = 4; hMsg.id = "lta-h-msg";
    l2.setAttribute("for", "lta-h-msg");
    hHoney = el("input", "lta-honey");
    hHoney.type = "text"; hHoney.name = "_honey"; hHoney.tabIndex = -1; hHoney.autocomplete = "off"; hHoney.setAttribute("aria-hidden", "true");
    humanBox.appendChild(hHoney);
    hSaid = el("p", "lta-human-said");
    hSaid.setAttribute("role", "status");
    var row = el("div", "lta-human-row");
    hSend = el("button", "lta-btn lta-btn-go", "Send to a person");
    hSend.type = "submit";
    var back = el("button", "lta-btn", "Back to the AI guide");
    back.type = "button";
    back.addEventListener("click", function () { showHuman(false); input.focus(); });
    row.appendChild(hSend); row.appendChild(back);
    [l1, hEmail, l2, hMsg, hSaid, row].forEach(function (n) { humanBox.appendChild(n); });
    humanBox.addEventListener("submit", sendHuman);
    return humanBox;
  }
  // Two buttons at the top right of every page: the AI guide, or a person.
  function talkBar() {
    var hd = doc.querySelector(".lt-header");
    if (!hd || doc.querySelector(".lta-talkbar")) return;
    var bar = el("div", "lta-talkbar");
    var inner = el("div", "lta-talkbar-in");
    var ai = el("button", "lta-talk", "Talk to AI guide");
    ai.type = "button";
    ai.addEventListener("click", function () { openPanel(""); showHuman(false); });
    var hu = el("button", "lta-talk lta-talk-human", "Talk to a human");
    hu.type = "button";
    hu.addEventListener("click", openHuman);
    inner.appendChild(ai); inner.appendChild(hu);
    bar.appendChild(inner);
    hd.parentNode.insertBefore(bar, hd);
  }
  // ---- plain-words help bubbles: hover, keyboard focus or a tap shows one line about the control ----
  var HELP = [
    [".lta-acct button", "Your account. Sign in to keep your own dashboard. The demo also works without signing in."],
    [".lta-talk-human", "Send a message to a real person. They also get your chat with the guide."],
    [".lta-talk", "Open the AI guide. Ask it anything about this page."],
    ["#kill, #kill-dock", "Stop all: stops every AI shopper right now, at every store. You can turn them back on."],
    ["#kill-demo", "This is a practice button. On the shopper page, Stop all stops every AI shopper at every store."],
    ['[data-act="approve"]', "Approve: yes, let this purchase go through."],
    ['[data-act="deny"]', "Deny: no, don\u2019t buy this. No money moves."],
    ["#mode, #mode-q", "Choose how much your AI shopper may do. Off: it buys nothing. AI Assist: it asks you first. Full Autopilot: it buys by itself when both sides say yes."],
    ["#smode", "Choose how your store treats AI shoppers. Off: no AI orders. AI Assist: a person checks each one. Full Autopilot: orders that fit your rules are accepted."],
    ["#daily_cap", "Slide to set the most your AI shopper may spend in one day."],
    ["#per_item_cap", "Slide to set the most it may spend on one thing."],
    ["#ask_above", "Above this amount, your AI shopper must stop and ask you first."],
    ["#max_order_no_human", "Orders above this amount wait for a person at your store."],
    ["#min_trust", "AI shoppers with a trust score below this number are turned away."],
    ["#rc-q", "Type what you are looking for in plain words. Example: paid this week over $20."],
    ["#auto-go", "Type what you want in the box, then press this. Your AI shopper looks, checks both sides\u2019 rules, and follows the mode you picked."]
  ];
  var tip = null, tipFor = null, tipTimer = 0, helpN = 0;
  function helpApply() {
    HELP.forEach(function (h) {
      [].forEach.call(doc.querySelectorAll(h[0]), function (n) { if (!n.getAttribute("data-help")) n.setAttribute("data-help", h[1]); });
    });
    [].forEach.call(doc.querySelectorAll("[data-help]:not([data-help-id])"), function (n) {
      var id = "lta-help-" + (++helpN), sr = el("span", "lta-sr", n.getAttribute("data-help"));
      sr.id = id;
      doc.body.appendChild(sr);
      n.setAttribute("data-help-id", id);
      if (/^(BUTTON|INPUT|A|SELECT|TEXTAREA)$/.test(n.tagName)) {
        var had = n.getAttribute("aria-describedby");
        n.setAttribute("aria-describedby", had ? had + " " + id : id);
      }
    });
  }
  function helpHide() { clearTimeout(tipTimer); if (tip) tip.hidden = true; tipFor = null; }
  function helpShow(n, ms) {
    if (!tip || !n) return;
    clearTimeout(tipTimer);
    tipFor = n;
    tip.textContent = n.getAttribute("data-help");
    tip.hidden = false;
    var r = n.getBoundingClientRect(), vw = doc.documentElement.clientWidth, vh = window.innerHeight;
    // Inside the Ask panel the bubble keeps to the panel's own width; elsewhere it centers on the control.
    var box = n.closest && n.closest(".lta-panel"), lo = 8, hi = vw - 8;
    if (box) { var br = box.getBoundingClientRect(); lo = Math.max(8, br.left + 8); hi = Math.min(vw - 8, br.right - 8); }
    var w = Math.min(300, hi - lo);
    tip.style.width = w + "px";
    var left = Math.max(lo, Math.min(hi - w, r.left + r.width / 2 - w / 2));
    if (n.classList && n.classList.contains("lta-big")) {              // in the guide: below the how-to line too, so both can be read
      var ht = box && box.querySelector(".lta-howto");
      if (ht) r = { left: r.left, width: r.width, top: r.top, bottom: ht.getBoundingClientRect().bottom };
    }
    var h = tip.offsetHeight, top = r.bottom + 8;                      // below the control, never on top of it
    if (top + h > vh - 8) top = r.top - h - 8 > 8 ? r.top - h - 8 : Math.max(8, vh - h - 8);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
    if (ms) tipTimer = setTimeout(helpHide, ms);
  }
  function helpInit() {
    tip = el("div", "lta-tip");
    tip.setAttribute("role", "tooltip");
    tip.hidden = true;
    doc.body.appendChild(tip);
    helpApply();
    var later = 0;
    if (window.MutationObserver) {
      new MutationObserver(function () { clearTimeout(later); later = setTimeout(helpApply, 300); }).observe(doc.body, { childList: true, subtree: true });
    }
    function target(ev) { return ev.target && ev.target.closest ? ev.target.closest("[data-help]") : null; }
    doc.addEventListener("mouseover", function (ev) { var n = target(ev); if (n && n !== tipFor) helpShow(n); });
    doc.addEventListener("mouseout", function (ev) { var n = target(ev); if (n && (!ev.relatedTarget || !n.contains(ev.relatedTarget))) helpHide(); });
    doc.addEventListener("focusin", function (ev) { var n = target(ev); if (n) helpShow(n, 8000); });
    doc.addEventListener("focusout", helpHide);
    doc.addEventListener("touchstart", function (ev) { var n = target(ev); if (n) helpShow(n, 6000); else helpHide(); }, { passive: true });
    doc.addEventListener("keydown", function (ev) { if (ev.key === "Escape") helpHide(); });
    window.addEventListener("scroll", helpHide, { passive: true });
  }

  function loadAccount() {                        // optional sign-in (ui/account.js); it stays hidden unless the server has accounts on
    var sc = doc.createElement("script");
    sc.src = "/account.js"; sc.defer = true;
    doc.head.appendChild(sc);
  }

  // ---- build the DOM once -------------------------------------------------------------
  function mount() {
    if (root) return;
    root = el("div", "lta-root");
    root.id = "lowtides-ask";

    fab = el("button", "lta-fab");
    fab.type = "button";
    fab.setAttribute("aria-haspopup", "dialog");
    fab.setAttribute("aria-expanded", "false");
    fab.setAttribute("aria-controls", "lta-panel");
    fab.appendChild(pooch("lta-fab-icon", 32));
    fab.appendChild(el("span", "lta-fab-text", "Ask Lowtides"));
    fab.addEventListener("click", function () { open ? close() : openPanel(); });

    panel = el("div", "lta-panel");
    panel.id = "lta-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Ask Lowtides");
    panel.hidden = true;

    var head = el("div", "lta-head");
    var brandRow = el("div", "lta-brand");
    brandRow.appendChild(pooch("lta-head-icon", 32));
    brandRow.appendChild(el("strong", "lta-title", "Ask Lowtides"));
    head.appendChild(brandRow);
    var tools = el("div", "lta-tools");
    micBtn = el("button", "lta-big");
    micBtn.type = "button";
    micBtn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
    micLabel = el("span", "lta-big-t", "Talk");
    micBtn.appendChild(micLabel);
    micBtn.setAttribute("aria-pressed", "false");
    micBtn.setAttribute("data-help", "Press Talk, then say your question out loud. I\u2019ll answer out loud.");
    voiceBtn = el("button", "lta-big");
    voiceBtn.type = "button";
    voiceBtn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5z" fill="currentColor"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
    voiceBtn.appendChild(el("span", "lta-big-t", "Read aloud"));
    voiceBtn.setAttribute("aria-pressed", "false");
    voiceBtn.setAttribute("data-help", "Read aloud: I\u2019ll read my answers to you.");
    var talkRow = el("div", "lta-voicebar");
    talkRow.appendChild(micBtn); talkRow.appendChild(voiceBtn);
    var howto = el("p", "lta-howto", "Press Talk and say your question. I\u2019ll answer out loud. Or type it below.");
    var x = el("button", "lta-icon lta-x", "✕");
    x.type = "button";
    x.setAttribute("aria-label", "Close Ask Lowtides");
    x.title = "Close (Esc)";
    tools.appendChild(x);
    head.appendChild(tools);

    vrow = el("div", "lta-vrow");
    vrow.hidden = true;
    vlabel = el("span", "lta-vlabel", "");
    vlabel.setAttribute("aria-live", "polite");
    var vlab = el("label", "lta-vlab", "Voice");
    vlab.setAttribute("for", "lta-vsel");
    vsel = el("select", "lta-vsel");
    vsel.id = "lta-vsel";
    vsel.hidden = true;
    vlab.hidden = true;
    vsel.addEventListener("change", function () { store("set", VOICE_KEY, vsel.value); });
    stopBtn = el("button", "lta-btn lta-stop", "Stop");
    stopBtn.type = "button";
    stopBtn.setAttribute("aria-label", "Stop reading aloud");
    stopBtn.hidden = true;
    stopBtn.addEventListener("click", function () { endTalk(); stopSpeaking(); });
    vrow.appendChild(vlabel); vrow.appendChild(vlab); vrow.appendChild(vsel); vrow.appendChild(stopBtn);

    statusEl = el("p", "lta-status");
    statusEl.setAttribute("role", "status");
    statusEl.hidden = true;

    log = el("div", "lta-log");
    log.setAttribute("role", "log");
    log.setAttribute("aria-live", "polite");
    log.setAttribute("aria-label", "Conversation");

    chipsBox = el("div", "lta-chips");
    chipsBox.setAttribute("aria-label", "Suggested questions");
    chipsFor(location.pathname).forEach(function (c) {
      var b = el("button", "lta-chip", c);
      b.type = "button";
      b.addEventListener("click", function () { submit(c); });
      chipsBox.appendChild(b);
    });

    form = el("form", "lta-form");
    var lab = el("label", "lta-sr", "Your question");
    lab.setAttribute("for", "lta-input");
    input = el("input", "lta-input");
    input.id = "lta-input";
    input.type = "text";
    input.maxLength = 500;
    input.autocomplete = "off";
    input.placeholder = "Ask anything about Lowtides...";
    sendBtn = el("button", "lta-btn lta-btn-go lta-send", "Send");
    sendBtn.type = "submit";
    form.appendChild(lab); form.appendChild(input); form.appendChild(sendBtn);
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var typed = input.value.replace(/\s+/g, " ").trim();
      if (typed && !busy && (pending || tourAt >= 0 || /\b(tour|show me around)\b/i.test(typed)) && localVoice(typed)) { input.value = ""; return; }
      submit(input.value);
    });

    var foot = el("p", "lta-foot", "I never move money. PayPal sandbox only. ");
    var toHuman = el("button", "lta-foot-link", "Talk to a human");
    toHuman.type = "button";
    toHuman.addEventListener("click", function () { showHuman(true); });
    foot.appendChild(toHuman);

    panel.appendChild(head); panel.appendChild(talkRow); panel.appendChild(howto); panel.appendChild(vrow); panel.appendChild(log); panel.appendChild(chipsBox); panel.appendChild(buildHuman()); panel.appendChild(statusEl); panel.appendChild(form); panel.appendChild(foot);
    if (store("get", "lta_tab")) root.classList.add("lta-tab");
    fab.setAttribute("aria-label", "Ask Lowtides: open the helper");
    mascots();
    talkBar();
    loadAccount();
    helpInit();
    root.appendChild(panel); root.appendChild(fab);
    doc.body.appendChild(root);

    x.addEventListener("click", close);
    doc.addEventListener("keydown", onKey);
    setupMic();
    voiceBtn.addEventListener("click", function () { setVoice(!voiceOn); });
    if (!("speechSynthesis" in window) && !(window.fetch && window.Audio)) voiceBtn.hidden = true;
    else setVoice(store("get", "lt_ask_voice") === "1");
    if (window.speechSynthesis) {
      refreshVoices();
      try { window.speechSynthesis.addEventListener("voiceschanged", refreshVoices); } catch (e) { window.speechSynthesis.onvoiceschanged = refreshVoices; }
      setTimeout(refreshVoices, 400); setTimeout(refreshVoices, 1500);
    }
  }

  // "Ask about this" links: <a href="#" data-ask="Why was it held?">
  doc.addEventListener("click", function (ev) {
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-ask]") : null;
    if (!t) return;
    ev.preventDefault();
    openPanel(t.getAttribute("data-ask"));
  });

  window.LowtidesAsk = {
    open: function (q) { openPanel(q ? String(q) : ""); },
    close: close,
    toggle: function () { open ? close() : openPanel(); },
    human: openHuman,
    speak: speak,
    stop: stopSpeaking,
    speaking: function () { return speakingNow; },
    // exposed so the voice logic can be checked in a real browser tab
    _voice: { cleanSpeech: cleanSpeech, splitSentences: splitSentences, scoreVoice: scoreVoice, pickBest: pickBest, groupChunks: groupChunks,
              refresh: refreshVoices, good: function () { return goodVoices.slice(); }, current: currentVoice,
              premiumOff: function () { return premiumOff; } }
  };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
