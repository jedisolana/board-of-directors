"""The tests that matter are the failure paths.

A board that works when every model answers is easy. The whole value of this thing is what it
does when a member is throttled, when the seam sees a key, or when the pool has no
independent members left -- so that is what most of these check.
"""
import contextlib
import datetime
import importlib
import inspect
import os
import re
import shutil
import tempfile
import threading
import time
import typing
import unittest

from boardofdirectors import (
    atomic,
    board,
    budget,
    catalogue,
    config,
    cost,
    openai_api,
    patch,
    redact,
    seats,
    server,
    sessions,
    truecount,
    usage,
)
from boardofdirectors.transport import Answer, Failure, OfflineTransport, OpenRouterTransport


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
        pool = [*POOL, model("openrouter/free"), model("google/lyria-3-pro-preview")]
        ids = {m["id"] for m in seats.seat(pool, size=99)}
        self.assertNotIn("openrouter/free", ids)
        self.assertNotIn("google/lyria-3-pro-preview", ids)

    def test_the_free_variant_of_a_barred_model_is_also_barred(self):
        """Exclusions are about what a model IS, not which variant you asked for.

        `nvidia/nemotron-3.5-content-safety:free` is a guardrail classifier. It slipped past
        the list because the list held the bare id and the catalogue ships the `:free` one.
        """
        pool = [*POOL, model("nvidia/nemotron-3.5-content-safety:free"), model("openrouter/free:free"), model("google/lyria-3-clip-preview:free")]
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

    def test_ordinary_code_does_not_trip_the_secret_rule(self):
        """`budget_tokens = ...` was reported as a leaked credential.

        The rule matched any identifier containing TOKEN followed by eight characters, which
        is most of a codebase. A seam that cries wolf on ordinary code is one people learn to
        click past, and the "send anyway" tick then means nothing.
        """
        for line in ("budget_tokens = codebase.audit_message(sc, budget)",
                     "max_tokens = 1024",
                     "prompt_tokens += len(chunk)",
                     "self.token_count = compute(a, b)",
                     "TOKENS_PER_CHAR = CHARS_PER_TOKEN // 4",
                     "secret_sauce = compute_things(x)"):
            with self.subTest(line=line):
                self.assertEqual(redact.scan(line), [], line)

    def test_a_real_assigned_secret_still_fires(self):
        for line in ('API_KEY = "sk-live-9f2a8b3c1d4e"',
                     'password: "hunter2placeholder"',
                     "OPENROUTER_API_KEY=abc123def456ghij",
                     "AUTH_TOKEN = eyJhbGciOiJIUzI1NiJ9abcdefgh"):
            with self.subTest(line=line[:20]):
                self.assertTrue(redact.scan(line), line)

    def test_the_seam_is_quiet_on_this_codebase(self):
        """The one repo we can check exhaustively: our own, minus its deliberate fixtures."""
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "boardofdirectors")
        noisy = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name)) as fh:
                for f in redact.scan(fh.read()):
                    if f.rule not in ("dotenv path", "home path"):
                        noisy.append(f"{name}: {f}")
        self.assertEqual(noisy, [])

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
        # Clamped to 15s, not 60s: a Retry-After longer than the board's own deadline is a
        # sleep nobody is waiting through. Five other members are answering.
        self.assertEqual(OpenRouterTransport._backoff(0, 999), 15.0)

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


class AChosenBoardIsHonoured(unittest.TestCase):
    """A hand-picked board was silently replaced by the automatic pick.

    `ask_in_context` re-seated unconditionally, so choosing five models got you whichever
    five the ranking preferred - and the caller was handed back the list it had ASKED for,
    so the substitution was invisible from both the API and the screen. The first test
    written for this asked the response which members it wanted rather than which it used,
    and passed while the bug was live.
    """

    def test_the_chosen_members_are_the_ones_that_answer(self):
        want = [POOL[2], POOL[4], POOL[5]]      # deliberately NOT what seat() would rank first
        t = OfflineTransport()
        s = board.ask_in_context("go", transport=t, models=POOL, members=want)
        self.assertEqual([m["id"] for m in s.members], [m["id"] for m in want])
        asked = {mid for mid, _ in t.calls}
        for m in want:
            self.assertIn(m["id"], asked, "a chosen member was never asked")

    def test_the_automatic_pick_would_have_differed(self):
        """Guards the test above: if seat() happened to agree, it would prove nothing."""
        want = [POOL[2], POOL[4], POOL[5]]
        auto = seats.seat(POOL, size=3)
        self.assertNotEqual([m["id"] for m in want], [m["id"] for m in auto])

    def test_no_members_given_still_auto_seats(self):
        s = board.ask_in_context("go", transport=OfflineTransport(), models=POOL, size=4)
        self.assertEqual(len(s.members), 4)


class TheChairCanFail(unittest.TestCase):
    """Three good answers were thrown away because one model refused to chair.

    A real run: three members answered at length, two were rate limited, and the chair
    returned 403 "only available on agentic harnesses" - so the session reported NO QUORUM
    and the answers went unsynthesised. One model's refusal spoke for the whole board, which
    is the same mistake as counting a throttled member as a vote.
    """

    def test_it_tries_another_chair(self):
        first = seats.chair(POOL, seats.seat(POOL, size=3))
        t = OfflineTransport(fail={first["id"]})
        s = board.ask_in_context("go", transport=t, models=POOL, size=3)
        self.assertIsNone(s.no_quorum)
        self.assertIsNotNone(s.decision)
        self.assertNotEqual(s.chair_model["id"], first["id"])
        self.assertEqual(s.chair_failures[0]["model"], first["id"])

    def test_when_no_model_can_chair_the_answers_survive_the_message(self):
        t = OfflineTransport(fail={m["id"] for m in POOL[3:]})
        s = board.ask_in_context("go", transport=t, models=POOL, size=3)
        if s.no_quorum:
            self.assertIn("answered", s.no_quorum)
            self.assertGreaterEqual(len(s.answers), 1)

    def test_the_report_names_the_chair_that_actually_wrote_it(self):
        first = seats.chair(POOL, seats.seat(POOL, size=3))
        s = board.ask_in_context("go", transport=OfflineTransport(fail={first["id"]}),
                                 models=POOL, size=3)
        self.assertIn(s.chair_model["id"], s.report())


