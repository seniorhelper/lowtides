"""The Leash: per-agent limits on reply size, prompt size, cost per call, and dump alerts."""
import json
import unittest

import fakes

from lowtides.spend import (DEFAULT_RULES, SpendError, agent_rules, compile_rules, describe_rules,
                            parse_rules_offline, validate_rules)

LEASH = ("max_reply_tokens", "max_input_tokens", "max_cost_per_call", "dump_alert_multiple")


def parsed(text):
    rules, _ = parse_rules_offline(text)
    return validate_rules(rules)


class TestLeashDefaults(unittest.TestCase):
    def test_defaults(self):
        self.assertIsNone(DEFAULT_RULES["max_reply_tokens"])
        self.assertIsNone(DEFAULT_RULES["max_input_tokens"])
        self.assertIsNone(DEFAULT_RULES["max_cost_per_call"])
        self.assertEqual(DEFAULT_RULES["dump_alert_multiple"], 5.0)
        r = validate_rules({})
        for k in LEASH:
            self.assertEqual(r[k], DEFAULT_RULES[k])

    def test_old_saved_rules_still_load(self):
        old = {"max_per_purchase": 40, "daily_purchase_limit": 100, "approval_over": 10,
               "blocked_categories": ["crypto"], "new_merchant_needs_approval": True, "hours": None,
               "max_purchases_per_hour": 5}
        r = agent_rules({"rules": json.dumps(old)})
        self.assertEqual(r["max_per_purchase"], 40)
        self.assertEqual(r["blocked_categories"], ["crypto"])
        for k in LEASH:
            self.assertEqual(r[k], DEFAULT_RULES[k])
        self.assertEqual(agent_rules({"rules": None})["dump_alert_multiple"], 5.0)

    def test_describe_works_on_partial_old_dict(self):
        old = dict(DEFAULT_RULES)
        for k in LEASH:
            del old[k]
        self.assertTrue(describe_rules(old))


class TestLeashValidation(unittest.TestCase):
    def test_accepts_good_values(self):
        r = validate_rules({"max_reply_tokens": 500, "max_input_tokens": "25000", "max_cost_per_call": "0.25",
                            "dump_alert_multiple": 10})
        self.assertEqual(r["max_reply_tokens"], 500)
        self.assertEqual(r["max_input_tokens"], 25000)
        self.assertEqual(r["max_cost_per_call"], 0.25)
        self.assertEqual(r["dump_alert_multiple"], 10.0)

    def test_none_and_zero(self):
        r = validate_rules({"max_reply_tokens": None, "max_input_tokens": None, "max_cost_per_call": None,
                            "dump_alert_multiple": None})
        for k in LEASH:
            self.assertIsNone(r[k])
        self.assertEqual(validate_rules({"dump_alert_multiple": 0})["dump_alert_multiple"], 0)

    def test_edges(self):
        self.assertEqual(validate_rules({"max_input_tokens": 2_000_000})["max_input_tokens"], 2_000_000)
        self.assertEqual(validate_rules({"max_reply_tokens": 1})["max_reply_tokens"], 1)
        self.assertEqual(validate_rules({"max_cost_per_call": 0.0001})["max_cost_per_call"], 0.0001)
        self.assertEqual(validate_rules({"max_cost_per_call": 10_000})["max_cost_per_call"], 10_000)
        self.assertEqual(validate_rules({"dump_alert_multiple": 1.5})["dump_alert_multiple"], 1.5)
        self.assertEqual(validate_rules({"dump_alert_multiple": 1000})["dump_alert_multiple"], 1000)

    def test_rejects_bad_values_in_plain_english(self):
        cases = [
            ({"max_reply_tokens": 0}, "between 1 and 2,000,000"),
            ({"max_reply_tokens": 2_000_001}, "between 1 and 2,000,000"),
            ({"max_input_tokens": "lots"}, "whole number of tokens"),
            ({"max_input_tokens": 12.5}, "whole number of tokens"),
            ({"max_reply_tokens": True}, "whole number of tokens"),
            ({"max_reply_tokens": [500]}, "whole number of tokens"),
            ({"max_cost_per_call": 0}, "between $0.0001 and $10,000"),
            ({"max_cost_per_call": 20_000}, "between $0.0001 and $10,000"),
            ({"max_cost_per_call": "cheap"}, "dollar amount"),
            ({"dump_alert_multiple": 1.2}, "0 (off) or between 1.5 and 1,000"),
            ({"dump_alert_multiple": 5000}, "0 (off) or between 1.5 and 1,000"),
            ({"dump_alert_multiple": -3}, "0 (off) or between 1.5 and 1,000"),
            ({"dump_alert_multiple": "big"}, "number like 5"),
        ]
        for bad, words in cases:
            with self.assertRaises(SpendError, msg=bad) as cm:
                validate_rules(bad)
            self.assertIn(words, str(cm.exception), bad)


