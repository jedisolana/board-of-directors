"""The tests that matter are the failure paths.

A board that works when every model answers is easy. The whole value of this thing is what it
does when a member is throttled, when the seam sees a key, or when the pool has no
independent members left -- so that is what most of these check.
"""
import importlib
import os
import re
import shutil
import tempfile
import threading
import unittest

from boardofdirectors import board, budget, catalogue, config, redact, seats, usage
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
        for q in ("build me an FPS game", "write a TOML parser",
                  "fix the crash in transport.py", "refactor this module"):
            self.assertTrue(board.looks_like_a_task(q), q)
        for q in ("should we rewrite the parser?", "is postgres better than sqlite?",
                  "which model is best for this?", "do we need a queue here?"):
            self.assertFalse(board.looks_like_a_task(q), q)


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
