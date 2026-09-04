"""The tests that matter are the failure paths.

A board that works when every model answers is easy. The whole value of this thing is what it
does when a member is throttled, when the seam sees a key, or when the pool has no
independent members left -- so that is what most of these check.
"""
import unittest

from freeboard import board, budget, catalogue, redact, seats
from freeboard.transport import Answer, Failure, OfflineTransport, OpenRouterTransport


def model(mid, ctx=100000, out=8000, params=("max_tokens", "temperature"), mods=("text",)):
    return {"id": mid, "name": mid, "family": mid.split("/")[0], "context_length": ctx,
            "max_completion_tokens": out, "is_moderated": False,
            "input_modalities": list(mods), "supported_parameters": sorted(params)}


POOL = [
    model("alpha/one:free", ctx=200000, params=("max_tokens", "temperature", "response_format")),
    model("alpha/two:free", ctx=900000, params=("max_tokens", "temperature", "response_format")),
    model("beta/one:free", ctx=150000),
    model("gamma/one:free", ctx=300000, params=("max_tokens", "temperature", "structured_outputs")),
    model("delta/one:free", ctx=50000, out=2000),
    model("epsilon/one:free", ctx=400000),
]


class Seating(unittest.TestCase):
    def test_one_seat_per_family(self):
        """Two checkpoints of one family is not a jury, it is one model with a stutter."""
        b = seats.seat(POOL, size=6)
        fams = [m["family"] for m in b]
        self.assertEqual(len(fams), len(set(fams)))

    def test_never_pads_the_board(self):
        """Asking for more seats than there are families gives fewer seats, not duplicates."""
        b = seats.seat(POOL, size=99)
        self.assertEqual(len(b), len({m["family"] for m in POOL}))

    def test_quorum_needs_three(self):
        with self.assertRaises(seats.NoQuorum):
            seats.quorum(seats.seat(POOL, size=2))

    def test_chair_is_not_a_member(self):
        b = seats.seat(POOL, size=3)
        ch = seats.chair(POOL, b)
        self.assertNotIn(ch["id"], {m["id"] for m in b})

    def test_seat_respects_asymmetric_limits(self):
        """A model with room for the prompt can still refuse the output length."""
        b = seats.seat(POOL, size=9, completion_tokens=5000)
        self.assertNotIn("delta/one:free", {m["id"] for m in b})   # out cap is 2000

    def test_non_deliberative_models_are_not_seated(self):
        pool = POOL + [model("openrouter/free"), model("google/lyria-3-pro-preview")]
        ids = {m["id"] for m in seats.seat(pool, size=99)}
        self.assertNotIn("openrouter/free", ids)
        self.assertNotIn("google/lyria-3-pro-preview", ids)

    def test_the_free_variant_of_a_barred_model_is_also_barred(self):
        """Exclusions are about what a model IS, not which variant you asked for.

        `nvidia/nemotron-3.5-content-safety:free` is a guardrail classifier. It slipped past
        the list because the list held the bare id and the catalogue ships the `:free` one.
        """
        pool = POOL + [model("nvidia/nemotron-3.5-content-safety:free"),
                       model("openrouter/free:free"),
                       model("google/lyria-3-clip-preview:free")]
        ids = {m["id"] for m in seats.seat(pool, size=99)}
        self.assertNotIn("nvidia/nemotron-3.5-content-safety:free", ids)
        self.assertNotIn("openrouter/free:free", ids)
        self.assertNotIn("google/lyria-3-clip-preview:free", ids)


class Budget(unittest.TestCase):
    def test_the_documented_numbers(self):
        self.assertEqual(budget.Budget(0).per_day, 50)
        self.assertEqual(budget.Budget(9.99).per_day, 50)
        self.assertEqual(budget.Budget(10).per_day, 1000)
        self.assertEqual(budget.Budget(0).per_minute, 20)

    def test_session_cost(self):
        # 5 answers + 5 blind rankings + 1 chair
        self.assertEqual(budget.requests_per_session(5, peer_review=True), 11)
        self.assertEqual(budget.requests_per_session(5, peer_review=False), 6)
        self.assertEqual(budget.requests_per_session(5, chair=False, peer_review=False), 5)

    def test_ten_dollars_is_a_twentyfold_lever(self):
        u = budget.upgrade(members=5, peer_review=True)
        self.assertEqual(u["before"]["sessions_per_day"], 4)
        self.assertEqual(u["after"]["sessions_per_day"], 90)
        self.assertGreater(u["multiplier"], 20)

    def test_a_huge_board_busts_the_minute(self):
        self.assertTrue(budget.sessions(budget.Budget(10), members=30)["burst_exceeds_rpm"])


