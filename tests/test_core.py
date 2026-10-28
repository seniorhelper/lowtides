import unittest

from helpers import agent, chat, config, engine

from costcop import pricing, router, trimmer
from costcop.memory import similarity


class TestPricing(unittest.TestCase):
    def test_cost_math(self):
        spec = {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}
        self.assertAlmostEqual(pricing.cost(spec, 1_000_000, 0), 2.0)
        self.assertAlmostEqual(pricing.cost(spec, 0, 1_000_000), 10.0)
        self.assertAlmostEqual(pricing.cost(spec, 0, 0, cache_read=1_000_000), 0.2)
        self.assertAlmostEqual(pricing.cost(spec, 0, 0, cache_write=20_000), 0.05)

    def test_caching_example_from_concept_doc(self):
        sonnet = {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}
        no_cache = pricing.cost(sonnet, 1_000_000)
        cached = pricing.cost(sonnet, 0, 0, cache_write=20_000) + pricing.cost(sonnet, 0, 0, cache_read=980_000)
        self.assertAlmostEqual(no_cache, 2.0)
        self.assertAlmostEqual(cached, 0.246, places=3)

    def test_money_format(self):
        self.assertEqual(pricing.money(0.004), "<$0.01")
        self.assertEqual(pricing.money(12.5), "$12.50")


class TestTrimmer(unittest.TestCase):
    def _history(self):
        big = "x" * 5000
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(5):
            msgs.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": "c%d" % i, "type": "function", "function": {"name": "f", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": "c%d" % i, "content": big})
        msgs.append({"role": "user", "content": "done?"})
        return {"model": "m", "messages": msgs}

    def test_trims_old_keeps_recent_and_pairs(self):
        body = self._history()
        new, saved = trimmer.trim(body, "openai", keep_recent=3, max_old_chars=1000)
        self.assertGreater(saved, 0)
        self.assertEqual(len(new["messages"]), len(body["messages"]))  # nothing removed
        self.assertEqual(body["messages"][3]["content"], "x" * 5000)  # original untouched
        self.assertEqual(new["messages"][-2]["content"], "x" * 5000)  # recent untouched
        old = new["messages"][3]["content"]
        self.assertTrue("CostCop" in old)
        self.assertLess(len(old), 5000)
        ids_before = [m.get("tool_call_id") for m in body["messages"]]
        ids_after = [m.get("tool_call_id") for m in new["messages"]]
        self.assertEqual(ids_before, ids_after)

    def test_anthropic_tool_result_blocks(self):
        body = {"model": "m", "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "y" * 4000}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
            {"role": "user", "content": "e"}]}
        new, saved = trimmer.trim(body, "anthropic", keep_recent=4, max_old_chars=500)
        self.assertGreater(saved, 0)
        self.assertEqual(new["messages"][2]["content"][0]["tool_use_id"], "t1")


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.r = router.Router(config())

    def test_easy_goes_cheap(self):
        order, info = self.r.plan("sim-large", chat("Translate to French: hello"))
        self.assertEqual(order[0], "sim-small")
        self.assertEqual(order[-1], "sim-large")

    def test_hard_starts_higher(self):
        order, info = self.r.plan("sim-large", chat("Debug this architecture and explain the strategy trade-offs"))
        self.assertNotEqual(order[0], "sim-small")

    def test_never_above_requested(self):
        order, _ = self.r.plan("sim-small", chat("Prove this theorem and analyze the strategy"))
        self.assertEqual(order, ["sim-small"])

    def test_routing_off(self):
        r = router.Router(config(routing="off"))
        order, _ = r.plan("sim-large", chat("Translate: hi"))
        self.assertEqual(order, ["sim-large"])

    def test_weak_answers(self):
        self.assertTrue(router.weak_answer(chat("hi"), {"text": ""}))
        self.assertTrue(router.weak_answer(chat("hi"), {"text": "I'm not sure."}))
        self.assertTrue(router.weak_answer(chat("Return JSON with name"), {"text": "name is bob"}))
        self.assertFalse(router.weak_answer(chat("Return JSON with name"), {"text": '```json\n{"name":"bob"}\n```'}))
        self.assertFalse(router.weak_answer(chat("hi"), {"text": "", "tool_calls": True}))


class TestMemorySimilarity(unittest.TestCase):
    def test_similarity(self):
        self.assertGreater(similarity("What are your store hours today?", "what are your store hours today"), 0.9)
        self.assertLess(similarity("What are your store hours?", "Cancel my order please"), 0.2)