class TwoKindsOfQuestion(unittest.TestCase):
    """"Should we build it?" and "build it" need opposite prompts.

    The board shipped with only the first, so asking it to build something got four models
    solemnly taking a position on whether building it was wise. They answered exactly what
    they were asked; what they were asked was wrong.
    """

    def _prompts(self, kind):
        t = OfflineTransport()
        board.ask("build me a parser", transport=t, models=POOL, size=4, kind=kind)
        return [p for _, p in t.calls]

    def test_make_asks_for_the_thing_not_a_position(self):
        first = self._prompts("make")[0]
        self.assertIn("Do the task", first)
        self.assertNotIn("your position", first)

    def test_decide_still_asks_for_a_position(self):
        first = self._prompts("decide")[0]
        self.assertIn("your position", first)
        self.assertNotIn("Do the task", first)

    def test_the_make_chair_delivers_work_not_a_review(self):
        chair = self._prompts("make")[-1]
        self.assertIn("DELIVER THE FINISHED WORK", chair)
        self.assertIn("Do not review the attempts", chair)

    def test_the_make_ranking_prefers_doing_over_describing(self):
        rank = next(p for p in self._prompts("make") if "Rank them" in p)
        self.assertIn("ACTUALLY DOES THE TASK", rank)

    def test_the_session_records_which_kind_it_was(self):
        s = board.ask("build a parser", transport=OfflineTransport(), models=POOL,
                      size=4, kind="make")
        self.assertEqual(s.kind, "make")
        self.assertIn("RESULT (chair)", s.report())
        self.assertNotIn("DECISION (chair)", s.report())

    def test_the_guess_separates_tasks_from_questions(self):
        """A DECISION is phrased as a question. Everything else is work.

        The first version listed task verbs and missed four in five: "draw a picture using
        characters" went to DECIDE, so a board asked to draw a dollar sign spent eleven
        requests voting on whether drawing it was PERMITTED and resolved that it "is
        approved". A verb list can never be complete and every word missing from it fails
        that way, so the test is now the other way round.
        """
        for q in ("build me an FPS game", "write a TOML parser",
                  "fix the crash in transport.py", "refactor this module",
                  "draw a picture using characters and signs",
                  "summarise this in three bullets", "give me a regex for emails",
                  "turn this into a table", "ascii art of a cat",
                  "a haiku about databases"):
            self.assertTrue(board.looks_like_a_task(q), q)
        for q in ("should we rewrite the parser?", "is postgres better than sqlite?",
                  "which model is best for this?", "do we need a queue here?",
                  "what is the tradeoff", "would you use microservices",
                  "why did this fail", "is it worth it", "has anyone shipped this"):
            self.assertFalse(board.looks_like_a_task(q), q)

    def test_ambiguity_lands_on_make(self):
        """Being handed the work when you wanted an opinion is a mild disappointment. Being
        handed a vote on whether your request is allowed is useless."""
        self.assertTrue(board.looks_like_a_task("the parser"))
        self.assertTrue(board.looks_like_a_task("something about caching"))

    def test_decide_tells_members_to_do_the_work_if_that_is_what_was_asked(self):
        """A failsafe: a mis-set mode should degrade to something useful, not something
        absurd. No member should be voting on whether a request is permissible."""
        t = OfflineTransport()
        board.ask("draw a cat", transport=t, models=POOL, size=3, kind="decide")
        first = t.calls[0][1]
        self.assertIn("ACTUALLY A REQUEST TO DO SOMETHING", first)
        self.assertIn("nobody asked you that", first)


class TheKeyBox(unittest.TestCase):
    """It accepted `flip to board and two ne` as an API key and stored it over the real one.

    A key is free to replace and impossible to recover, so the box that takes it should be the
    fussiest thing in the program. It was the loosest.
    """

    def test_pasted_prose_is_not_a_key(self):
        with self.assertRaises(config.BadKey):
            config.check_key("flip to board and two ne")

    def test_a_shape_we_guessed_at_must_not_refuse_a_real_key(self):
        """The first version refused anything not `sk-or-` and under 40 characters.

        Both numbers came from one example; OpenRouter documents no key format. The guess
        then rejected a real key. A shape check is a guess about someone else's format, so
        it may WARN and must never refuse.
        """
        for k in ("ork_" + "a" * 60, "sk-or-short", "sk-or-v1-abc"):
            with self.subTest(k=k[:10]):
                self.assertEqual(config.check_key(k), k)      # accepted
                self.assertTrue(config.looks_unusual(k))       # but flagged

    def test_another_provider_s_key_is_warned_about_not_refused(self):
        for k in ("sk-ant-" + "a" * 60, "ghp_" + "c" * 40):
            with self.subTest(k=k[:8]):
                config.check_key(k)
                self.assertIn("different service", config.looks_unusual(k))

    def test_empty_and_whitespace_are_refused(self):
        for k in ("", "   ", "\n\t"):
            with self.assertRaises(config.BadKey):
                config.check_key(k)

    def test_whitespace_anywhere_is_refused(self):
        for k in ("sk-or-v1 abc", "sk-or-v1-abc\ndef", "sk or v1"):
            with self.subTest(k=k[:12]), self.assertRaises(config.BadKey):
                config.check_key(k)

    def test_a_normal_key_draws_no_warning(self):
        self.assertEqual(config.looks_unusual("sk-or-v1-" + "a" * 64), "")

    def test_a_real_shaped_key_passes_and_is_trimmed(self):
        k = "sk-or-v1-" + "a" * 64
        self.assertEqual(config.check_key(f"  {k}  "), k)


