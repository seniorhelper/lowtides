/**
 * lowtides-pass-check.js — check an AI agent's Lowtides Pass before you take its money.
 *
 * Dependency-free ES module for browsers and Node 18+ (uses global `fetch` and Web Crypto).
 *
 *   import { checkPass } from "./lowtides-pass-check.js";
 *   const v = await checkPass({ pass, amount: 45, merchant: "Acme Data", category: "data" });
 *   if (v.decision === "capture") captureOrder();      // capture the PayPal order
 *   else if (v.decision === "hold") keepOrderOpen();   // the agent's owner must approve first
 *   else refuseAndVoid();                              // void the authorization if you placed one
 *
 * Two checks, both every time:
 *  1. OFFLINE: the Ed25519 signature, with the public keys from
 *     <verify origin>/.well-known/lowtides-pass-keys.json (cached), when this runtime's
 *     Web Crypto supports Ed25519. A forged or edited pass is refused even if the
 *     network lies.
 *  2. ONLINE: POST /v1/pass/verify, which is the only way to see revocation (Stop all, STOP,
 *     STOP ALL, loop guard, owner revoke, rules change) and to run the purchase checks
 *     against today's real totals.
 *
 * Fails closed: if Lowtides can't be reached the decision is "hold", never "capture".
 *
 * Trust: keys always come from the server you configure (`verifyUrl`), never from the
 * pass's own `iss` or `status_url`, so a pass can't point you at a server that vouches
 * for it.
 *
 * License: AGPL-3.0, part of Lowtides (https://github.com/seniorhelper/lowtides).
 */

export const DEFAULT_VERIFY_URL = "https://lowtides.ai/v1/pass/verify";
export const PASS_TYP = "lowtides-pass+jws";
export const PASS_ALG = "EdDSA";
const KEY_TTL_MS = 5 * 60 * 1000;
const MAX_TOKEN = 16 * 1024;

/** @type {Map<string, {at: number, keys: Array<object>}>} keys URL -> cached JWKS keys */
const keyCache = new Map();

/** Forget cached public keys (for tests, or after a key rotation you know about). */
export function clearKeyCache() {
  keyCache.clear();
}