class TestEngine(unittest.TestCase):
    def test_easy_step_saves_money(self):
        eng, store = engine()
        a = agent(store)
        status, resp, h = eng.handle("openai", chat("Summarize in one line: the meeting moved to 3pm."), a["key"])
        self.assertEqual(status, 200)
        self.assertEqual(h["X-CostCop-Model-Used"], "sim-small")
        row = store.ledger()[0]
        self.assertLess(row["cost"], row["baseline_cost"])
        self.assertIn("[simulated", resp["choices"][0]["message"]["content"])

    def test_hard_step_escalates_and_counts_failed_attempt(self):
        eng, store = engine()
        a = agent(store)
        status, resp, h = eng.handle("openai", chat("Analyze this proof please", model="sim-medium"), a["key"])
        self.assertEqual(status, 200)
        row = store.ledger()[0]
        self.assertEqual(h["X-CostCop-Model-Used"], "sim-medium")
        # graded hard -> starts at tier 2 directly (ceiling), no escalation needed
        self.assertEqual(row["escalations"], 0)
        status, resp, h = eng.handle("openai", chat("Derive the answer", model="sim-large"), a["key"],
                                     {"X-CostCop-Tier": "1"})
        row = store.ledger()[0]
        self.assertEqual(row["escalations"], 1)
        self.assertNotEqual(h["X-CostCop-Model-Used"], "sim-small")
        self.assertIn("unsure", row["note"])

    def test_repeat_is_free(self):
        eng, store = engine()
        a = agent(store)
        eng.handle("openai", chat("Translate to Spanish: thank you"), a["key"])
        status, resp, h = eng.handle("openai", chat("Translate to Spanish: thank you"), a["key"])
        self.assertEqual(h["X-CostCop-Model-Used"], "answer memory")
        row = store.ledger()[0]
        self.assertEqual(row["cost"], 0)
        self.assertGreater(row["baseline_cost"], 0)

    def test_memory_is_per_agent_and_can_be_skipped(self):
        eng, store = engine()
        a, b = agent(store, "A"), agent(store, "B")
        eng.handle("openai", chat("Translate: hi"), a["key"])
        _, _, h = eng.handle("openai", chat("Translate: hi"), b["key"])
        self.assertNotEqual(h["X-CostCop-Model-Used"], "answer memory")
        _, _, h = eng.handle("openai", chat("Translate: hi"), a["key"], {"X-CostCop-Memory": "off"})
        self.assertNotEqual(h["X-CostCop-Model-Used"], "answer memory")

    def test_unknown_key_and_model(self):
        eng, store = engine()
        a = agent(store)
        self.assertEqual(eng.handle("openai", chat("hi"), "nope")[0], 401)
        self.assertEqual(eng.handle("openai", chat("hi", model="mystery-9"), a["key"])[0], 400)

    def test_stop_button(self):
        eng, store = engine()
        a = agent(store)
        store.update_agent(a["id"], status="stopped")
        status, body, _ = eng.handle("openai", chat("hi"), a["key"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "agent_stopped")
        self.assertEqual(store.ledger()[0]["source"], "blocked")
        self.assertEqual(store.ledger()[0]["cost"], 0)

    def test_daily_budget(self):
        eng, store = engine()
        a = agent(store, daily=0.0001)
        status, body, _ = eng.handle("openai", chat("Write a full step by step essay " * 50), a["key"])
        self.assertEqual(status, 402)
        self.assertEqual(body["error"]["code"], "daily_budget")

    def test_task_budget(self):
        eng, store = engine()
        a = agent(store)
        store.add_task(a["id"], "tiny", 0.00001, tid="t-tiny")
        status, body, _ = eng.handle("openai", chat("Write a full report " * 40), a["key"], {"X-CostCop-Task": "t-tiny"})
        self.assertEqual(status, 402)
        self.assertEqual(body["error"]["code"], "task_budget")

    def test_task_auto_created_and_isolated(self):
        eng, store = engine()
        a, b = agent(store, "A"), agent(store, "B")
        eng.handle("openai", chat("hello there"), a["key"], {"X-CostCop-Task": "job-1"})
        self.assertIsNotNone(store.task("job-1"))
        status, body, _ = eng.handle("openai", chat("hi"), b["key"], {"X-CostCop-Task": "job-1"})
        self.assertEqual(status, 403)

    def test_loop_cop_pauses(self):
        eng, store = engine(config(loop_max_repeats=3, answer_memory=False))
        a = agent(store)
        codes = [eng.handle("openai", chat("Retry the upload"), a["key"])[0] for _ in range(4)]
        self.assertEqual(codes[:2], [200, 200])
        self.assertEqual(codes[2], 429)
        self.assertEqual(store.agent(a["id"])["status"], "paused")
        self.assertEqual(codes[3], 403)
        self.assertTrue(any(e["kind"] == "loop" for e in store.events()))

    def test_wallet(self):
        eng, store = engine()
        a = agent(store, wallet=True)
        status, body, _ = eng.handle("openai", chat("hi"), a["key"])
        self.assertEqual(body["error"]["code"], "wallet_empty")
        store.wallet_move(a["id"], 1.0, "test", ref="r1")
        self.assertFalse(store.wallet_move(a["id"], 1.0, "test", ref="r1"))  # idempotent
        status, body, h = eng.handle("openai", chat("Translate: hi"), a["key"])
        self.assertEqual(status, 200)
        self.assertLess(store.balance(a["id"]), 1.0)

    def test_output_cap(self):
        eng, store = engine(config(max_output_tokens_cap=100))
        a = agent(store)
        eng.handle("openai", chat("Translate: hi", max_tokens=5000), a["key"])
        self.assertIn("capped", store.ledger()[0]["note"])


if __name__ == "__main__":
    unittest.main()