class TheCounter(unittest.TestCase):
    """The ledger, including the bug where a corrected count then never moved again."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        importlib.reload(usage)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)
        importlib.reload(config)
        importlib.reload(usage)

    def test_the_estimate_counts_down(self):
        for _ in range(10):
            usage.record("m", True)
        self.assertEqual(usage.status(0).calls, 10)
        self.assertEqual(usage.status(0).remaining, 40)      # 50/day free tier
        self.assertFalse(usage.status(0).measured)

    def test_a_429_replaces_the_estimate_with_the_real_number(self):
        for _ in range(10):
            usage.record("m", True)
        usage.learn_from_429(limit=50, remaining=17, reset=None)
        st = usage.status(0)
        self.assertTrue(st.measured)
        self.assertEqual(st.remaining, 17)

    def test_the_measured_number_keeps_counting_down(self):
        """It used to freeze. `since` was hardcoded to zero with a comment describing the
        subtraction it was not doing, so the console sat on one figure through every call
        that followed - worse than the estimate it replaced."""
        usage.learn_from_429(limit=50, remaining=17, reset=None)
        for _ in range(5):
            usage.record("m", True)
        self.assertEqual(usage.status(0).remaining, 12)

    def test_failures_count_against_the_allowance(self):
        usage.record("m", False)
        st = usage.status(0)
        self.assertEqual((st.calls, st.failed), (1, 1))
        self.assertEqual(st.remaining, 49)

    def test_concurrent_writers_do_not_lose_calls(self):
        """Read, add one, write - from several threads at once, or two open consoles."""
        threads = [threading.Thread(target=lambda: [usage.record("m", True) for _ in range(20)])
                   for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(usage.status(0).calls, 200)


class TheRename(unittest.TestCase):
    """A rename must not cost someone their key.

    The rename pass rewrote the LEGACY_HOME literal to the NEW path, the guard against
    migrating a directory onto itself then matched, and the migration became a silent no-op --
    a rename that quietly ate the key it was written to preserve. Found only because the
    migration was actually run and the key had vanished.
    """

    def test_the_legacy_home_is_not_the_current_home(self):
        self.assertNotEqual(os.path.abspath(config.HOME),
                            os.path.abspath(config.LEGACY_HOME))
        self.assertTrue(config.LEGACY_HOME.endswith("freeboard"))

    def test_an_old_install_is_carried_across(self):
        old = tempfile.mkdtemp()
        new = tempfile.mkdtemp()
        os.rmdir(new)
        with open(os.path.join(old, "config.json"), "w") as f:
            f.write('{"api_key": "sk-or-v1-carried", "credits_purchased_usd": 10}')
        with open(os.path.join(old, "usage.json"), "w") as f:
            f.write('{"days": {"2026-09-04": {"calls": 7, "failed": 0, "models": {}}}}')
        try:
            os.environ["BOARD_HOME"] = new
            importlib.reload(config)
            config.LEGACY_HOME = old
            config.DEFAULT_HOME = config.HOME     # this run's home IS the default
            self.assertEqual(config.load().get("api_key"), "sk-or-v1-carried")
            self.assertTrue(os.path.exists(os.path.join(new, "usage.json")))
            self.assertTrue(os.path.exists(os.path.join(old, "config.json")),
                            "the old install must not be deleted")
        finally:
            os.environ.pop("BOARD_HOME", None)
            importlib.reload(config)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(new, ignore_errors=True)

    def test_an_explicit_home_is_never_migrated_into(self):
        """BOARD_HOME is a request for that exact directory - a fixture, a second account, a
        sandbox. Importing a previous install's key and call count into it is contamination,
        not migration."""
        old, new = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(old, "config.json"), "w") as f:
            f.write('{"api_key": "sk-or-v1-should-not-appear"}')
        try:
            os.environ["BOARD_HOME"] = new
            importlib.reload(config)
            config.LEGACY_HOME = old              # DEFAULT_HOME left alone on purpose
            self.assertEqual(config.load(), {})
        finally:
            os.environ.pop("BOARD_HOME", None)
            importlib.reload(config)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(new, ignore_errors=True)

    def test_migration_never_overwrites_newer_state(self):
        old, new = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(old, "config.json"), "w") as f:
            f.write('{"api_key": "sk-or-v1-old"}')
        with open(os.path.join(new, "config.json"), "w") as f:
            f.write('{"api_key": "sk-or-v1-current"}')
        try:
            os.environ["BOARD_HOME"] = new
            importlib.reload(config)
            config.LEGACY_HOME = old
            config.DEFAULT_HOME = config.HOME
            self.assertEqual(config.load().get("api_key"), "sk-or-v1-current")
        finally:
            os.environ.pop("BOARD_HOME", None)
            importlib.reload(config)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(new, ignore_errors=True)


class WatchingItHappen(unittest.TestCase):
    """A board session takes a minute. Reporting nothing until it is over turns deliberation
    into a spinner and hides the one thing worth watching."""

    def _events(self, **kw):
        seen = []
        board.ask("Ship it?", transport=OfflineTransport(**kw.pop("t", {})),
                  models=POOL, on_event=seen.append, **kw)
        return seen

    def test_every_stage_is_announced_in_order(self):
        kinds = [e["type"] for e in self._events(size=4)]
        self.assertEqual(kinds[0], "seated")
        self.assertLess(kinds.index("asking"), kinds.index("answer"))
        self.assertLess(kinds.index("answer"), kinds.index("tally"))
        self.assertLess(kinds.index("tally"), kinds.index("chairing"))
        self.assertEqual(kinds[-1], "decision")

    def test_each_answer_carries_its_vote_as_it_lands(self):
        answers = [e for e in self._events(size=4) if e["type"] == "answer"]
        self.assertTrue(answers)
        for a in answers:
            self.assertIn(a["vote"], board.VOTES)
            self.assertNotIn("VOTE:", a["text"], "the marker belongs in the badge, not the prose")

    def test_a_member_that_fails_is_announced_too(self):
        # Fail a model that is ACTUALLY SEATED. The first version failed POOL[0], which the
        # ranking never seats - so nothing failed, and the test was asserting against a
        # session it had not changed.
        seated = seats.seat(POOL, size=4)[0]["id"]
        evs = self._events(size=4, t={"fail": {seated}})
        fails = [e for e in evs if e["type"] == "failure"]
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0]["model"], seated)
        self.assertNotIn(seated, [e["model"] for e in evs if e["type"] == "answer"])

    def test_a_broken_listener_cannot_fail_the_session(self):
        """A display must never be able to take the board down with it."""
        def explode(_ev):
            raise RuntimeError("the browser went away")
        s = board.ask("Ship it?", transport=OfflineTransport(), models=POOL,
                      size=4, on_event=explode)
        self.assertIsNotNone(s.decision)
        self.assertEqual(len(s.answers), 4)

    def test_no_listener_at_all_is_fine(self):
        s = board.ask("Ship it?", transport=OfflineTransport(), models=POOL, size=4)
        self.assertIsNotNone(s.decision)


def paidmodel(mid, pin=1.0, pout=3.0, ctx=200000):
    m = model(mid, ctx=ctx)
    m.update({"free": False, "price_in": pin, "price_out": pout})
    return m


class PaidSeats(unittest.TestCase):
    """The first thing here that can spend money, so the rules are different.

    Everything else in this program is wrong-in-the-safe-direction by choice. Here the safe
    direction is not spending, and every gate points that way.
    """

    POOLP: typing.ClassVar[list] = [
        {**m, "free": True, "price_in": 0.0, "price_out": 0.0} for m in POOL
    ] + [paidmodel("zeta/pricey:x", 10.0, 30.0), paidmodel("eta/cheap:x", 0.02, 0.05)]

    def test_three_tiers_because_paid_only_is_a_real_want(self):
        """"Paid" and "free and paid" are different. Somebody paying for quality may not want
        free models on the board at all - a weak free seat is not a bargain, it is a vote."""
        free = catalogue.deliberative(self.POOLP, tier="free")
        paid = catalogue.deliberative(self.POOLP, tier="paid")
        both = catalogue.deliberative(self.POOLP, tier="both")
        self.assertTrue(all(m["free"] for m in free))
        self.assertTrue(all(not m["free"] for m in paid))
        self.assertEqual(len(both), len(free) + len(paid))
        self.assertTrue(free and paid, "the fixture must exercise both sides")

    def test_paid_only_seats_no_free_model_even_as_chair(self):
        b = seats.seat(self.POOLP, size=2, tier="paid")
        self.assertTrue(b and all(not m["free"] for m in b))

    def test_an_unknown_tier_falls_back_to_free(self):
        """The safe direction. A typo must not silently widen what may be spent."""
        d = catalogue.deliberative(self.POOLP, tier="freee")
        self.assertTrue(all(m["free"] for m in d))

    def test_the_old_boolean_setting_still_reads_as_a_tier(self):
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            config.save({"allow_paid": True})
            self.assertEqual(config.model_tier(), "both")
            config.save({"allow_paid": False})
            self.assertEqual(config.model_tier(), "free")
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)

    def test_paid_models_are_not_seated_by_default(self):
        b = seats.seat(self.POOLP, size=9)
        self.assertTrue(all(m["free"] for m in b), "a default board must cost nothing")

    def test_they_are_seated_only_when_explicitly_allowed(self):
        b = seats.seat(self.POOLP, size=9, allow_paid=True)
        self.assertTrue(any(not m["free"] for m in b))

    def test_the_chair_follows_the_same_permission(self):
        """The chair is chosen by the program, not the user. It must never be the thing that
        turns a free session into a paid one."""
        b = seats.seat(self.POOLP, size=3)
        self.assertTrue(seats.chair(self.POOLP, b)["free"])

    def test_a_free_board_costs_nothing_and_says_so(self):
        b = seats.seat(self.POOLP, size=4)
        e = cost.session(b, seats.chair(self.POOLP, b))
        self.assertEqual(e.usd, 0.0)
        self.assertEqual(e.human(), "free")

    def test_an_unpriced_model_is_refused_not_costed_at_zero(self):
        """Unknown is not free, and unknown cannot be consented to."""
        m = paidmodel("theta/mystery:x")
        m["price_in"] = None
        with self.assertRaises(cost.Unpriced):
            cost.session([m])

    def test_a_negative_price_never_becomes_a_bargain(self):
        """OpenRouter uses -1 for routers whose price depends on what they pick. Multiplied
        out that reads as minus a million dollars, and sorts to the top of "cheapest"."""
        self.assertIsNone(catalogue._per_million(-1))
        self.assertEqual(catalogue._per_million(0.000002), 2.0)

    def test_the_estimate_rounds_up(self):
        """Being pleasantly surprised is the only acceptable direction to be wrong in."""
        e = cost.session([paidmodel("zeta/pricey:x", 10.0, 30.0)], peer_review=False)
        raw = (cost.DEFAULT_PROMPT_TOKENS / 1e6) * 10.0 + (cost.DEFAULT_OUTPUT_TOKENS / 1e6) * 30.0
        self.assertGreaterEqual(e.usd, round(raw, 6))

    def test_the_chair_is_priced_for_reading_everyone(self):
        members = [paidmodel(f"m{i}/x:y", 1.0, 1.0) for i in range(4)]
        chair = paidmodel("chair/x:y", 1.0, 1.0)
        with_chair = cost.session(members, chair, peer_review=False)
        without = cost.session(members, None, peer_review=False)
        # the chair's prompt is the whole meeting, so it costs more than one member does
        one_member = (with_chair.usd - without.usd)
        self.assertGreater(one_member, without.usd / len(members))

    def test_a_zero_cap_makes_spending_impossible_not_merely_unselected(self):
        """Plenty of people buy the $10 for the rate limit alone.

        It moves free models from 50 to 1000 requests a day and is never meant to be spent -
        the balance is a key, not a wallet. For them "paid is switched off" is one stray click
        from being wrong, so a zero cap has to be a lock rather than a preference.
        """
        e = cost.session([paidmodel("zeta/pricey:x", 10.0, 30.0)])
        self.assertTrue(cost.locked_to_free(0))
        self.assertTrue(cost.locked_to_free(0.0))
        self.assertTrue(cost.over_cap(e, 0))
        self.assertFalse(cost.locked_to_free(0.01))
        self.assertFalse(cost.locked_to_free(None), "no cap set is not the same as a zero cap")

    def test_a_free_board_still_runs_under_a_zero_cap(self):
        """The lock stops spending, not the program."""
        b = seats.seat(self.POOLP, size=4)
        e = cost.session(b, seats.chair(self.POOLP, b))
        self.assertEqual(e.usd, 0.0)
        self.assertFalse(cost.over_cap(e, 0))

    def test_a_cap_is_a_wall(self):
        e = cost.session([paidmodel("zeta/pricey:x", 10.0, 30.0)] * 3)
        self.assertTrue(cost.over_cap(e, 0.01))
        self.assertFalse(cost.over_cap(e, 1000.0))
        self.assertFalse(cost.over_cap(e, None), "no cap set means no wall")

    def test_routers_can_never_hold_a_seat(self):
        """Two routers can quietly choose the same underlying model, and the independence the
        whole thing rests on is gone without anything looking wrong."""
        pool = [*self.POOLP, paidmodel("openrouter/auto", 1.0, 1.0),
                {**model("openrouter/free"), "free": True}]
        ids = {m["id"] for m in seats.seat(pool, size=99, allow_paid=True)}
        self.assertNotIn("openrouter/auto", ids)
        self.assertNotIn("openrouter/free", ids)


class TheBoardDoesNotQueue(unittest.TestCase):
    """"The board has never worked" - and it had not, for a reason that never threw.

    Members were asked one at a time in a for loop, each call up to four attempts at a
    two-minute timeout. One stuck model could hold the whole board for eight minutes with
    nothing on screen. Not a crash: a queue. The independence that is the entire premise of
    this thing was being thrown away by the code that asks them.
    """

    class Slow(OfflineTransport):
        def __init__(self, slow, secs):
            super().__init__()
            self.slow, self.secs = slow, secs

        def ask(self, model, messages, **kw):
            if model["id"] in self.slow:
                time.sleep(self.secs)
            return super().ask(model, messages, **kw)

    def test_one_slow_member_does_not_hold_the_board(self):
        b = seats.seat(POOL, size=4)
        t0 = time.time()
        s = board.ask("x", transport=self.Slow({b[0]["id"]}, 3), models=POOL,
                      members=b, deadline=0.5)
        took = time.time() - t0
        self.assertLess(took, 2.0, f"the board waited {took:.1f}s for a member it gave up on")
        self.assertEqual(len(s.answers), 3)
        self.assertIn("did not wait", s.failures[0].reason)

    def test_a_missed_deadline_is_a_failure_like_any_other(self):
        """From the board's point of view it IS one: the member did not answer. Which means
        it is not counted as agreement, and it can cost the session its quorum honestly."""
        b = seats.seat(POOL, size=4)
        s = board.ask("x", transport=self.Slow({m["id"] for m in b}, 2), models=POOL,
                      members=b, minimum=3, deadline=0.3)
        self.assertEqual(len(s.answers), 0)
        self.assertIsNotNone(s.no_quorum)

    def test_members_are_asked_at_the_same_time_not_in_turn(self):
        """Four members that each take half a second should cost about half a second."""
        b = seats.seat(POOL, size=4)
        t0 = time.time()
        board.ask("x", transport=self.Slow({m["id"] for m in b}, 0.4), models=POOL,
                  members=b, peer_review=False, deadline=5.0)
        took = time.time() - t0
        self.assertLess(took, 1.6, f"asked in turn, not at once: {took:.1f}s for 4 x 0.4s")

    def test_the_retry_policy_cannot_outlast_the_deadline(self):
        from boardofdirectors.transport import OpenRouterTransport as T
        self.assertEqual(T("sk-or-v1-" + "a" * 64).max_retries, 2)
        self.assertEqual(T("sk-or-v1-" + "a" * 64).timeout, 45.0)
        self.assertLessEqual(T._backoff(0, 999), 15.0)


class TheVote(unittest.TestCase):
    """A board that cannot show its own vote is a discussion.

    The tally is READ from what each member declared. Inferring a position from wording would
    put words in a member's mouth and then count them - the same failure as counting a silent
    member as agreement, one step further along.
    """

    def test_a_declared_vote_is_read_in_the_forms_models_actually_write(self):
        for text, expect in (("reasons\nVOTE: FOR", "FOR"),
                             ("**VOTE:** AGAINST", "AGAINST"),
                             ("vote: depends", "DEPENDS"),
                             ("VOTE : FOR ", "FOR"),
                             ("VOTE:**FOR**", "FOR")):
            with self.subTest(text=text):
                self.assertEqual(board.read_vote(text), expect)

    def test_an_undeclared_vote_is_unclear_not_guessed(self):
        for text in ("I strongly support this proposal.",
                     "Absolutely not, this is a terrible idea.",
                     "", "yes"):
            with self.subTest(text=text[:24]):
                self.assertEqual(board.read_vote(text), "UNCLEAR")

    def test_the_marker_is_stripped_from_the_reasoning(self):
        t = "Start with a monolith.\n\nVOTE: AGAINST"
        self.assertEqual(board.strip_vote(t), "Start with a monolith.")
        self.assertNotIn("VOTE", board.strip_vote(t))

    def test_the_tally_counts_and_calls_it(self):
        class A:
            def __init__(self, t):
                self.text = t
        t = board.tally([A("a\nVOTE: FOR"), A("b\nVOTE: FOR"),
                         A("c\nVOTE: AGAINST"), A("d\nVOTE: DEPENDS"), A("e no marker")])
        self.assertEqual((t["FOR"], t["AGAINST"], t["DEPENDS"], t["UNCLEAR"]), (2, 1, 1, 1))
        self.assertEqual(t["decided"], 3)
        self.assertTrue(t["carried"])
        self.assertFalse(t["split"])

    def test_a_tie_is_reported_as_split_not_carried(self):
        class A:
            def __init__(self, t):
                self.text = t
        t = board.tally([A("VOTE: FOR"), A("VOTE: AGAINST")])
        self.assertTrue(t["split"])
        self.assertFalse(t["carried"])

    def test_no_declared_votes_is_not_a_verdict(self):
        class A:
            def __init__(self, t):
                self.text = t
        t = board.tally([A("no marker"), A("none here")])
        self.assertEqual(t["decided"], 0)
        self.assertIsNone(t["carried"])

    def test_a_session_carries_the_tally(self):
        s = board.ask("Should we?", transport=OfflineTransport(), models=POOL, size=4)
        self.assertTrue(s.tally)
        self.assertIn("VOTE:", s.report())

    def test_the_chair_is_handed_the_count_not_asked_to_recount_prose(self):
        t = OfflineTransport()
        board.ask("Should we?", transport=t, models=POOL, size=4)
        chair_prompt = t.calls[-1][1]
        self.assertIn("already been counted for you", chair_prompt)
        self.assertRegex(chair_prompt, r"\d+ for, \d+ against")


class TwoKindsOf429(unittest.TestCase):
    """The counter read 58/50 while every other model on the board answered perfectly.

    Two causes, and they compound. Retries were counted as separate calls, so one question to
    a busy model registered as four. And a 429 from the upstream PROVIDER - "Provider returned
    error (429)", the model's own company at capacity - was counted as spent allowance, which
    it is not. Checked against the account: OpenRouter reported usage_daily 0 and no 429 all
    day had carried a rate-limit header, meaning the platform limit had never been touched.
    """

    def test_a_platform_limit_is_told_apart_from_a_busy_provider(self):
        from boardofdirectors.transport import is_platform_limit
        # OpenRouter's own limit arrives with the headers that state it
        self.assertTrue(is_platform_limit({"X-RateLimit-Remaining": "0"}, "{}"))
        # an upstream provider at capacity carries none of them and says so
        self.assertFalse(is_platform_limit({}, '{"error":{"message":"Provider returned error"}}'))
        # anything else unheadered is assumed to be ours, which is the safe direction
        self.assertTrue(is_platform_limit({}, '{"error":{"message":"Rate limit exceeded"}}'))

    def test_a_busy_provider_does_not_move_the_meter(self):
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            importlib.reload(usage)
            usage.record("a/one", ok=True)
            usage.record("b/one", ok=False, provider_side=True)
            usage.record("b/one", ok=False, provider_side=True)
            st = usage.status(0)
            self.assertEqual(st.calls, 1, "a provider refusal is not spent allowance")
            self.assertEqual(st.provider_busy, 2)
            self.assertEqual(st.remaining, 49)
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)
            importlib.reload(usage)

    def test_the_ledger_is_never_clamped(self):
        """An over-count is a FACT - the allowance is not what we think, or something else is
        using the key. Clamping it in the data would hide the thing that needs explaining."""
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            importlib.reload(usage)
            for _ in range(58):
                usage.record("a/one", ok=True)
            self.assertEqual(usage.status(0).calls, 58)
            self.assertEqual(usage.status(0).remaining, 0)
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)
            importlib.reload(usage)

    def test_a_retry_is_not_a_second_question(self):
        """One request to a busy provider used to register four times."""
        import io
        import urllib.error
        import urllib.request
        home = tempfile.mkdtemp()
        old_home, old_open = os.environ.get("BOARD_HOME"), urllib.request.urlopen
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            importlib.reload(usage)
            from boardofdirectors.transport import OpenRouterTransport

            def busy(*a, **kw):
                raise urllib.error.HTTPError(
                    "u", 429, "Too Many Requests", {},
                    io.BytesIO(b'{"error":{"message":"Provider returned error"}}'))

            urllib.request.urlopen = busy
            t = OpenRouterTransport("sk-or-v1-" + "a" * 64, max_retries=4, sleep=lambda s: None)
            r = t.ask({"id": "b/one", "supported_parameters": []}, [{"role": "user", "content": "x"}])
            self.assertFalse(r.ok)
            st = usage.status(0)
            self.assertEqual(st.calls, 0, "four attempts at one question is still one question")
            self.assertEqual(st.provider_busy, 1)
        finally:
            urllib.request.urlopen = old_open
            if old_home is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old_home
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)
            importlib.reload(usage)


class TheOutputCap(unittest.TestCase):
    """An audit of seven files stopped mid-sentence, at the word "So".

    Every request was capped at 1024 output tokens - a number chosen for a chat reply and then
    applied to a model asked to enumerate defects across a codebase. A truncated audit is
    worse than no audit: it reads like a finished list, so the findings it never reached are
    indistinguishable from findings it did not have.
    """

    def test_the_default_follows_the_model_not_a_chat_sized_guess(self):
        big = {"id": "a/big", "max_completion_tokens": 230400, "supported_parameters": ["max_tokens"]}
        small = {"id": "a/small", "max_completion_tokens": 8192, "supported_parameters": ["max_tokens"]}
        for m, expected in ((big, 32768), (small, 8192)):
            sent = {}
            t = OpenRouterTransport("sk-or-v1-" + "a" * 64, meter=False)
            orig = OpenRouterTransport._payload

            def spy(model, messages, want_json, max_tokens, temperature, _s=sent, _o=orig):
                _s["max_tokens"] = max_tokens
                return _o(model, messages, want_json, max_tokens, temperature)

            OpenRouterTransport._payload = staticmethod(spy)
            try:
                t.ask(m, [{"role": "user", "content": "x"}])
            except Exception:
                pass
            finally:
                OpenRouterTransport._payload = staticmethod(orig)
            self.assertEqual(sent.get("max_tokens"), expected, m["id"])

    def test_a_caller_can_still_ask_for_a_short_answer(self):
        m = {"id": "a/big", "max_completion_tokens": 230400, "supported_parameters": ["max_tokens"]}
        body = OpenRouterTransport._payload(m, [], False, 256, 0.5)
        self.assertEqual(body["max_tokens"], 256)


class TheTrueCount(unittest.TestCase):
    """An inference key cannot see its own usage. A management key can.

    /api/v1/analytics/query serves a request_count metric and answers 403 to an ordinary key:
    "Only management keys can access analytics". So the exact number is obtainable, with a
    second credential the user opts into.
    """

    def test_a_failure_is_never_reported_as_zero(self):
        """"We could not read it" must not become "you have used nothing" - the worst
        direction for a quota meter to be wrong in."""
        n, why = truecount.requests_today("")
        self.assertIsNone(n)
        self.assertIn("no management key", why)

    def test_the_day_window_is_a_full_utc_day(self):
        start, end = truecount._utc_day_bounds(datetime.date(2026, 9, 4))
        self.assertEqual(start, "2026-09-04T00:00:00Z")
        self.assertEqual(end, "2026-09-05T00:00:00Z")

    def test_the_management_key_is_only_ever_sent_to_analytics(self):
        """It can create and DELETE API keys. A credential with that power must never be one
        keystroke from exercising it, so nothing in this module may touch a key route."""
        # Checked as CODE, not as prose. A first version of this searched the whole file for
        # the word "DELETE" and failed on the docstring that warns about deletion - the same
        # trap as a secret-scanner tripping on its own example.
        import ast
        with open(truecount.__file__) as fh:
            tree = ast.parse(fh.read())
        # Docstrings are string constants too, and this module's own docstring names
        # openrouter.ai twice while explaining the danger. Third time today that prose has
        # broken a checker: exclude them and look only at strings the code uses.
        doctexts = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))}
        urls = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "openrouter.ai" in n.value and n.value not in doctexts]
        self.assertTrue(urls, "no endpoint found at all")
        for u in urls:
            self.assertTrue(u.endswith("/analytics/query"),
                            f"the management key must only ever reach analytics, not {u}")
        methods = [kw.value.value for n in ast.walk(tree)
                   if isinstance(n, ast.Call) for kw in getattr(n, "keywords", [])
                   if kw.arg == "method" and isinstance(kw.value, ast.Constant)]
        self.assertEqual([m for m in methods if m not in ("GET", "POST")], [],
                         "no destructive HTTP method may appear here")

    def test_the_truth_outranks_the_estimate_and_says_so(self):
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            importlib.reload(usage)
            for _ in range(9):
                usage.record("a/one", ok=True)
            self.assertEqual(usage.status(0).source, "estimate")
            st = usage.status(0, true_calls=31)
            self.assertEqual((st.calls, st.remaining, st.source), (31, 19, "analytics"))
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)
            importlib.reload(usage)

    def test_the_truth_overrides_a_reset_too(self):
        """A reset discards what WE counted. It cannot discard what actually happened."""
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            importlib.reload(usage)
            usage.record("a/one", ok=True)
            usage.reset_today()
            self.assertTrue(usage.status(0).since_reset)
            st = usage.status(0, true_calls=12)
            self.assertFalse(st.since_reset)
            self.assertEqual(st.calls, 12)
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)
            importlib.reload(usage)

    def test_a_management_key_is_stored_separately_from_the_inference_key(self):
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            config.set_api_key("sk-or-v1-" + "a" * 64)
            config.set_management_key("mgmt-" + "b" * 40)
            self.assertTrue(config.api_key()[0].startswith("sk-or-v1-"))
            self.assertTrue(config.management_key()[0].startswith("mgmt-"))
            config.forget_management_key()
            self.assertIsNone(config.management_key()[0])
            self.assertIsNotNone(config.api_key()[0], "forgetting one must not drop the other")
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)


class ResettingTheCount(unittest.TestCase):
    """A wrong count is not self-correcting.

    Today's figure was inflated by a bug that counted retries and provider-side refusals as
    spent allowance. Fixing the counter does nothing for a number that was already too high,
    so there has to be a way to start it clean - as a command the user runs, not as a file
    quietly rewritten underneath them.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        importlib.reload(usage)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)
        importlib.reload(config)
        importlib.reload(usage)

    def test_it_clears_the_day_and_reports_what_it_discarded(self):
        for _ in range(7):
            usage.record("a/one", ok=True)
        usage.record("b/one", ok=False, provider_side=True)
        was = usage.reset_today()
        self.assertEqual(was["calls"], 7)
        self.assertEqual(was["provider_busy"], 1)
        self.assertEqual(usage.status(0).calls, 0)
        self.assertEqual(usage.status(0).remaining, 50)

    def test_a_measured_figure_from_the_discarded_day_goes_too(self):
        """Keeping a 429's number after clearing the calls it was measured against would
        leave the meter pinned to a figure with nothing behind it."""
        usage.record("a/one", ok=True)
        usage.learn_from_429(limit=50, remaining=11, reset=None)
        self.assertTrue(usage.status(0).measured)
        usage.reset_today()
        st = usage.status(0)
        self.assertFalse(st.measured)
        self.assertEqual(st.remaining, 50)

    def test_a_reset_day_stops_promising_a_full_allowance(self):
        """"0 / 50" after a reset promises fifty requests with no basis for it.

        Clearing OUR count returns none of OpenRouter's allowance, and what was spent before
        the reset is unrecoverable - the only record was the broken one that got discarded.
        A meter that cannot know must say so rather than round its ignorance up.
        """
        for _ in range(9):
            usage.record("a/one", ok=True)
        self.assertFalse(usage.status(0).since_reset)
        usage.reset_today()
        self.assertTrue(usage.status(0).since_reset)

    def test_an_ordinary_day_is_not_marked(self):
        usage.record("a/one", ok=True)
        self.assertFalse(usage.status(0).since_reset)

    def test_counting_after_a_reset_starts_from_one(self):
        for _ in range(5):
            usage.record("a/one", ok=True)
        usage.reset_today()
        usage.record("a/one", ok=True)
        self.assertEqual(usage.status(0).calls, 1)

    def test_other_days_are_untouched(self):
        usage.record("a/one", ok=True, day="2026-09-01")
        usage.record("a/one", ok=True)
        usage.reset_today()
        self.assertEqual(usage.status(0).calls, 0)
        self.assertEqual(len([r for r in usage._load()["days"] if r == "2026-09-01"]), 1)