function b64urlToBytes(s) {
  if (typeof s !== "string" || !/^[A-Za-z0-9_-]*$/.test(s) || s.length % 4 === 1) {
    throw new Error("not base64url");
  }
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToJson(bytes) {
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

/**
 * Split and decode a pass without checking anything.
 * @param {string} token The pass: base64url(header).base64url(payload).base64url(signature)
 * @returns {{header: object, payload: object, signature: Uint8Array, signingInput: string}}
 * @throws {Error} when it isn't shaped like a Lowtides Pass at all.
 */
export function decodePass(token) {
  if (typeof token !== "string" || !token.trim()) throw new Error("Paste a Lowtides Pass: three parts separated by dots.");
  token = token.trim();
  if (token.length > MAX_TOKEN) throw new Error("That is too long to be a Lowtides Pass.");
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("A Lowtides Pass has three parts separated by dots. This has " + parts.length + ".");
  let header, payload, signature;
  try {
    header = bytesToJson(b64urlToBytes(parts[0]));
    payload = bytesToJson(b64urlToBytes(parts[1]));
    signature = b64urlToBytes(parts[2]);
  } catch (e) {
    throw new Error("That isn't a readable Lowtides Pass (bad base64url or JSON).");
  }
  if (!header || typeof header !== "object" || !payload || typeof payload !== "object") {
    throw new Error("That isn't a readable Lowtides Pass.");
  }
  return { header, payload, signature, signingInput: parts[0] + "." + parts[1] };
}

/**
 * The public-keys URL for a verify URL: same origin, /.well-known/lowtides-pass-keys.json.
 * @param {string} verifyUrl
 */
export function keysUrlFor(verifyUrl) {
  return new URL("/.well-known/lowtides-pass-keys.json", verifyUrl).toString();
}

async function getKeys(keysUrl, fetchFn, force) {
  const hit = keyCache.get(keysUrl);
  if (hit && !force && Date.now() - hit.at < KEY_TTL_MS) return hit.keys;
  const r = await fetchFn(keysUrl, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error("Couldn't load Lowtides public keys (HTTP " + r.status + ").");
  const j = await r.json();
  const keys = Array.isArray(j && j.keys) ? j.keys : [];
  keyCache.set(keysUrl, { at: Date.now(), keys });
  return keys;
}

let ed25519Support = null;
/** True when this runtime's Web Crypto can verify Ed25519 (Node 20+, recent browsers). */
export async function hasWebCryptoEd25519() {
  if (ed25519Support !== null) return ed25519Support;
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (!subtle) return (ed25519Support = false);
  try {
    // RFC 8032 test 1 public key: importing it proves the algorithm exists.
    const pk = new Uint8Array([215, 90, 152, 1, 130, 177, 10, 183, 213, 75, 254, 211, 201, 100, 7, 58,
      14, 225, 114, 243, 218, 166, 35, 37, 175, 2, 26, 104, 247, 7, 81, 26]);
    await subtle.importKey("raw", pk, { name: "Ed25519" }, false, ["verify"]);
    ed25519Support = true;
  } catch (e) {
    ed25519Support = false;
  }
  return ed25519Support;
}

/**
 * Check the signature and expiry offline (no revocation: that needs the online check).
 * @param {string} token
 * @param {{keysUrl?: string, verifyUrl?: string, fetch?: typeof fetch, now?: number}} [opts]
 * @returns {Promise<{available: boolean, signatureOk: (boolean|null), expired: (boolean|null), kid: (string|null), reason: (string|null)}>}
 *   `available` is false when Web Crypto has no Ed25519 or the keys couldn't be loaded;
 *   `signatureOk` is then null (unknown), and you must rely on the online check.
 */
export async function verifyOffline(token, opts = {}) {
  const fetchFn = opts.fetch || globalThis.fetch;
  const keysUrl = opts.keysUrl || keysUrlFor(opts.verifyUrl || DEFAULT_VERIFY_URL);
  const out = { available: false, signatureOk: null, expired: null, kid: null, reason: null };
  let d;
  try {
    d = decodePass(token);
  } catch (e) {
    return Object.assign(out, { available: true, signatureOk: false, reason: e.message });
  }
  out.kid = typeof d.header.kid === "string" ? d.header.kid : null;
  if (d.header.alg !== PASS_ALG) return Object.assign(out, { available: true, signatureOk: false, reason: "Wrong signing method '" + d.header.alg + "'. Lowtides Passes use EdDSA (Ed25519) only." });
  if (d.header.typ !== PASS_TYP) return Object.assign(out, { available: true, signatureOk: false, reason: "This isn't a Lowtides Pass (type '" + d.header.typ + "')." });
  const exp = Number(d.payload.exp);
  out.expired = Number.isFinite(exp) ? (opts.now || Date.now()) / 1000 >= exp : true;
  if (!(await hasWebCryptoEd25519())) return Object.assign(out, { reason: "This runtime's Web Crypto has no Ed25519, so only the online check ran." });
  let keys;
  try {
    keys = await getKeys(keysUrl, fetchFn, false);
    if (!keys.some((k) => k.kid === out.kid)) keys = await getKeys(keysUrl, fetchFn, true); // maybe a new key
  } catch (e) {
    return Object.assign(out, { reason: e.message });
  }
  const jwk = keys.find((k) => k.kid === out.kid && k.kty === "OKP" && k.crv === "Ed25519");
  if (!jwk) return Object.assign(out, { available: true, signatureOk: false, reason: "Unknown signing key '" + out.kid + "'. This pass wasn't signed by the Lowtides server you trust." });
  try {
    const key = await globalThis.crypto.subtle.importKey("raw", b64urlToBytes(jwk.x), { name: "Ed25519" }, false, ["verify"]);
    const ok = await globalThis.crypto.subtle.verify({ name: "Ed25519" }, key, d.signature, new TextEncoder().encode(d.signingInput));
    out.available = true;
    out.signatureOk = !!ok;
    if (!ok) out.reason = "The signature doesn't match. This pass was changed or forged.";
  } catch (e) {
    out.available = true;
    out.signatureOk = false;
    out.reason = "The signature couldn't be read. This pass was changed or forged.";
  }
  return out;
}

/**
 * @typedef {Object} PassVerdict
 * @property {"capture"|"hold"|"refuse"} decision What a merchant should do with the payment.
 * @property {string} label Human words for the decision ("Capture", "Hold for owner", "Refuse").
 * @property {string[]} reasons Plain-English reasons, most important first.
 * @property {boolean} valid Signed, not expired, not revoked (from the online check).
 * @property {boolean|null} signatureOk Signature result (offline if it ran, otherwise online).
 * @property {boolean} revoked
 * @property {string|null} revokedReason
 * @property {boolean} expired
 * @property {"allowed"|"needs_owner_approval"|"denied"|null} wouldAllow Lowtides' purchase decision.
 * @property {{id: number, name: string, status: string}|null} agent
 * @property {string|null} issuer The Lowtides server that governs the agent.
 * @property {string|null} expiresAt ISO time.
 * @property {string[]} limits The pass's limits in plain English.
 * @property {{available: boolean, signatureOk: (boolean|null), expired: (boolean|null), kid: (string|null), reason: (string|null)}} offline
 * @property {{reachable: boolean, status: number, body: (object|null)}} online Raw answer from POST /v1/pass/verify.
 */

/**
 * Check an AI agent's Lowtides Pass for one purchase. Use the amount from YOUR order
 * (the PayPal order total), not a number the agent tells you.
 *
 * @param {Object} args
 * @param {string} args.pass The agent's pass.
 * @param {number} [args.amount] Purchase amount in US dollars.
 * @param {string} [args.merchant] Your merchant name, as the agent's owner would list it.
 * @param {string} [args.category] What is being bought ("data", "compute", ...).
 * @param {string} [args.verifyUrl] The Lowtides server you trust. Default https://lowtides.ai/v1/pass/verify
 * @param {boolean} [args.offline=true] Also check the signature locally (when Web Crypto has Ed25519).
 * @param {typeof fetch} [args.fetch] A fetch to use (default: global fetch).
 * @param {number} [args.timeoutMs=8000] Online check timeout.
 * @returns {Promise<PassVerdict>}
 */
export async function checkPass({ pass, amount, merchant, category, verifyUrl = DEFAULT_VERIFY_URL, offline = true, fetch: fetchFn, timeoutMs = 8000 } = {}) {
  fetchFn = fetchFn || globalThis.fetch;
  const v = {
    decision: "refuse", label: "", reasons: [], valid: false, signatureOk: null, revoked: false, revokedReason: null,
    expired: false, wouldAllow: null, agent: null, issuer: null, expiresAt: null, limits: [],
    offline: { available: false, signatureOk: null, expired: null, kid: null, reason: null },
    online: { reachable: false, status: 0, body: null },
  };
  const done = (decision, reasons) => {
    v.decision = decision;
    v.label = { capture: "Capture", hold: "Hold for owner", refuse: "Refuse" }[decision];
    v.reasons = reasons.filter(Boolean);
    return v;
  };
  try {
    decodePass(pass);
  } catch (e) {
    v.signatureOk = false;
    return done("refuse", [e.message]);
  }
  if (amount !== undefined && amount !== null && !(typeof amount === "number" && Number.isFinite(amount) && amount > 0)) {
    return done("refuse", ["'amount' must be a positive number of dollars."]);
  }

  const offlineP = offline ? verifyOffline(pass, { verifyUrl, fetch: fetchFn }) : Promise.resolve(v.offline);
  const body = { pass: pass.trim() };
  if (amount !== undefined && amount !== null) body.amount = amount;
  if (merchant) body.merchant = String(merchant);
  if (category) body.category = String(category);
  const onlineP = (async () => {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    try {
      const r = await fetchFn(verifyUrl, {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body), signal: ctl ? ctl.signal : undefined,
      });
      let j = null;
      try { j = await r.json(); } catch (e) { j = null; }
      return { reachable: true, status: r.status, body: j };
    } catch (e) {
      return { reachable: false, status: 0, body: null };
    } finally {
      if (timer) clearTimeout(timer);
    }
  })();
  const [off, on] = await Promise.all([offlineP, onlineP]);
  v.offline = off;
  v.online = on;

  // A bad signature found locally wins over anything the network says.
  if (off.available && off.signatureOk === false) {
    v.signatureOk = false;
    return done("refuse", [off.reason || "The signature doesn't match."]);
  }
  if (!on.reachable || (on.status >= 500) || !on.body) {
    v.signatureOk = off.available ? off.signatureOk : null;
    return done("hold", [
      "Couldn't reach Lowtides to check revocation, so don't capture yet.",
      off.available && off.signatureOk ? "The signature checked out offline, but only the online check can see a Stop all." : off.reason,
    ]);
  }
  const j = on.body;
  if (on.status !== 200) return done("refuse", [j.error || "Lowtides refused this pass (HTTP " + on.status + ")."]);

  v.valid = !!j.valid;
  v.signatureOk = off.available ? off.signatureOk && !!j.signature_ok : !!j.signature_ok;
  v.revoked = !!j.revoked;
  v.revokedReason = j.revoked_reason || null;
  v.expired = !!j.expired;
  v.wouldAllow = j.would_allow || null;
  v.agent = j.agent || null;
  v.issuer = j.issuer || null;
  v.expiresAt = j.expires_at || null;
  v.limits = Array.isArray(j.limits_plain) ? j.limits_plain : [];
  const why = Array.isArray(j.reasons) ? j.reasons : [];

  if (!j.signature_ok) return done("refuse", why.length ? why : ["The signature doesn't match. This pass was changed or forged."]);
  if (v.revoked) return done("refuse", why.length ? why : ["REVOKED: " + (v.revokedReason || "the owner revoked this pass.")]);
  if (v.expired) return done("refuse", why);
  if (!v.valid) return done("refuse", why);
  if (v.wouldAllow === "allowed") return done("capture", why);
  if (v.wouldAllow === "needs_owner_approval") return done("hold", why);
  if (v.wouldAllow === "denied") return done("refuse", why);
  return done("hold", ["The pass is valid, but no purchase was checked. Send the amount and merchant."]);
}

export default checkPass;
