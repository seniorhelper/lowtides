"""The home hero's "protected today" figure: counted from real records only."""
import time
import unittest

from helpers import agent, config

from lowtides.guard import start_of_today
from lowtides.paypal import PayPal
from lowtides.server import App
from lowtides.store import Store


def app():
    store = Store(":memory:")
    return App(config(), store, paypal=PayPal("", "")), store


def spend(store, aid, sid, amount, status, flags=(), ts=None, decision=None):
    row = dict(id=sid, agent_id=aid, amount=amount, merchant="M", decision=decision or
               {"pending": "needs_approval", "denied": "denied"}.get(status, "approved"), status=status, flags=list(flags))
    if ts is not None:
        row["ts"] = ts
    store.add_spend(**row)


class TestProtected(unittest.TestCase):
    def test_empty_day_is_all_zero(self):
        a, _ = app()
        p = a.state()["protected"]
        self.assertEqual(p, {"stopped": 0.0, "scam_held": 0, "blocked": 0, "cancelled": 0, "loops": 0,
                             "waiting": 0, "waiting_amount": 0.0})

    def test_counts_the_runaway_story(self):
        a, store = app()
        aid = agent(store)["id"]
        spend(store, aid, "sp_paid", 12.0, "paid")
        spend(store, aid, "sp_inj", 19.0, "pending", flags=["possible_injection"])
        spend(store, aid, "sp_gpu", 480.0, "denied")
        spend(store, aid, "sp_data", 45.0, "pending")
        spend(store, aid, "sp_old", 900.0, "denied", ts=start_of_today() - 3600)  # yesterday: not counted
        store.event(aid, "loop", "Loop guard: paused it.")
        store.event(aid, "control", "something else")
        p = a.state()["protected"]
        self.assertEqual(p["stopped"], 499.0)
        self.assertEqual(p["scam_held"], 1)
        self.assertEqual(p["blocked"], 1)
        self.assertEqual(p["loops"], 1)
        self.assertEqual(p["waiting"], 1)
        self.assertEqual(p["waiting_amount"], 45.0)

    def test_approving_the_scam_removes_it(self):
        a, store = app()
        aid = agent(store)["id"]
        spend(store, aid, "sp_inj", 19.0, "pending", flags=["possible_injection"])
        store.update_spend("sp_inj", status="paid")
        self.assertEqual(a.state()["protected"]["stopped"], 0.0)

    def test_denied_injection_and_cancelled_payout(self):
        a, store = app()
        aid = agent(store)["id"]
        spend(store, aid, "sp_inj", 19.0, "denied", flags=["possible_injection"])
        spend(store, aid, "sp_back", 7.5, "cancelled")
        p = a.state()["protected"]
        self.assertEqual(p["stopped"], 26.5)
        self.assertEqual(p["scam_held"], 1)
        self.assertEqual(p["blocked"], 0)
        self.assertEqual(p["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