class TestLeashDescribe(unittest.TestCase):
    def test_lines(self):
        lines = describe_rules(validate_rules({"max_reply_tokens": 500, "max_input_tokens": 25000,
                                               "max_cost_per_call": 0.25, "dump_alert_multiple": 5}))
        self.assertIn("Replies are capped at about 500 tokens (~375 words).", lines)
        self.assertIn("Prompts over about 50 pages (~25,000 tokens) are blocked.", lines)
        self.assertIn("No single AI call over $0.25.", lines)
        self.assertIn("Calls 5x bigger than usual are flagged.", lines)

    def test_small_values_and_off(self):
        lines = describe_rules(validate_rules({"max_input_tokens": 500, "max_cost_per_call": 0.005,
                                               "dump_alert_multiple": 0}))
        self.assertIn("Prompts over about 1 page (~500 tokens) are blocked.", lines)
        self.assertIn("No single AI call over $0.005.", lines)
        self.assertFalse(any("bigger than usual" in l for l in lines))
        self.assertFalse(any("Replies are capped" in l for l in describe_rules(validate_rules({}))))


class TestLeashOfflineParser(unittest.TestCase):
    def test_keep_replies_short(self):
        self.assertEqual(parsed("Keep replies short")["max_reply_tokens"], 400)

    def test_replies_under_words(self):
        self.assertEqual(parsed("replies under 300 words")["max_reply_tokens"], 399)

    def test_max_reply_tokens(self):
        self.assertEqual(parsed("max reply 800 tokens")["max_reply_tokens"], 800)

    def test_never_send_pages(self):
        self.assertEqual(parsed("never send more than 50 pages")["max_input_tokens"], 25000)

    def test_no_prompt_over_k_tokens(self):
        self.assertEqual(parsed("no prompt over 20k tokens")["max_input_tokens"], 20000)

    def test_cents_per_call(self):
        self.assertEqual(parsed("no single call over 25 cents")["max_cost_per_call"], 0.25)

    def test_dollars_per_call(self):
        self.assertEqual(parsed("$0.25 per call")["max_cost_per_call"], 0.25)

    def test_alert_multiple(self):
        self.assertEqual(parsed("alert me if a call is 10x bigger than usual")["dump_alert_multiple"], 10.0)

    def test_combined_sentence(self):
        rules, notes = parse_rules_offline("Keep replies short. Never send more than 50 pages. No single call over 25 cents.")
        r = validate_rules(rules)
        self.assertEqual(r["max_reply_tokens"], 400)
        self.assertEqual(r["max_input_tokens"], 25000)
        self.assertEqual(r["max_cost_per_call"], 0.25)
        self.assertEqual(r["dump_alert_multiple"], 5.0)
        self.assertEqual(sorted(rules), ["max_cost_per_call", "max_input_tokens", "max_reply_tokens"])
        self.assertEqual(len(notes), 3)

    def test_leash_and_purchase_rules_together(self):
        r = parsed("Max $50 a day, ask me over $20. Keep replies short. $0.10 per call.")
        self.assertEqual(r["daily_purchase_limit"], 50)
        self.assertEqual(r["approval_over"], 20)
        self.assertEqual(r["max_reply_tokens"], 400)
        self.assertEqual(r["max_cost_per_call"], 0.1)

    def test_purchase_phrasing_sets_no_leash(self):
        rules, _ = parse_rules_offline("Max $50 a day, never buy gift cards. 5 purchases an hour.")
        self.assertFalse(set(rules) & set(LEASH))


class TestLeashCompile(unittest.TestCase):
    def test_offline_compile(self):
        out = compile_rules("Keep replies short. Never send more than 50 pages. No single call over 25 cents.")
        self.assertEqual(out["source"], "offline")
        self.assertEqual(out["set"], ["max_cost_per_call", "max_input_tokens", "max_reply_tokens"])
        self.assertIn("No single AI call over $0.25.", out["plain"])

    def test_compile_with_claude(self):
        llm, url = fakes.start(fakes.FakeLLMHandler, fakes.llm_state())
        try:
            llm.state["reply"] = '{"max_reply_tokens": 400, "max_input_tokens": 25000, "max_cost_per_call": 0.25, ' \
                                 '"dump_alert_multiple": 10, "notes": ["Short replies", "50 pages max"]}'
            out = compile_rules("keep replies short, never more than 50 pages, 25 cents a call, flag 10x",
                                api_key="sk-ant-test", base_url=url)
            self.assertEqual(out["source"], "claude")
            self.assertEqual(out["rules"]["max_reply_tokens"], 400)
            self.assertEqual(out["rules"]["max_input_tokens"], 25000)
            self.assertEqual(out["rules"]["max_cost_per_call"], 0.25)
            self.assertEqual(out["rules"]["dump_alert_multiple"], 10.0)
            self.assertEqual(out["set"], sorted(LEASH))
            sent = llm.state["requests"][-1][2]
            for k in LEASH:
                self.assertIn(k, sent["system"])
        finally:
            llm.shutdown()

    def test_claude_bad_leash_value_falls_back_offline(self):
        llm, url = fakes.start(fakes.FakeLLMHandler, fakes.llm_state())
        try:
            llm.state["reply"] = '{"max_input_tokens": 99999999}'
            out = compile_rules("never send more than 50 pages", api_key="sk-ant-test", base_url=url)
            self.assertEqual(out["source"], "offline")
            self.assertEqual(out["rules"]["max_input_tokens"], 25000)
        finally:
            llm.shutdown()


if __name__ == "__main__":
    unittest.main()
