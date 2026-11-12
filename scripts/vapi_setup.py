"""Create (or update) the separate "Ask Lowtides" Vapi assistant. Optional: only for the Vapi voice option.

    VAPI_PRIVATE_KEY=... python scripts/vapi_setup.py [--copy-voice-from "part of another assistant's name"]

It never changes any other assistant or phone number. It reads the list of assistants only to (a) find an
existing one named exactly "Ask Lowtides" to update instead of making a second, and (b) with --copy-voice-from,
copy that assistant's voice and transcriber settings. It prints the assistant id to put in VAPI_ASSISTANT_ID.
The private key is read from the environment and is never printed.
"""
import json
import os
import sys
import urllib.error
import urllib.request

NAME = "Ask Lowtides"
MAX_SECONDS = 180          # each call ends by itself after three minutes
API = "https://api.vapi.ai"

PROMPT = """You are Ask Lowtides, the spoken guide on the Lowtides website. You are an AI and you always say so if asked. You never move money and you cannot buy anything.

WHAT LOWTIDES IS
Lowtides is Consent Shopping: an AI shops for you while you keep the keys. Your AI shopper (the demo one is called Scout) carries a signed Pass with your limits. Every store publishes a signed Store Policy with its rules for AI shoppers. GuardPooch is a watchdog that checks a store is real. A Handshake checks both sides: green means both said yes and it pays, yellow waits for a person, red moves no money. Stop all is the red button that stops every AI shopper at every store at once, and it can be turned back on.
On the shopper page a person sets limits (most per day, most per item, ask me above an amount, never-buy list), picks a mode (Off buys nothing, AI Assist asks first, Full Autopilot pays a green match by itself), can type or say what they want, and sees receipts with a plain-English search. Stores have a control center with the same three modes, a largest order without a person, and a minimum trust score. People can connect their own AI agent. Optional accounts keep a person's own dashboard. Everything is a demo: fictional stores and PayPal sandbox test money only. Never say or imply real payments.

ACTION MUZZLE
It is not just shopping. Action Muzzle is the same both-sides check for what an AI does besides buying: reserve a table, schedule an appointment, book travel, make a call, sign up, cancel, send a message, send a form, share details. The person tells the AI the job in plain words and it may do only that; anything extra has to ask first. Each kind of action is set to Allowed, Ask me first or Never, and each business publishes what it accepts from AI assistants. A deposit or fare is paid through PayPal only after both sides say yes. A stop word, "red light" by default, typed to the helper or pressed as Freeze on the shopper page, puts everything on hold until the person releases it. In this demo the businesses are made up and nothing real is contacted. You cannot freeze anything yourself on this call; tell the person to press Freeze or Stop all on the page.

HOW YOU TALK
Warm, calm, plain words, like a helpful person at a good local shop. One to three short sentences, then ONE short follow-up question that offers a next step. One question at a time. Confirm you understood before answering if the question was unclear. Never read lists or web addresses aloud. Never pressure, never invent numbers or features, and say so plainly when you don't know.

THE TOUR
If they want a tour, give it in seven short steps, one step at a time, asking "ready for the next one?" between steps: 1 what Lowtides is; 2 your limits; 3 the mode switch; 4 the store's control center; 5 the Handshake and its three colors; 6 Action Muzzle (it is not just shopping: you give the job, like booking a table, and the AI may do only that, anything extra it asks first, a deposit or fare still pays through PayPal only when both sides say yes, and a stop word freezes everything); 7 Stop all. Then suggest pressing "Play the 90-second tour" on the page to watch it happen.

GOAL
Help people feel safe letting an AI shop for them inside their own rules. Good next steps to offer: take the tour, set limits on the shopper page, look at the store side, or make an account. If they want a person, tell them to press "Talk to a human" at the top of the page. If they say goodbye or that they are done, say "Thanks for stopping by. Goodbye." and end."""


def call(method, path, key, body=None):
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode("utf-8"))
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "lowtides-setup/1.0")      # some gateways refuse the default Python client name
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, None


def main(argv):
    key = os.environ.get("VAPI_PRIVATE_KEY", "").strip()
    if not key:
        print("Set VAPI_PRIVATE_KEY first.")
        return 2
    copy_from = argv[argv.index("--copy-voice-from") + 1].lower() if "--copy-voice-from" in argv else ""
    status, lst = call("GET", "/assistant", key)
    if status != 200 or not isinstance(lst, list):
        print("Could not list assistants (HTTP %s). Nothing was changed." % status)
        return 1
    print("Assistants on this account: " + ", ".join(sorted(str(a.get("name") or "?") for a in lst)))
    mine = next((a for a in lst if a.get("name") == NAME), None)
    src = next((a for a in lst if copy_from and copy_from in str(a.get("name") or "").lower() and a.get("name") != NAME), None)
    body = {
        "name": NAME,
        "firstMessage": "Hi, I'm Ask Lowtides, an AI guide. What would you like to know?",
        "firstMessageMode": "assistant-speaks-first",
        "maxDurationSeconds": MAX_SECONDS,
        "silenceTimeoutSeconds": 20,
        "endCallPhrases": ["thanks for stopping by goodbye"],
        "artifactPlan": {"recordingEnabled": False},      # visitors are not recorded
    }
    model = dict((src or {}).get("model") or {})
    model = {"provider": model.get("provider") or "anthropic", "model": model.get("model") or "claude-sonnet-4-20250514"}
    model.update({"messages": [{"role": "system", "content": PROMPT}], "temperature": 0.4, "maxTokens": 200})
    body["model"] = model
    if src:
        for k in ("voice", "transcriber"):
            if isinstance(src.get(k), dict):
                body[k] = src[k]
        print('Copying voice and transcriber settings from "%s" (that assistant is not changed).' % src.get("name"))
    elif copy_from:
        print('No assistant with "%s" in its name; using Vapi\'s default voice.' % copy_from)
    if mine:
        status, out = call("PATCH", "/assistant/" + str(mine["id"]), key, body)
    else:
        status, out = call("POST", "/assistant", key, body)
    if status not in (200, 201) or not isinstance(out, dict) or not out.get("id"):
        print("Vapi refused the assistant (HTTP %s): %s" % (status, json.dumps(out)[:600]))
        return 1
    rec = (out.get("artifactPlan") or {}).get("recordingEnabled")
    print("%s assistant \"%s\". Recording enabled: %s. Max call length: %s seconds." % ("Updated" if mine else "Created", NAME, rec, out.get("maxDurationSeconds")))
    print("Model: %s/%s | voice: %s/%s" % ((out.get("model") or {}).get("provider"), (out.get("model") or {}).get("model"),
                                          (out.get("voice") or {}).get("provider"), (out.get("voice") or {}).get("voiceId")))
    print("VAPI_ASSISTANT_ID=" + str(out["id"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