class WritingFilesSafely(unittest.TestCase):
    """Every store shared one temp filename, which stopped being survivable the day the board
    went parallel.

    Write x.tmp, rename over x. The rename is atomic - that is the point - but the temp NAME
    was fixed, so two writers shared it: one renamed it away while the other was still
    writing, and the second one's rename found nothing there. A 403 from two members at once
    has two threads inside mark_unusable, which is a read-modify-write on the file holding
    the API key. Hammered with twelve threads: eleven crashed, and thirteen of a hundred and
    forty-four writes survived.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "x.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_concurrent_read_modify_writes_lose_nothing(self):
        atomic.write_json(self.p, {"n": 0})

        def hammer():
            for _ in range(30):
                with atomic.locked(self.p):
                    c = atomic.read_json(self.p, {}) or {}
                    c["n"] = c.get("n", 0) + 1
                    atomic.write_json(self.p, c)

        threads = [threading.Thread(target=hammer) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(atomic.read_json(self.p)["n"], 360)

    def test_no_two_writers_share_a_scratch_file(self):
        """The whole bug in one assertion: the temp name has to differ per thread."""
        import re as _re
        src = inspect.getsource(atomic.write)
        m = _re.search(r'tmp = f"([^"]+)"', src)
        self.assertIsNotNone(m, "the temp name is not built from a format string")
        self.assertIn("getpid", m.group(1) + src)
        self.assertIn("get_ident", src, "two THREADS was the case that actually bit")

    def test_nothing_is_left_behind(self):
        for i in range(20):
            atomic.write_json(self.p, {"i": i})
        self.assertEqual([f for f in os.listdir(self.d) if ".tmp" in f], [])

    def test_a_truncated_file_reads_as_absent_rather_than_raising(self):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write('{"half": ')
        self.assertEqual(atomic.read_json(self.p, "fallback"), "fallback")

    def test_a_failed_write_leaves_no_scratch_file(self):
        with contextlib.suppress(OSError):
            atomic.write(os.path.join(self.d, "nope", "deep", "x"), "y")
        self.assertEqual([f for f in os.listdir(self.d) if ".tmp" in f], [])

    def test_the_config_survives_twelve_threads_marking_models_unusable(self):
        """The real path: a 403 from several members at once."""
        home = tempfile.mkdtemp()
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        try:
            importlib.reload(config)
            config.set_api_key("sk-or-v1-" + "a" * 64)

            def hammer(i):
                for n in range(12):
                    config.mark_unusable(f"v{i}/m{n}:free", "busy")

            threads = [threading.Thread(target=hammer, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            cfg = config.load()
            self.assertTrue(cfg.get("api_key"), "the key must survive")
            self.assertEqual(len(cfg.get("unusable") or {}), 144)
        finally:
            if old is None:
                os.environ.pop("BOARD_HOME", None)
            else:
                os.environ["BOARD_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)
            importlib.reload(config)


class ReachableFromABrowser(unittest.TestCase):
    """"No auth is fine, it is only on localhost" skips over the browser.

    Loopback keeps the NETWORK out. It does not stop a page you merely visit from POSTing to
    127.0.0.1 - the page cannot read the reply without CORS, and none is sent, but a
    fire-and-forget POST is enough to spend your balance or write a file. Nor does it stop DNS
    rebinding, where a hostname the attacker controls is re-pointed at 127.0.0.1 so their page
    is same-origin with this server.
    """

    def test_a_cross_site_origin_is_refused(self):
        for origin in ("https://evil.example", "http://evil.example:8420",
                       "https://127.0.0.1.evil.example"):
            with self.subTest(origin=origin):
                self.assertFalse(server._origin_ok(origin))

    def test_the_page_itself_is_allowed(self):
        for origin in ("http://127.0.0.1:8420", "http://localhost:8420"):
            with self.subTest(origin=origin):
                self.assertTrue(server._origin_ok(origin))

    def test_no_origin_at_all_is_allowed(self):
        """Curl, a script, an SDK - none of them is a browser, and none sends Origin."""
        self.assertTrue(server._origin_ok(None))
        self.assertTrue(server._origin_ok(""))

    def test_a_rebound_hostname_is_refused(self):
        """Origin alone cannot catch this: after rebinding, the attacker's page IS the origin."""
        for host in ("attacker.example", "attacker.example:8420", "192.168.1.9:8420"):
            with self.subTest(host=host):
                self.assertFalse(server._host_ok(host))

    def test_all_interfaces_is_not_loopback(self):
        """0.0.0.0 means EVERY interface and it was in the allowlist, so a Host of 0.0.0.0
        counted as local - the opposite of what the check is for. It was there because the
        same names were describing what the server BINDS to, which is a different question."""
        self.assertFalse(server._host_ok("0.0.0.0"))
        self.assertFalse(server._host_ok("0.0.0.0:8420"))
        self.assertNotIn("0.0.0.0", server.LOOPBACK_HOSTS)

    def test_the_check_uses_the_constant_rather_than_a_second_copy(self):
        """The duplicate had already drifted: it included 0.0.0.0 and the constant did not."""
        import inspect
        src = inspect.getsource(server._host_ok)
        self.assertIn("LOOPBACK_HOSTS", src)
        self.assertNotIn('"127.0.0.1",', src.split("return")[-1])

    def test_loopback_hostnames_are_allowed(self):
        for host in ("127.0.0.1:8420", "localhost:8420", "localhost", "[::1]:8420", None):
            with self.subTest(host=host):
                self.assertTrue(server._host_ok(host))