class Seam(unittest.TestCase):
    def test_catches_keys_addresses_and_paths(self):
        for text in [
            "key is sk-or-v1-0123456789abcdef0123456789",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_0123456789012345678901234567890123",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "ssh to 100.64.0.1",
            "server at 192.168.1.10",
            "see ~/.ssh/id_ed25519",
            "config in /Users/someone/app/.env",
            "OPENROUTER_API_KEY=abc12345xyz",
            "Authorization: Bearer abc123def456",
        ]:
            with self.subTest(text=text):
                self.assertTrue(redact.scan(text), f"missed: {text}")
                with self.assertRaises(redact.Refused):
                    redact.check(text)

    def test_ordinary_questions_pass(self):
        for text in ["Should we rewrite the parser this quarter?",
                     "Is 10.x a good version number for the next release?",
                     "Compare Postgres and SQLite for a 50 GB analytics table."]:
            with self.subTest(text=text):
                self.assertEqual(redact.check(text), text)

    def test_the_report_does_not_repeat_the_secret(self):
        secret = "sk-or-v1-0123456789abcdefSECRETTAIL"
        found = redact.scan(f"key {secret}")
        self.assertTrue(found)
        for f in found:
            self.assertNotIn(secret, str(f))

    def test_the_seam_runs_before_anything_is_sent(self):
        t = OfflineTransport()
        with self.assertRaises(redact.Refused):
            board.ask("deploy with sk-or-v1-0123456789abcdef0123", transport=t, models=POOL)
        self.assertEqual(t.calls, [], "the seam must refuse BEFORE the first request")


class Transport(unittest.TestCase):
    def test_only_supported_parameters_are_sent(self):
        """OpenRouter drops unsupported parameters silently -- ask for JSON, get prose, get a 200."""
        with_json = OpenRouterTransport._payload(POOL[0], [], True, 100, 0.5)
        without = OpenRouterTransport._payload(POOL[2], [], True, 100, 0.5)
        self.assertIn("response_format", with_json)
        self.assertNotIn("response_format", without)

    def test_structured_outputs_also_counts_as_json_capable(self):
        self.assertTrue(catalogue.speaks_json(POOL[3]))
        self.assertFalse(catalogue.speaks_json(POOL[2]))

    def test_retry_after_is_preferred_over_backoff(self):
        self.assertEqual(OpenRouterTransport._backoff(3, 7.5), 7.5)
        self.assertLessEqual(OpenRouterTransport._backoff(3, None), 30.0)
        self.assertEqual(OpenRouterTransport._backoff(0, 999), 60.0)   # clamped

    def test_a_failure_is_never_an_answer(self):
        f = Failure("m", "rate limited", status=429)
        self.assertFalse(f.ok)
        self.assertFalse(hasattr(f, "text"))
        self.assertTrue(Answer("m", "hi").ok)


class BoardSession(unittest.TestCase):
    def test_a_throttled_member_is_not_a_vote(self):
        t = OfflineTransport(fail={"alpha/two:free", "gamma/one:free"})
        s = board.ask("ship it?", transport=t, models=POOL, size=5)
        self.assertEqual(len(s.failures), 2)
        voters = {a.model for a in s.answers}
        self.assertNotIn("alpha/two:free", voters)
        self.assertIn("DID NOT VOTE", s.report())
        self.assertIn("not counted as agreement", s.report())

    def test_too_many_failures_is_no_quorum_not_a_decision(self):
        t = OfflineTransport(fail={m["id"] for m in POOL[:4]})
        s = board.ask("ship it?", transport=t, models=POOL, size=5, minimum=3)
        self.assertIsNotNone(s.no_quorum)
        self.assertIsNone(s.decision)
        self.assertIn("NO QUORUM", s.report())

    def test_the_ranking_round_is_blind(self):
        """No model id may appear in the text sent to a ranker."""
        t = OfflineTransport()
        board.ask("ship it?", transport=t, models=POOL, size=4, peer_review=True)
        ids = [m["id"] for m in POOL]
        ranking_prompts = [p for _, p in t.calls if "Rank them best to worst" in p]
        self.assertTrue(ranking_prompts)
        for p in ranking_prompts:
            for mid in ids:
                self.assertNotIn(mid, p, "a model id leaked into the blind ranking round")
            self.assertIn("Member A", p)

    def test_members_answer_independently(self):
        """No member may see another member's answer in round one."""
        t = OfflineTransport()
        board.ask("ship it?", transport=t, models=POOL, size=4, peer_review=False)
        answer_prompts = [p for _, p in t.calls if "You are one member of an independent board.\n" in p]
        for p in answer_prompts:
            self.assertNotIn("Member A", p)
            self.assertNotIn("--- ", p)

    def test_a_silent_member_does_not_get_to_rank(self):
        t = OfflineTransport(fail={"alpha/two:free"})
        s = board.ask("ship it?", transport=t, models=POOL, size=5, peer_review=True)
        self.assertLessEqual(len(s.rankings), len(s.answers))
        self.assertNotIn("alpha/two:free", {r.model for r in s.rankings})

    def test_labels_map_back_for_the_audit(self):
        s = board.ask("ship it?", transport=OfflineTransport(), models=POOL, size=4)
        self.assertEqual(len(s.labels), len(s.answers))
        self.assertEqual(set(s.labels.values()), {a.model for a in s.answers})


class Catalogue(unittest.TestCase):
    def test_snapshot_loads_and_is_all_free(self):
        c = catalogue.snapshot()
        self.assertGreater(len(c["models"]), 0)
        for m in c["models"]:
            self.assertTrue(m["id"])
            self.assertIn("family", m)

    def test_offline_load_never_touches_the_network(self):
        c = catalogue.load(live=False)
        self.assertIn("snapshot", c["origin"])

    def test_asymmetric_fit(self):
        m = model("x/y:free", ctx=1000, out=100)
        self.assertTrue(catalogue.fits(m, 500, 100)[0])
        self.assertFalse(catalogue.fits(m, 500, 101)[0])    # output cap, not context
        self.assertFalse(catalogue.fits(m, 950, 100)[0])    # context


if __name__ == "__main__":
    unittest.main(verbosity=2)
