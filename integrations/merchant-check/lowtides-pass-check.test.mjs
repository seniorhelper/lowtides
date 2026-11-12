// node --test integrations/merchant-check/lowtides-pass-check.test.mjs
// Self-contained: makes its own Ed25519 key and a fake Lowtides (a fake fetch).
import test from "node:test";
import assert from "node:assert/strict";
import { checkPass, verifyOffline, decodePass, clearKeyCache, keysUrlFor, hasWebCryptoEd25519 } from "./lowtides-pass-check.js";

const VERIFY = "https://lowtides.test/v1/pass/verify";
const enc = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");

const kp = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const rawPub = new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey));
const KID = "lt-pass-test";
const JWKS = { keys: [{ kty: "OKP", crv: "Ed25519", x: Buffer.from(rawPub).toString("base64url"), kid: KID, alg: "EdDSA" }] };

async function makePass(payload = {}, header = {}) {
  const h = enc({ alg: "EdDSA", typ: "lowtides-pass+jws", kid: KID, ...header });
  const p = enc({ v: 1, iss: "https://lowtides.test", sub: "agent:1", exp: Math.floor(Date.now() / 1000) + 3600, iat: 0, ...payload });
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, kp.privateKey, new TextEncoder().encode(h + "." + p)));
  return h + "." + p + "." + Buffer.from(sig).toString("base64url");
}

function fakeFetch(answer, calls = []) {
  return async (url, opts = {}) => {
    calls.push(url);
    if (url === keysUrlFor(VERIFY)) return new Response(JSON.stringify(JWKS), { status: 200 });
    if (url === VERIFY) {
      if (answer === "down") throw new TypeError("fetch failed");
      const body = JSON.parse(opts.body);
      const a = typeof answer === "function" ? answer(body) : answer;
      return new Response(JSON.stringify(a.body), { status: a.status || 200 });
    }
    return new Response("{}", { status: 404 });
  };
}
const ok = (would_allow, extra = {}) => ({ body: { valid: true, signature_ok: true, revoked: false, expired: false, would_allow,
  reasons: ["because"], agent: { id: 1, name: "Bot", status: "on" }, issuer: "https://lowtides.test", limits_plain: ["Up to $60.00 per purchase."], ...extra } });

test("runtime has Ed25519 Web Crypto", async () => assert.equal(await hasWebCryptoEd25519(), true));

test("allowed -> capture, sends the purchase, checks offline too", async () => {
  clearKeyCache();
  let sent;
  const v = await checkPass({ pass: await makePass(), amount: 12, merchant: "Acme Data", category: "data", verifyUrl: VERIFY,
    fetch: fakeFetch((b) => { sent = b; return ok("allowed"); }) });
  assert.equal(v.decision, "capture");
  assert.equal(v.label, "Capture");
  assert.equal(v.offline.available, true);
  assert.equal(v.offline.signatureOk, true);
  assert.equal(v.signatureOk, true);
  assert.deepEqual([sent.amount, sent.merchant, sent.category], [12, "Acme Data", "data"]);
});

test("needs approval -> hold; denied -> refuse; revoked -> refuse", async () => {
  const pass = await makePass();
  assert.equal((await checkPass({ pass, amount: 45, verifyUrl: VERIFY, fetch: fakeFetch(ok("needs_owner_approval")) })).decision, "hold");
  assert.equal((await checkPass({ pass, amount: 900, verifyUrl: VERIFY, fetch: fakeFetch(ok("denied")) })).decision, "refuse");
  const r = await checkPass({ pass, amount: 5, verifyUrl: VERIFY, fetch: fakeFetch(ok("denied", { valid: false, revoked: true, revoked_reason: "KILL", reasons: ["REVOKED: KILL"] })) });
  assert.equal(r.decision, "refuse");
  assert.equal(r.revoked, true);
  assert.deepEqual(r.reasons, ["REVOKED: KILL"]);
});

test("tampered pass is refused offline even if the network says allowed", async () => {
  const pass = await makePass({ limits: { max_per_purchase: 60 } });
  const [h, , s] = pass.split(".");
  const forged = h + "." + enc({ sub: "agent:1", exp: 9999999999, limits: { max_per_purchase: 99999 } }) + "." + s;
  const v = await checkPass({ pass: forged, amount: 5, verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed")) });
  assert.equal(v.decision, "refuse");
  assert.equal(v.signatureOk, false);
  assert.match(v.reasons[0], /signature/i);
});

test("alg none, unknown key and garbage are refused", async () => {
  const none = await makePass({}, { alg: "none" });
  assert.equal((await verifyOffline(none, { verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed")) })).signatureOk, false);
  const other = await makePass({}, { kid: "someone-else" });
  assert.equal((await checkPass({ pass: other, amount: 1, verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed")) })).decision, "refuse");
  assert.equal((await checkPass({ pass: "not-a-pass", amount: 1, verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed")) })).decision, "refuse");
  assert.throws(() => decodePass("a.b"));
});

test("Lowtides unreachable -> hold (fails closed), never capture", async () => {
  const v = await checkPass({ pass: await makePass(), amount: 5, verifyUrl: VERIFY, fetch: fakeFetch("down") });
  assert.equal(v.decision, "hold");
  assert.equal(v.online.reachable, false);
  assert.equal(v.offline.signatureOk, true);
});

test("offline off -> online only; keys are cached", async () => {
  clearKeyCache();
  const calls = [];
  const pass = await makePass();
  await checkPass({ pass, amount: 5, verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed"), calls) });
  await checkPass({ pass, amount: 5, verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed"), calls) });
  assert.equal(calls.filter((u) => u.endsWith("lowtides-pass-keys.json")).length, 1);
  const v = await checkPass({ pass, amount: 5, verifyUrl: VERIFY, offline: false, fetch: fakeFetch(ok("allowed")) });
  assert.equal(v.offline.available, false);
  assert.equal(v.decision, "capture");
});

test("bad amount and 400 answers are refused; no amount -> hold", async () => {
  const pass = await makePass();
  assert.equal((await checkPass({ pass, amount: -3, verifyUrl: VERIFY, fetch: fakeFetch(ok("allowed")) })).decision, "refuse");
  assert.equal((await checkPass({ pass, amount: 3, verifyUrl: VERIFY, fetch: fakeFetch({ status: 400, body: { error: "nope" } }) })).decision, "refuse");
  assert.equal((await checkPass({ pass, verifyUrl: VERIFY, fetch: fakeFetch(ok(null)) })).decision, "hold");
});