class TheOpenAIEndpoint(unittest.TestCase):
    """The library covers Python. Everything else had no way in but the page's internals.

    So the board speaks the dialect every LLM tool already speaks: point any OpenAI-compatible
    client at it, ask for the model `board`, and a whole board's decision comes back in the
    shape the client already parses.
    """

    def run_it(self, payload, **kw):
        return openai_api.run(payload, POOL, OfflineTransport(**kw.pop("t", {})), **kw)

    def test_the_model_name_selects_the_shape(self):
        self.assertEqual(openai_api.parse_model("board")["kind"], "decide")
        self.assertEqual(openai_api.parse_model("board:make")["kind"], "make")
        self.assertEqual(openai_api.parse_model("board:3")["size"], 3)
        self.assertEqual(openai_api.parse_model("board:make:4"),
                         {"single": None, "kind": "make", "size": 4})
        self.assertEqual(openai_api.parse_model("a/b:free")["single"], "a/b:free")

    def test_a_board_comes_back_as_a_chat_completion(self):
        body, status = self.run_it({"model": "board:3",
                                    "messages": [{"role": "user", "content": "Ship it?"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "chat.completion")
        self.assertTrue(body["choices"][0]["message"]["content"])
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")

    def test_usage_reports_requests_not_guessed_tokens(self):
        """A guess in the usage field would be read as a measurement. A board's real cost is
        the number of calls it made, and that is the number a caller budgets against."""
        body, _ = self.run_it({"model": "board:3", "messages": [{"role": "user", "content": "x"}]})
        self.assertGreater(body["usage"]["requests"], 1)
        self.assertEqual(body["usage"]["total_tokens"], 0)

    def test_the_vote_and_the_members_travel_with_it(self):
        body, _ = self.run_it({"model": "board:3", "messages": [{"role": "user", "content": "x"}]})
        b = body["board"]
        self.assertEqual(len(b["members"]), 3)
        self.assertNotIn(b["chair"], b["members"])
        self.assertTrue(b["tally"])
        for a in b["answers"]:
            self.assertIn(a["vote"], board.VOTES)

    def test_a_member_that_failed_is_reported_not_folded_in(self):
        """A caller needs to know the decision came from four models and not five."""
        seated = seats.seat(POOL, size=4)[0]["id"]
        body, _ = self.run_it({"model": "board:4", "messages": [{"role": "user", "content": "x"}]},
                              t={"fail": {seated}})
        self.assertEqual([f["model"] for f in body["board"]["failures"]], [seated])

    def test_no_quorum_is_a_409_that_still_carries_the_answers(self):
        body, status = self.run_it(
            {"model": "board:4", "messages": [{"role": "user", "content": "x"}]},
            t={"fail": {m["id"] for m in seats.seat(POOL, size=4)}})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "no_quorum")
        self.assertIn("board", body)

    def test_a_plain_model_id_passes_straight_through(self):
        body, status = self.run_it({"model": POOL[0]["id"],
                                    "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["usage"]["requests"], 1)
        self.assertNotIn("board", body)

    def test_errors_use_the_shape_clients_already_parse(self):
        body, status = self.run_it({"model": "nope/nope",
                                    "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["type"], "model_not_found")
        body, status = self.run_it({"model": "board", "messages": []})
        self.assertEqual(status, 400)

    def test_the_model_list_offers_the_boards_first(self):
        d = openai_api.model_list(POOL)["data"]
        self.assertEqual([r["id"] for r in d[:3]], ["board", "board:make", "board:3"])


class ProposedChanges(unittest.TestCase):
    """The first thing here that can change your files, so the safe direction is doing nothing.

    The model never writes. It returns whole files; the server diffs them against disk and
    shows you; a person clicks apply, one file at a time, and the previous contents are kept.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a - b\n")
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "b.py"), "w") as f:
            f.write("x = 1\n")
        self.allowed = {"calc.py", "sub/b.py"}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_whole_file_is_parsed_and_diffed(self):
        answer = "----- calc.py -----\ndef add(a, b):\n    return a + b\n"
        ch, _ = patch.parse(answer, self.root, self.allowed)
        self.assertEqual([c.rel for c in ch], ["calc.py"])
        self.assertEqual((ch[0].added, ch[0].removed), (1, 1))
        self.assertIn("+    return a + b", ch[0].diff())

    def test_a_fenced_block_is_unwrapped(self):
        """Models wrap the contents in backticks about half the time."""
        answer = "----- calc.py -----\n```python\ndef add(a, b):\n    return a + b\n```\n"
        ch, _ = patch.parse(answer, self.root, self.allowed)
        self.assertEqual(len(ch), 1)
        self.assertNotIn("```", ch[0].new)

    def test_traversal_is_refused_not_normalised(self):
        """`.lstrip("./")` strips a SET of characters, not a prefix: it turned
        "../../.ssh/config" into "ssh/config", quietly rewriting a traversal attempt into a
        plausible relative path that an allowlist might have accepted."""
        answer = ("----- ../../.ssh/config -----\npwned\n"
                  "----- /etc/passwd -----\npwned\n"
                  "----- sub/../calc.py -----\npwned\n")
        ch, notes = patch.parse(answer, self.root, self.allowed | {"ssh/config"})
        self.assertEqual(ch, [])
        self.assertEqual(len(notes), 3)
        for n in notes:
            self.assertIn("escapes", n)

    def test_a_file_the_board_never_saw_is_refused(self):
        answer = "----- secrets.env -----\nAPI_KEY=x\n"
        ch, notes = patch.parse(answer, self.root, self.allowed)
        self.assertEqual(ch, [])
        self.assertIn("was not in the code", notes[0])

    def test_an_unchanged_file_is_not_offered(self):
        answer = "----- sub/b.py -----\nx = 1\n"
        ch, notes = patch.parse(answer, self.root, self.allowed)
        self.assertEqual(ch, [])
        self.assertIn("unchanged", notes[0])

    def test_applying_writes_and_keeps_the_old_contents(self):
        ch, _ = patch.parse("----- calc.py -----\ndef add(a, b):\n    return a + b\n",
                            self.root, self.allowed)
        backups = os.path.join(self.root, ".bak")
        patch.apply(ch[0], expect_digest=patch.digest(ch[0].old), backup_dir=backups)
        with open(ch[0].path) as f:
            self.assertIn("a + b", f.read())
        kept = os.listdir(backups)
        self.assertEqual(len(kept), 1)
        with open(os.path.join(backups, kept[0])) as f:
            self.assertIn("a - b", f.read(), "the previous contents must be recoverable")

    def test_a_file_that_moved_since_the_board_read_it_is_refused(self):
        """The proposal was written against text that is no longer there. Applying it would
        silently discard whatever happened in between."""
        ch, _ = patch.parse("----- calc.py -----\ndef add(a, b):\n    return a + b\n",
                            self.root, self.allowed)
        stale = patch.digest(ch[0].old)
        with open(ch[0].path, "w") as f:
            f.write("someone else edited this\n")
        with self.assertRaises(patch.Rejected) as e:
            patch.apply(ch[0], expect_digest=stale)
        self.assertIn("changed on disk", str(e.exception))

    def test_parsing_alone_writes_nothing(self):
        target = os.path.join(self.root, "calc.py")
        with open(target) as f:
            before = f.read()
        patch.parse("----- calc.py -----\ntotally different\n", self.root, self.allowed)
        with open(target) as f:
            self.assertEqual(f.read(), before)


class SavedSessions(unittest.TestCase):
    """A board turn costs 9-11 of a 50-a-day allowance. Losing it on reload is not acceptable."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        importlib.reload(sessions)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)
        importlib.reload(config)
        importlib.reload(sessions)

    TURNS: typing.ClassVar[list] = [
        {"role": "user", "content": "Should we use Postgres or SQLite?"},
        {"role": "assistant", "board": {
            "kind": "decide", "chair": "x/chair", "calls": 9,
            "answers": [{"label": "Member A", "model": "a/one", "text": "Postgres."},
                        {"label": "Member B", "model": "b/one", "text": "SQLite."}],
            "failures": [{"model": "c/one", "reason": "Rate limit exceeded (429)"}],
            "decision": "2-0 for Postgres."}},
    ]

    def test_the_whole_proceeding_survives_a_round_trip(self):
        """Storing only the verdict would reopen looking unanimous - the exact dishonesty the
        board exists to prevent. Every member, and every member that failed, comes back."""
        sid = sessions.new_id()
        sessions.save(sid, self.TURNS)
        back = sessions.load(sid)
        b = back["turns"][1]["board"]
        self.assertEqual(len(b["answers"]), 2)
        self.assertEqual(b["failures"][0]["model"], "c/one")
        self.assertEqual(b["decision"], "2-0 for Postgres.")

    def test_the_title_is_the_question(self):
        sid = sessions.new_id()
        sessions.save(sid, self.TURNS)
        self.assertEqual(sessions.load(sid)["title"], "Should we use Postgres or SQLite?")

    def test_saving_twice_keeps_the_original_creation_time(self):
        sid = sessions.new_id()
        sessions.save(sid, self.TURNS)
        created = sessions.load(sid)["created"]
        sessions.save(sid, [*self.TURNS, {"role": "user", "content": "and backups?"}])
        after = sessions.load(sid)
        self.assertEqual(after["created"], created)
        self.assertGreaterEqual(after["updated"], created)
        self.assertEqual(len(after["turns"]), 3)

    def test_listing_is_newest_first(self):
        for q in ("first", "second", "third"):
            sessions.save(sessions.new_id(), [{"role": "user", "content": q}])
        self.assertEqual([r["title"] for r in sessions.listing()], ["third", "second", "first"])

    def test_a_session_id_cannot_escape_the_directory(self):
        """It is ours, but it still arrives from an HTTP request."""
        for bad in ("../../etc/passwd", "/etc/passwd", "..", ""):
            with self.subTest(bad=bad):
                try:
                    p = sessions._path(bad)
                except ValueError:
                    continue
                self.assertTrue(os.path.abspath(p).startswith(os.path.abspath(sessions.DIR)))

    def test_export_keeps_the_dissent_and_the_failures(self):
        """A board's output is only worth keeping if the disagreement comes with it."""
        sid = sessions.new_id()
        sessions.save(sid, self.TURNS)
        md = sessions.as_markdown(sessions.load(sid))
        self.assertIn("Member A", md)
        self.assertIn("Member B", md)
        self.assertIn("not counted as agreement", md)
        self.assertIn("c/one", md)
        self.assertIn("2-0 for Postgres.", md)

    def test_deleting_removes_it(self):
        sid = sessions.new_id()
        sessions.save(sid, self.TURNS)
        self.assertTrue(sessions.delete(sid))
        self.assertIsNone(sessions.load(sid))
        self.assertEqual(sessions.listing(), [])

    def test_old_sessions_are_pruned(self):
        sessions.MAX_SESSIONS = 3
        try:
            for i in range(6):
                sessions.save(sessions.new_id(), [{"role": "user", "content": f"q{i}"}])
                time.sleep(0.002)
            self.assertLessEqual(len(sessions.listing()), 3)
        finally:
            importlib.reload(sessions)


class ThePage(unittest.TestCase):
    """Catch the mistake that kept the key dialog open.

    An edit added JavaScript referencing `#tierAsk` while the edit that was supposed to CREATE
    that element silently did not match. `$("#tierAsk").style` then threw, the save handler
    died halfway, and the dialog never closed -- so a working key looked like a broken app.
    Nothing in Python could see it, because the bug lived in a string.
    """

    PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "boardofdirectors", "web", "index.html")

    def page(self):
        with open(self.PAGE) as f:
            return f.read()

    def test_every_element_the_script_reaches_for_exists(self):
        h = self.page()
        ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', h))
        used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', h))
        self.assertEqual(sorted(used - ids), [], "script references elements that do not exist")

    def test_a_closed_dialog_is_actually_hidden(self):
        """close() cleared `open` while the CSS kept painting the box.

        The probe that "proved" the dialog closed asked for `.open`, which was false, and
        never asked whether anything was still on the screen. Styling `dialog` unconditionally
        with `display:flex` overrides the browser's own `display:none` for the closed state,
        so both dialogs were permanently visible. Measure the thing the user sees, not the
        flag that is supposed to cause it.
        """
        h = self.page()
        self.assertIn("dialog:not([open])", h,
                      "nothing hides a closed dialog")
        self.assertNotRegex(h, r"(?m)^\s*dialog\{[^}]*display:\s*flex",
                            "bare `dialog{display:flex}` paints the closed state too")

    def test_control_groups_do_not_share_a_class(self):
        """Two independent filters shared one class and cleared each other's highlight.

        The rank tabs (best at coding / thinking) and the tier pills (free / paid / both) are
        different questions that COMPOSE - "best at coding, among free models". Both carried
        `class="sort"`, and the rank handler did
        `querySelectorAll(".sort").forEach(x => x.classList.toggle("on", x === b))`, so
        clicking a score tab deselected the tier pills. Nothing threw; the filters just looked
        broken, which is how it got reported.
        """
        h = self.page()
        rank = set(re.findall(r'class="[^"]*\brank\b[^"]*"[^>]*data-k="([a-z]+)"', h))
        tier = set(re.findall(r'class="[^"]*\btier\b[^"]*"[^>]*id="([a-zA-Z]+)"', h))
        self.assertTrue(rank, "no rank tabs found")
        self.assertTrue(tier, "no tier pills found")
        # every group-wide toggle must name its OWN group, never the shared base class
        for sel in re.findall(r'querySelectorAll\("\.([a-z]+)"\)\.forEach\(x => x\.classList', h):
            self.assertNotEqual(sel, "sort",
                                "a group toggle on the shared class clears the other group")

    def test_model_output_is_escaped_before_it_reaches_the_page(self):
        """Every string a model produced lands in innerHTML. One unescaped and the page is
        executing whatever a free model felt like returning."""
        h = self.page()
        for expr in ("a.text", "ev.text", "r.text", "r.decision", "ev.reason", "f.reason"):
            for m in re.finditer(re.escape("${" + expr + "}"), h):
                ctx = h[max(0, m.start() - 60):m.start()]
                self.assertIn("esc(", ctx + "${" + expr + "}",
                              f"${{{expr}}} reaches the page unescaped")
            # and the escaped form must actually appear somewhere
            self.assertIn(f"esc({expr})", h, f"{expr} is never escaped anywhere")

    def test_esc_neutralises_the_characters_that_open_a_tag(self):
        h = self.page()
        m = re.search(r"const esc = (.+?);\n", h, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        for ch in ("&", "<", ">"):
            self.assertIn(ch, body, f"esc does not handle {ch}")

    def test_tags_are_balanced(self):
        """A half-applied edit left a stray closing tag inside the dialog."""
        h = self.page()
        for tag in ("div", "dialog", "section", "aside"):
            opens = len(re.findall(rf"<{tag}[\s>]", h))
            closes = len(re.findall(rf"</{tag}>", h))
            self.assertEqual(opens, closes, f"<{tag}> opened {opens} times, closed {closes}")

    def test_the_endpoints_the_page_calls_are_served(self):
        h = self.page()
        called = set(re.findall(r'"(/api/[a-z_]+)"', h))
        with open(os.path.join(os.path.dirname(self.PAGE), "..", "server.py")) as fh:
            src = fh.read()
        served = set(re.findall(r'self\.path(?:\s*==\s*|\.startswith\()\s*"(/api/[a-z_]+)"', src))
        self.assertEqual(sorted(called - served), [], "page calls endpoints the server does not serve")


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
