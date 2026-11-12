/* Lowtides free tools. Everything runs in the browser. No network calls. */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }
  function all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function money(n) {
    var fixed = (Math.round(n * 100) / 100).toFixed(2);
    if (fixed.slice(-3) === '.00') { fixed = fixed.slice(0, -3); }
    var parts = fixed.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return '$' + parts.join('.');
  }

  function joinList(items) {
    if (items.length === 0) { return ''; }
    if (items.length === 1) { return items[0]; }
    if (items.length === 2) { return items[0] + ' or ' + items[1]; }
    return items.slice(0, -1).join(', ') + ' or ' + items[items.length - 1];
  }

  function joinAnd(items) {
    if (items.length === 0) { return ''; }
    if (items.length === 1) { return items[0]; }
    if (items.length === 2) { return items[0] + ' and ' + items[1]; }
    return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
  }

  /* Reads a dollar field. Bad or negative input shows a message and falls back. */
  var MAX_AMOUNT = 1000000;
  function readMoney(id, fallback) {
    var input = $(id);
    var err = $(id + '-err');
    var raw = String(input.value).trim();
    var n = Number(raw);
    var msg = '';
    if (raw === '' || !isFinite(n)) {
      msg = 'Please type a number. Using ' + money(fallback) + ' for now.';
      n = fallback;
    } else if (n < 0) {
      msg = 'This cannot be less than $0. Using ' + money(fallback) + ' for now.';
      n = fallback;
    } else if (n > MAX_AMOUNT) {
      msg = 'That is very large. Using ' + money(MAX_AMOUNT) + ' for now.';
      n = MAX_AMOUNT;
    }
    n = Math.round(n * 100) / 100;
    if (err) {
      err.textContent = msg;
      err.hidden = msg === '';
    }
    if (msg) { input.setAttribute('aria-invalid', 'true'); } else { input.removeAttribute('aria-invalid'); }
    return n;
  }

  function checkedValues(name) {
    return all('input[name="' + name + '"]:checked').map(function (el) { return el.value; });
  }

  function radioValue(name, fallback) {
    var el = document.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : fallback;
  }

  function setParagraphs(box, lines) {
    while (box.firstChild) { box.removeChild(box.firstChild); }
    lines.forEach(function (line) {
      var p = document.createElement('p');
      p.textContent = line;
      box.appendChild(p);
    });
  }

  function setList(list, items, className) {
    while (list.firstChild) { list.removeChild(list.firstChild); }
    items.forEach(function (text) {
      var li = document.createElement('li');
      li.textContent = text;
      if (className) { li.className = className; }
      list.appendChild(li);
    });
  }

  /* ---------- Tool 1: shopper rules ---------- */
  function renderShopper() {
    var perItem = readMoney('t1-item', 50);
    var perDay = readMoney('t1-day', 100);
    var askOver = readMoney('t1-ask', 25);
    var never = checkedValues('t1-never');
    var mode = radioValue('t1-mode', 'assist');
    var lines = [];

    if (mode === 'off') {
      lines.push('AI shopping is off. Your AI may not buy anything for you.');
      lines.push('If you turn it on later, these limits apply: up to ' + money(perItem) + ' per item and up to ' + money(perDay) + ' per day.');
    } else {
      lines.push('Your AI may spend up to ' + money(perItem) + ' per item and up to ' + money(perDay) + ' in one day.');
      if (mode === 'assist') {
        lines.push('Mode: AI Assist. It must ask you before every purchase, no matter the price.');
      } else {
        lines.push('Mode: Full Autopilot. It may buy inside these rules without asking each time.');
        if (askOver < perItem) {
          lines.push('It must ask you first for anything over ' + money(askOver) + '.');
        } else {
          lines.push('Your "ask me first" amount (' + money(askOver) + ') is not below your per item limit, so it will not ask before buying. Lower it if you want to be asked.');
        }
      }
    }
    if (never.length) {
      lines.push('It must never buy ' + joinList(never) + '.');
    } else {
      lines.push('You have not blocked any kinds of purchase.');
    }
    if (perItem > perDay) {
      lines.push('Note: your per item limit is higher than your daily limit. The daily limit of ' + money(perDay) + ' will stop it first.');
    }
    lines.push('If a purchase breaks any rule, the answer is no and no money moves.');
    setParagraphs($('t1-plain'), lines);

    var data = {
      kind: 'lowtides-shopper-rules-draft',
      version: 1,
      signed: false,
      mode: mode,
      currency: 'USD',
      max_per_item: perItem,
      max_per_day: perDay,
      ask_first_over: askOver,
      never_buy: never
    };
    $('t1-json').textContent = JSON.stringify(data, null, 2);
  }

  /* ---------- Tool 2: store rules ---------- */
  var TRUST_TEXT = {
    any: 'any AI shopper',
    known: 'AI shoppers it already knows',
    trusted: 'trusted AI shoppers only'
  };

  /* Business type starting points. "service" types do not use the product categories. */
  var BIZ = {
    restaurant: { max: 150, status: 'open', trust: 'any', service: true,
      note: 'Bookings: AI assistants may reserve a table. A deposit is paid only after both sides say yes.' },
    salon: { max: 120, status: 'open', trust: 'known', service: true,
      note: 'Bookings: AI assistants may book an appointment. Cancellations need a person.' },
    home_services: { max: 0, status: 'review', trust: 'known', service: true,
      note: 'Requests: AI assistants may ask for a quote or a visit. A person confirms every job and price.' },
    retail: { max: 250, status: 'open', trust: 'any', service: false,
      note: 'Orders: AI shoppers may buy items in the categories below.' },
    clinic: { max: 0, status: 'review', trust: 'trusted', service: true,
      note: 'Appointments: AI assistants may ask for a visit time. A person confirms. Health details are never shared with an AI assistant here.' },
    other: null
  };
  var DEFAULT_CATS = ['home', 'tools', 'pet'];
  var bizType = '';

  /* Pre-fills the builder for a business type, then renders through the usual path. */
  function pickBusiness(type) {
    if (!Object.prototype.hasOwnProperty.call(BIZ, type)) { return; }
    bizType = type;
    all('#t2-biz button[data-biz]').forEach(function (btn) {
      btn.setAttribute('aria-pressed', btn.getAttribute('data-biz') === type ? 'true' : 'false');
    });
    var preset = BIZ[type];
    if (preset) {
      $('t2-max').value = String(preset.max);
      $('t2-trust').value = preset.trust;
      all('input[name="t2-status"]').forEach(function (el) { el.checked = el.value === preset.status; });
      var boxes = all('input[name="t2-cat"]');
      if (preset.service) {
        boxes.forEach(function (el) { el.checked = false; });
      } else if (checkedValues('t2-cat').length === 0) {
        boxes.forEach(function (el) { el.checked = DEFAULT_CATS.indexOf(el.value) !== -1; });
      }
    }
    renderStore();
  }

  function renderStore() {
    var preset = bizType ? BIZ[bizType] : null;
    var isService = !!(preset && preset.service);
    var name = String($('t2-name').value).replace(/\s+/g, ' ').trim().slice(0, 60) || 'This store';
    var maxOrder = readMoney('t2-max', 75);
    var cats = checkedValues('t2-cat');
    var trust = $('t2-trust').value;
    if (!TRUST_TEXT[trust]) { trust = 'known'; }
    var disclose = $('t2-disclose').checked;
    var status = radioValue('t2-status', 'open');
    var lines = [];

    if (status === 'closed') {
      lines.push(name + ' is closed to AI shoppers right now. Orders placed by an AI are turned down.');
    } else {
      if (status === 'open') {
        lines.push(name + ' is open to AI shoppers.');
      } else {
        lines.push(name + ' accepts AI shoppers, and a person reviews each order before it goes through.');
      }
      if (preset) { lines.push(preset.note); }
      if (cats.length) {
        lines.push((isService ? 'AI shoppers may also buy items from these categories: ' : 'AI shoppers may buy from these categories: ') + joinAnd(cats) + '. Everything else needs a person.');
      } else if (!isService) {
        lines.push('No categories are picked yet, so an AI shopper cannot buy anything here. Pick at least one.');
      }
      if (maxOrder === 0) {
        lines.push('Nothing goes through with no human. A person approves every order and price.');
      } else if (status === 'open') {
        lines.push('The largest order an AI may place with no human is ' + money(maxOrder) + '. Larger orders need a person to approve them.');
      } else {
        lines.push('The largest order an AI may ask for is ' + money(maxOrder) + '.');
      }
      lines.push('This store accepts ' + TRUST_TEXT[trust] + '.');
    }
    lines.push(disclose
      ? 'An AI shopper must say it is an AI.'
      : 'An AI shopper does not have to say it is an AI.');
    lines.push('If an order breaks any of these rules, the answer is no and no money moves.');
    setParagraphs($('t2-plain'), lines);

    var data = {
      kind: 'lowtides-store-rules-draft',
      version: 1,
      signed: false,
      store_name: name,
      business_type: bizType || 'not_set',
      ai_actions_note: (preset && status !== 'closed') ? preset.note : '',
      status: status,
      currency: 'USD',
      max_order_without_human: maxOrder,
      categories_for_ai_shoppers: cats,
      minimum_trust_level: trust,
      ai_must_disclose: disclose
    };
    $('t2-json').textContent = JSON.stringify(data, null, 2);
  }

  /* ---------- Tool 3a: stop phrase ---------- */
  var FIRST_WORDS = ['red', 'blue', 'green', 'purple', 'silver', 'golden', 'yellow', 'orange', 'quiet', 'frozen', 'copper', 'velvet'];
  var SECOND_WORDS = ['light', 'anchor', 'lantern', 'harbor', 'pelican', 'compass', 'walnut', 'tulip', 'canyon', 'marble', 'otter', 'kettle', 'meadow', 'pickle'];

  function pick(list) {
    var i;
    if (window.crypto && window.crypto.getRandomValues) {
      var buf = new Uint32Array(1);
      window.crypto.getRandomValues(buf);
      i = buf[0] % list.length;
    } else {
      i = Math.floor(Math.random() * list.length);
    }
    return list[i];
  }

  function suggestPhrase() {
    var box = $('t3-phrase');
    var current = box.textContent;
    var next = current;
    var tries = 0;
    while (next === current && tries < 10) {
      next = pick(FIRST_WORDS) + ' ' + pick(SECOND_WORDS);
      tries += 1;
    }
    box.textContent = next;
  }

  /* ---------- Tool 3b: job checker (plain rules, no AI) ---------- */
  var ACTIONS = [
    { label: 'Reserve', re: /\b(reserve|reservation|table for|hold a (table|spot|room))\b/i },
    { label: 'Schedule', re: /\b(schedule|reschedule|appointment|calendar|set up a (time|meeting)|book (a|an|my|the)? ?(appointment|meeting|visit|haircut|checkup|cleaning|table))\b/i },
    { label: 'Book travel', re: /\b(flight|flights|hotel|motel|airfare|plane|train ticket|rental car|rent a car|cruise|trip|travel|book a (room|ride|cab|taxi))\b/i },
    { label: 'Call', re: /\b(call|phone|ring|dial|voicemail)\b/i },
    { label: 'Sign up', re: /\b(sign (me )?up|signup|register|enroll|subscribe|join|create an account|open an account|free trial|membership)\b/i },
    { label: 'Cancel', re: /\b(cancel|unsubscribe|end my|stop my|close my account|refund|return)\b/i },
    { label: 'Message', re: /\b(message|text|email|e-mail|reply|write to|dm)\b/i },
    { label: 'Send a form', re: /\b(form|application|apply|fill (out|in)|submit|paperwork|claim)\b/i },
    { label: 'Share details', re: /\b(share|give (them|him|her|it) my|my (address|phone|number|birthday|date of birth|email|card|social|ssn|insurance|license|password|details|information|info))\b/i }
  ];

  var VAGUE = [
    'handle', 'sort out', 'deal with', 'take care of', 'everything', 'whatever', 'anything',
    'stuff', 'things', 'somehow', 'figure it out', 'figure out', 'as needed', 'you decide', 'all of it', 'etc'
  ];

  var WHEN_RE = /\b(today|tonight|tomorrow|morning|afternoon|evening|noon|midnight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|next week|this week|next month|this month|january|february|march|april|june|july|august|september|october|november|december|\d{1,2}(:\d{2})? ?(am|pm|a\.m\.|p\.m\.)|\d{1,2}\/\d{1,2}|\d{1,2}(st|nd|rd|th)|by (the )?end of|before \d|within \d+ (minutes|hours|days|weeks))\b/i;
  var WHO_RE = /\b(for (me|us|my|two|three|four|five|six|\d+|mom|dad|[A-Z][a-z]+)|with (my|dr\.?|doctor|[A-Z][a-z]+)|my (dentist|doctor|vet|bank|landlord|plumber|mechanic|barber|pharmacy|insurance|gym|airline|hotel|wife|husband|son|daughter|mother|father|friend)|at [A-Z][a-z]+|near me|dr\.? [A-Z])/;
  var LIMIT_RE = /(\$ ?\d|\b\d+ ?(dollars|bucks)\b|\b(under|at most|no more than|up to|budget|max|maximum|cheapest|free)\b)/i;
  var SENSITIVE_RE = /\b(password|ssn|social security|card number|bank account|pin)\b/i;

  function checkJob() {
    var text = String($('t3-job').value).replace(/\s+/g, ' ').trim();
    var verdict = $('t3-verdict');
    var actionsList = $('t3-actions');
    var tipsList = $('t3-tips');

    if (!text) {
      verdict.textContent = 'Type a job above to check it.';
      verdict.setAttribute('data-level', 'empty');
      setList(actionsList, ['Nothing yet'], 'tl-chip-none');
      setList(tipsList, ['Try something like: "Book a haircut with my barber for Saturday morning, under $40."']);
      return;
    }

    var lower = text.toLowerCase();
    var words = text.split(' ').length;
    var found = ACTIONS.filter(function (a) { return a.re.test(text); }).map(function (a) { return a.label; });
    var vagueHits = VAGUE.filter(function (w) {
      return new RegExp('\\b' + w.replace(/ /g, '\\s+') + '\\b', 'i').test(lower);
    });
    var hasWhat = found.length > 0;
    var hasWhen = WHEN_RE.test(text);
    var hasWho = WHO_RE.test(text);
    var hasLimit = LIMIT_RE.test(text);
    var tips = [];

    if (vagueHits.length) {
      tips.push('Vague words found: "' + vagueHits.join('", "') + '". Swap them for the exact thing you want done.');
    }
    if (!hasWhat) {
      tips.push('Say what to do. Start with an action word like book, call, cancel or sign up.');
    }
    if (!hasWhen) {
      tips.push('Say when. Add a day or a time, such as "Friday at 7 pm" or "by the end of the week".');
    }
    if (!hasWho) {
      tips.push('Say who or where. Name the person or place, such as "my dentist" or "for two people".');
    }
    if (!hasLimit) {
      tips.push('Add a spending limit if money is involved, such as "under $80".');
    }
    if (words < 6) {
      tips.push('This job is very short. One more sentence of detail helps.');
    }
    if (found.length > 3) {
      tips.push('This job mixes ' + found.length + ' kinds of action. Smaller jobs are easier to check. Try one job at a time.');
    }
    if (found.indexOf('Share details') !== -1) {
      tips.push('This job shares your details. Say exactly which details are okay to share, and nothing more.');
    }
    if (found.indexOf('Sign up') !== -1) {
      tips.push('Sign-ups can renew and bill again. Say whether a paid plan or a trial is okay.');
    }
    if (SENSITIVE_RE.test(text)) {
      tips.push('Do not put passwords, card numbers or ID numbers in a job.');
    }

    var missing = (hasWhat ? 0 : 1) + (hasWhen ? 0 : 1) + (hasWho ? 0 : 1);
    var level;
    if (vagueHits.length === 0 && missing === 0) {
      level = 'good';
      verdict.textContent = 'Nice and specific. This job says what to do, when, and who or where.';
      if (tips.length === 0) { tips.push('Nothing to fix. Keep your stop phrase handy in case you change your mind.'); }
    } else if (vagueHits.length === 0 && missing === 1) {
      level = 'ok';
      verdict.textContent = 'Close. One detail is missing. See the tips below.';
    } else {
      level = 'vague';
      verdict.textContent = 'This job is vague. An AI would have to guess. See the tips below.';
    }
    verdict.setAttribute('data-level', level);

    if (found.length) {
      setList(actionsList, found);
    } else {
      setList(actionsList, ['None spotted'], 'tl-chip-none');
    }
    setList(tipsList, tips);
  }

  /* ---------- Copy buttons ---------- */
  var timers = {};
  function say(statusId, text) {
    var el = $(statusId);
    if (!el) { return; }
    el.textContent = text;
    window.clearTimeout(timers[statusId]);
    timers[statusId] = window.setTimeout(function () { el.textContent = ''; }, 4000);
  }

  function textOf(el) {
    var ps = el.querySelectorAll('p');
    if (ps.length) {
      return Array.prototype.map.call(ps, function (p) { return p.textContent; }).join('\n');
    }
    return el.textContent;
  }

  function selectNode(el) {
    try {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) { /* selection is a nicety only */ }
  }

  function copyFrom(button) {
    var source = $(button.getAttribute('data-copy'));
    var statusId = button.getAttribute('data-status');
    if (!source) { return; }
    var text = textOf(source);
    var failed = function () {
      selectNode(source);
      say(statusId, 'Copy did not work here. The text is selected, so you can copy it by hand.');
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        say(statusId, 'Copied.');
      }, failed);
    } else {
      failed();
    }
  }

  /* ---------- Wire up ---------- */
  function init() {
    if (!$('t1-form')) { return; }
    ['input', 'change'].forEach(function (evt) {
      $('t1-form').addEventListener(evt, renderShopper);
      $('t2-form').addEventListener(evt, renderStore);
    });
    $('t1-form').addEventListener('submit', function (e) { e.preventDefault(); });
    $('t2-form').addEventListener('submit', function (e) { e.preventDefault(); });
    all('#t2-biz button[data-biz]').forEach(function (btn) {
      btn.addEventListener('click', function () { pickBusiness(btn.getAttribute('data-biz')); });
    });
    $('t3-suggest').addEventListener('click', suggestPhrase);
    $('t3-job').addEventListener('input', checkJob);
    all('button[data-copy]').forEach(function (btn) {
      btn.addEventListener('click', function () { copyFrom(btn); });
    });
    renderShopper();
    renderStore();
    checkJob();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
