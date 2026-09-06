"""The tests that matter are the failure paths.

A board that works when every model answers is easy. The whole value of this thing is what it
does when a member is throttled, when the seam sees a key, or when the pool has no
independent members left -- so that is what most of these check.
"""
import contextlib
import datetime
import importlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import typing
import unittest

# ----------------------------------------------------------------------------- no network
# The suite makes no network calls - the CI comment says so, and one test quietly made four
# real requests to OpenRouter with a fake key for months. This makes the promise mechanical:
# any urlopen to a host that is not loopback raises. A test that needs the wire has a bug.
import urllib.request as _ur

from boardofdirectors import (
    atomic,
    board,
    budget,
    catalogue,
    codebase,
    config,
    cost,
    openai_api,
    patch,
    recipes,
    redact,
    seats,
    server,
    sessions,
    truecount,
    usage,
)
from boardofdirectors.transport import (
    Answer,
    Capped,
    Failure,
    OfflineTransport,
    OpenRouterTransport,
)

_real_urlopen = _ur.urlopen


def _no_network(url, *a, **k):
    full = url.full_url if hasattr(url, "full_url") else str(url)
    if "://127.0.0.1" in full or "://localhost" in full or "://[::1]" in full:
        return _real_urlopen(url, *a, **k)
    raise OSError(f"the test suite is offline: refused {full}")


_ur.urlopen = _no_network


def model(mid, ctx=100000, out=8000, params=("max_tokens", "temperature"), mods=("text",),
          free=None, price_in=0.0, price_out=0.0):
    # `free` is not optional in the real catalogue - every one of the 431 entries carries it,
    # because the money gates read it. A fixture that leaves it off is a fixture where every
    # model silently looks paid, which is how a test came to assert that a paid model is
    # served without permission.
    return {"id": mid, "name": mid, "family": mid.split("/")[0], "context_length": ctx,
            "max_completion_tokens": out, "is_moderated": False,
            "free": mid.endswith(":free") if free is None else free,
            "price_in": price_in, "price_out": price_out,
            "input_modalities": list(mods), "supported_parameters": sorted(params)}


POOL = [
    model("alpha/one:free", ctx=200000, params=("max_tokens", "temperature", "response_format")),
    model("alpha/two:free", ctx=900000, params=("max_tokens", "temperature", "response_format")),
    model("beta/one:free", ctx=150000),
    model("gamma/one:free", ctx=300000, params=("max_tokens", "temperature", "structured_outputs")),
    model("delta/one:free", ctx=50000, out=2000),
    model("epsilon/one:free", ctx=400000),
]



def symlink_or_skip(test, target, link):
    """Windows needs a privilege for symlinks that CI runners do not have. The feature under
    test is the guard against following one - if the OS cannot make one, there is nothing to
    guard, and an OSError here is not a failure of ours."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        test.skipTest(f"symlinks unavailable here: {e}")


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
            "ssh to 100.64.0.1"          # the CGNAT range's own first address, nobody's,
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
            # Labelled blocks, one per OTHER member: a ranker judges everyone but itself,
            # so with four answers each ranking prompt carries exactly three.
            self.assertEqual(p.count("--- Member "), 3)

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
            f.write('{"days": {"2021-06-07": {"calls": 7, "failed": 0, "models": {}}}}')
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
            # No sleeping between retries and no wire: this asserts what would be SENT, and
            # the unpatched version made four real requests to OpenRouter with a fake key.
            t = OpenRouterTransport("sk-or-v1-" + "a" * 64, meter=False, sleep=lambda _s: None)
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
        start, end = truecount._utc_day_bounds(datetime.date(2021, 6, 7))
        self.assertEqual(start, "2021-06-07T00:00:00Z")
        self.assertEqual(end, "2021-06-08T00:00:00Z")

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
        usage.record("a/one", ok=True, day="2021-06-01")
        usage.record("a/one", ok=True)
        usage.reset_today()
        self.assertEqual(usage.status(0).calls, 0)
        self.assertEqual(len([r for r in usage._load()["days"] if r == "2021-06-01"]), 1)


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

    def test_the_package_imports_without_fcntl(self):
        """fcntl is Unix-only, and it was imported at module scope in two files.

        The package did not import AT ALL on Windows - an ImportError before anything ran,
        under a README promising no dependencies and Python 3.10+. CI ran Ubuntu and macOS,
        so it could never have caught it. It runs Windows now, and this asserts the same
        thing without needing one.
        """
        import subprocess
        probe = (
            "import sys\n"
            "class B:\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n == 'fcntl': raise ImportError('no fcntl')\n"
            "sys.meta_path.insert(0, B())\n"
            "import boardofdirectors\n"
            "from boardofdirectors import atomic, usage, config\n"
            "print('ok')\n"
        )
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           cwd=root)
        self.assertEqual(r.returncode, 0, f"import fails without fcntl:\n{r.stderr[-600:]}")

    def test_locking_still_serialises_threads_without_fcntl(self):
        """Losing cross-PROCESS locking on Windows is acceptable. Losing cross-THREAD locking
        is not - threads are the case that actually bit, from one parallel board."""
        import boardofdirectors.atomic as a
        real, a.fcntl = a.fcntl, None
        try:
            atomic.write_json(self.p, {"n": 0})

            def hammer():
                for _ in range(20):
                    with a.locked(self.p):
                        c = a.read_json(self.p, {}) or {}
                        c["n"] = c.get("n", 0) + 1
                        a.write_json(self.p, c)

            ts = [threading.Thread(target=hammer) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(a.read_json(self.p)["n"], 160)
        finally:
            a.fcntl = real

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
        patch.apply(ch[0], self.root, expect_digest=patch.digest(ch[0].old), backup_dir=backups)
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
            patch.apply(ch[0], self.root, expect_digest=stale)
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

    def test_two_session_ids_cannot_become_one_file(self):
        """The id was CLEANED rather than refused, and cleaning is lossy: `a/../../b` and
        `a\\x00b` both wrote `ab.json`, so loading one handed back the other's session.

        Traversal never escaped - every write landed in the sessions directory - so the guard
        that was there did the job it was written for and quietly did a different damage.
        `patch.parse` has a comment about exactly this, about rewriting a hostile path into a
        plausible one instead of refusing it; the same mistake was one directory away.
        """
        seen = {}
        for sid in ("a/../../b", "a\x00b", "....//x", "../../etc/passwd", "/etc/passwd"):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                sessions.save(sid, [{"role": "user", "content": "x"}])
        # and the ids this program actually makes are all still fine, and all distinct
        for _ in range(50):
            sid = sessions.new_id()
            self.assertNotIn(sid, seen, "new_id collided")
            seen[sid] = sessions.save(sid, [{"role": "user", "content": "x"}])
        self.assertEqual(len(set(seen.values())), len(seen), "two ids share one file")

    def test_a_session_id_cannot_escape_the_directory(self):
        """It is ours, but it still arrives from an HTTP request. Every shape a scanner worries
        about, and a few it does not: URL encoding, a null byte, Windows separators, and the
        doubled-dot trick that survives one round of stripping."""
        for bad in ("../../etc/passwd", "/etc/passwd", "..", "", ".",
                    "..%2f..%2fetc%2fpasswd", "a/../../b", "....//....//x",
                    "C:\\Windows\\system32\\x", "\x00etc/passwd", "~/.ssh/id_rsa",
                    "." * 300, "%2e%2e/%2e%2e/x"):
            with self.subTest(bad=bad):
                try:
                    p = sessions._path(bad)
                except ValueError:
                    continue
                root = os.path.abspath(sessions.DIR)
                self.assertTrue(os.path.abspath(p).startswith(root + os.sep),
                                f"{bad!r} produced {p}")
                self.assertEqual(os.path.dirname(os.path.abspath(p)), root)

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

    def test_esc_also_neutralises_the_characters_that_close_an_attribute(self):
        """`&<>` is the right set for TEXT and not enough for an attribute, and the same
        function serves both. It was used in thirteen places inside a quoted attribute -
        `title="${esc(...)}"`, `data-rel="${esc(...)}"` - while escaping neither quote, so a
        value carrying a `"` closed the attribute early and everything after it became real
        markup. A file named `x" onmouseover="alert(1)` in a scanned folder was enough, and its
        name reaches the page through the apply button."""
        h = self.page()
        body = re.search(r"const esc = (.+?);\n", h, re.S).group(1)
        for ch, entity in (('"', "quot"), ("'", "#39")):
            self.assertIn(entity, body, f"esc does not turn {ch} into an entity")

    def test_every_attribute_that_interpolates_is_escaped(self):
        """Anything dropped inside a quoted attribute goes through esc, or is on the list
        below with a reason. The list is the point: a value that cannot carry a quote is safe
        raw, and saying WHY keeps the next addition from being waved through.
        """
        # expression -> why it cannot carry a `"`
        ALLOWED = {
            'sc(m)==null?"none":""': "a ternary between two literals",
            'kind': "callers pass a literal class name: warn, bad, ok",
            '(a.vote||"UNCLEAR").toLowerCase()': "read_vote returns one of four constants",
            '(ev.vote||"UNCLEAR").toLowerCase()': "read_vote returns one of four constants",
            't.split||t.decided===0?"tied":(t.carried?"yes":"no")': "a ternary between literals",
            'm.free ? "free" : ""': "a ternary between two literals",
            'i': "a loop index",
            's.id===sessionId?"on":""': "a ternary between two literals",
        }
        h = self.page()
        raw = []
        for m in re.finditer(r'=\"([^\"]*?\$\{[^}]+\}[^\"]*?)\"', h):
            for expr in re.findall(r"\$\{([^}]+)\}", m.group(1)):
                e = expr.strip()
                if "esc(" in e or e in ALLOWED:
                    continue
                raw.append(e)
        self.assertEqual(sorted(set(raw)), [],
                         "unescaped interpolation inside an attribute - escape it, or add it "
                         "to ALLOWED with the reason it cannot carry a quote")

    def test_the_vote_that_reaches_a_class_attribute_is_a_constant(self):
        """The one entry above that is model-derived rather than a literal. It is safe because
        the SERVER constrains it, so that is where the guard has to be - if read_vote ever
        returned the model's own words, the console would be putting them in an attribute."""
        for text in ('VOTE: FOR', 'VOTE: AGAINST', 'VOTE: DEPENDS', 'nothing declared',
                     'VOTE: FOR" onmouseover="alert(1)', 'VOTE: <img src=x onerror=alert(1)>'):
            with self.subTest(text=text):
                self.assertIn(board.read_vote(text), board.VOTES)

    def test_tags_are_balanced(self):
        """A half-applied edit left a stray closing tag inside the dialog."""
        h = self.page()
        for tag in ("div", "dialog", "section", "aside"):
            opens = len(re.findall(rf"<{tag}[\s>]", h))
            closes = len(re.findall(rf"</{tag}>", h))
            self.assertEqual(opens, closes, f"<{tag}> opened {opens} times, closed {closes}")


    def test_every_interpolated_title_attribute_is_escaped(self):
        """The one sink that skipped esc() was a title attribute fed from the catalogue.

        Scores arrive from a third-party feed and went into `title="${scoreTip(m)}"` raw --
        a quote in that string ends the attribute and starts whatever it likes, in a page
        that holds the key and can write files. The rule is structural so the next attribute
        someone adds fails here, not in a stranger's browser.
        """
        h = self.page()
        for m in re.finditer(r'title="\$\{', h):
            tail = h[m.end():m.end() + 4]
            self.assertEqual(tail, "esc(",
                             f"an interpolated title attribute is not escaped: "
                             f"...{h[m.start():m.start() + 40]}...")

    def test_a_score_from_the_feed_is_a_number_or_nothing(self):
        """The server side of the same defence: a float cannot carry markup."""
        raw = {"id": "x/y", "pricing": {}, "top_provider": {}, "architecture": {},
               "benchmarks": {"artificial_analysis": {
                   "coding_index": '"><img src=x onerror=alert(1)>',
                   "intelligence_index": 61.549, "agentic_index": True}}}
        sc = catalogue._normalise(raw)["score"]
        self.assertIsNone(sc["coding"])
        self.assertEqual(sc["thinking"], 61.5)
        self.assertIsNone(sc["agentic"], "True is not a benchmark result")


    def test_the_board_and_saved_panels_can_be_folded_away(self):
        """Browsing four hundred models with six seats and last week's sessions pinned
        underneath left the list a third of the pane. Both fold; the fold is remembered."""
        h = self.page()
        for btn, body in (("foldBoard", "seated"), ("foldSaved", "sessions")):
            self.assertIn(f'id="{btn}"', h, f"no fold button {btn}")
            self.assertIn(f'id="{body}"', h, f"fold target {body} missing")
        m = re.search(r"const FOLDS = \{(.*?)\}", h)
        self.assertIsNotNone(m)
        for pair in m.group(1).split(","):
            btn, body = (x.strip().strip('"') for x in pair.split(":"))
            self.assertIn(f'id="{btn}"', h)
            self.assertIn(f'id="{body}"', h)
        # storage is a convenience, never a dependency
        self.assertIn("try{ localStorage", h)
        self.assertIn("#seated[hidden],#sessions[hidden]{display:none!important}", h,
                      "a hidden panel must not be re-shown by a display rule")

    def test_the_endpoints_the_page_calls_are_served(self):
        h = self.page()
        called = set(re.findall(r'"(/api/[a-z_]+)"', h))
        with open(os.path.join(os.path.dirname(self.PAGE), "..", "server.py"), encoding="utf-8") as fh:
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


class Packaging(unittest.TestCase):
    """A `pip install` shipped a 404 console because nothing here ever looked in the wheel.

    The tests below are deliberately about the *rule* rather than the two files that broke it:
    the next asset someone adds is the one that will be forgotten, not these.
    """

    PKG = os.path.dirname(os.path.abspath(catalogue.__file__))
    ROOT = os.path.dirname(PKG)

    def test_the_files_the_program_reads_live_inside_the_package(self):
        """Anything resolved from the repo root vanishes the moment it is installed."""
        for rel in ("web/index.html", "data/free-models.json"):
            here = os.path.join(self.PKG, *rel.split("/"))
            self.assertTrue(os.path.exists(here), f"{rel} is not inside the package directory")

    def test_every_non_python_file_is_covered_by_a_package_data_glob(self):
        """The guard that generalises: add an asset, forget the glob, fail here."""
        import fnmatch
        with open(os.path.join(self.ROOT, "pyproject.toml"), encoding="utf-8") as f:
            toml = f.read()
        block = re.search(r"^boardofdirectors\s*=\s*\[(.*?)\]", toml, re.S | re.M)
        self.assertIsNotNone(block, "pyproject has no package-data entry for boardofdirectors")
        globs = re.findall(r'"([^"]+)"', block.group(1))

        for dirpath, dirnames, filenames in os.walk(self.PKG):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), self.PKG).replace(os.sep, "/")
                if rel.endswith(".py"):
                    continue
                self.assertTrue(any(fnmatch.fnmatch(rel, g) for g in globs),
                                f"{rel} ships in no package-data glob - a pip install will not have it")

    def test_the_snapshot_path_is_package_relative(self):
        """It was repo-relative, so the offline fallback only worked from a git checkout."""
        self.assertTrue(catalogue.SNAPSHOT.startswith(self.PKG + os.sep),
                        f"SNAPSHOT points outside the package: {catalogue.SNAPSHOT}")


class Symlinks(unittest.TestCase):
    """`.env -> ~/secrets/.env` is an ordinary thing to have in a project.

    The scanner read straight through it, which meant the contents of a file outside the
    folder went into a prompt sent to several outside companies. Nothing in the folder looked
    wrong, and the audit report listed it as just another source file.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = os.path.join(self.tmp, "proj")
        self.out = os.path.join(self.tmp, "outside")
        os.makedirs(self.proj)
        os.makedirs(self.out)
        with open(os.path.join(self.proj, "main.py"), "w", encoding="utf-8") as f:
            f.write("print(1)\n")
        with open(os.path.join(self.out, "creds.env"), "w", encoding="utf-8") as f:
            f.write("AWS_SECRET=hunter2\n")

    def link(self, name, target):
        symlink_or_skip(self, target, os.path.join(self.proj, name))

    def test_a_link_out_of_the_folder_is_never_read(self):
        self.link("config.py", os.path.join(self.out, "creds.env"))
        s = codebase.scan(self.proj)
        self.assertEqual([f.rel for f in s.files], ["main.py"])
        self.assertNotIn("hunter2", "".join(f.text for f in s.files))
        self.assertIn("config.py", [rel for rel, _ in s.skipped])

    def test_a_link_inside_the_folder_still_works(self):
        """The fix must not break a monorepo that links one of its own files."""
        with open(os.path.join(self.proj, "real.py"), "w", encoding="utf-8") as f:
            f.write("X = 1\n")
        self.link("alias.py", os.path.join(self.proj, "real.py"))
        s = codebase.scan(self.proj)
        self.assertIn("alias.py", [f.rel for f in s.files])

    def test_containment_resolves_both_sides(self):
        """Resolving only the file is the classic way to write this wrong: on macOS /tmp is
        itself a link, so every file under a /tmp root would look like an escape."""
        self.assertTrue(codebase.inside(self.proj, os.path.join(self.proj, "main.py")))
        self.assertFalse(codebase.inside(self.proj, os.path.join(self.out, "creds.env")))
        self.assertTrue(codebase.inside("/tmp", "/tmp"))

    def test_a_symlinked_folder_is_reported_not_silently_dropped(self):
        symlink_or_skip(self, self.out, os.path.join(self.proj, "shared"))
        s = codebase.scan(self.proj)
        self.assertIn("shared", [rel for rel, _ in s.skipped])
        self.assertNotIn("hunter2", "".join(f.text for f in s.files))

    def test_the_write_side_refuses_a_link_even_if_it_is_allowed(self):
        """The allowlist is data. The check that counts stands next to the write."""
        self.link("config.py", os.path.join(self.out, "creds.env"))
        text = "----- config.py -----\nowned = True\n"
        changes, notes = patch.parse(text, self.proj, allowed={"config.py"})
        self.assertEqual(changes, [])
        self.assertTrue(any("outside the folder" in n for n in notes), notes)
        with open(os.path.join(self.out, "creds.env"), encoding="utf-8") as f:
            self.assertIn("hunter2", f.read())


class ClosedTab(unittest.TestCase):
    """Closing the tab is the most ordinary thing a person does to a dashboard.

    It must not print a traceback in the terminal they are still looking at.
    """

    class DeadPipe(io.BytesIO):
        def write(self, b): raise BrokenPipeError(32, "Broken pipe")
        def flush(self): pass

    class Handler:
        def __init__(self): self.wfile = ClosedTab.DeadPipe()
        def send_response(self, *a): pass
        def send_header(self, *a): pass
        def end_headers(self): pass

    def run_with(self, fake_board):
        real_board, real_state = server._board, server._state
        server._board, server._state = fake_board, lambda: {"usage": {}}
        try:
            server._stream_board(self.Handler(), {})
        finally:
            server._board, server._state = real_board, real_state

    def test_nothing_escapes_whenever_the_tab_closes(self):
        """The `done` push sat outside the guard, so finishing a board and closing the tab in
        the same second was the one moment that still threw."""
        for name, fake in (
            ("after a clean finish", lambda p, on_event=None: {"answers": [], "calls": 0}),
            ("after an error",       lambda p, on_event=None: {"error": "boom"}),
            ("mid-run",              lambda p, on_event=None: on_event({"type": "answer"})),
        ):
            with self.subTest(name):
                self.run_with(fake)     # must not raise

    def test_the_server_stays_quiet_about_a_dropped_connection(self):
        printed = []
        srv = server._Server.__new__(server._Server)
        try:
            raise ConnectionResetError(54, "Connection reset by peer")
        except ConnectionResetError:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                srv.handle_error(None, ("127.0.0.1", 1))
            printed.append(err.getvalue())
        self.assertEqual(printed[0], "", "a closed tab printed to stderr")

    def test_a_real_error_is_still_printed(self):
        srv = server._Server.__new__(server._Server)
        try:
            raise ValueError("a genuine bug")
        except ValueError:
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.suppress(Exception):
                srv.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("a genuine bug", err.getvalue(), "a real error was swallowed")


class NeverSpendsWithoutPermission(unittest.TestCase):
    """The one invariant in this program that costs real money when it is wrong.

    Both single-model paths reached `transport.ask` with no paid check on them at all. The
    board path was gated, the picker only offers free models while paid is off, and so the
    hole stayed invisible: it needed a stale tab, a remembered choice, or any local program
    posting to the endpoint - and then it spent money with the cap sitting at $0.00.
    """

    PAID = model("costly/opus", free=False, price_in=15.0, price_out=75.0)
    FREE = model("cheap/one:free")

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old_home = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        self.calls = []
        outer = self

        class Spy:
            def ask(self, m, msgs, **kw):          # Transport.ask takes **kw; the fence uses it
                outer.calls.append(m["id"])
                return board.Answer(model=m["id"], text="hi")

        self.spy = Spy()
        self.old_cache = dict(server._CACHE)
        self.old_transport = server._transport
        server._CACHE["models"] = [self.PAID, self.FREE]
        server._CACHE["at"] = time.time() + 10_000
        server._transport = lambda offline: (self.spy, True)

    def tearDown(self):
        server._CACHE.clear()
        server._CACHE.update(self.old_cache)
        server._transport = self.old_transport
        if self.old_home is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old_home
        importlib.reload(config)

    def single(self, mid, **extra):
        self.calls.clear()
        return server._single({"model": mid, "messages": [{"role": "user", "content": "x"}],
                               **extra})

    def test_the_console_will_not_call_a_paid_model_with_the_cap_at_zero(self):
        config.set_model_tier("free")
        config.set_spend_cap(0.0)
        out = self.single("costly/opus")
        self.assertIn("paid model", out.get("error", ""))
        self.assertEqual(self.calls, [], "a paid model was called with spending locked off")

    def test_a_zero_cap_beats_explicit_consent(self):
        """Someone who set the cap to zero said that about their money, not about a checkbox."""
        config.set_model_tier("both")
        config.set_spend_cap(0.0)
        out = self.single("costly/opus", allow_paid=True)
        self.assertIn("locked off", out.get("error", ""))
        self.assertEqual(self.calls, [])

    def test_the_toggle_alone_is_not_consent_for_this_send(self):
        """A setting from last week must not be what decides today's question costs money."""
        config.set_model_tier("both")
        config.set_spend_cap(5.0)
        out = self.single("costly/opus")                    # no allow_paid on the request
        self.assertIn("not allowed on this send", out.get("error", ""))
        self.assertEqual(self.calls, [], "the stored setting alone let a paid model through")

    def test_permission_plus_headroom_does_work(self):
        """The gate has to let the paying customer pay, or it is just broken."""
        config.set_model_tier("both")
        config.set_spend_cap(5.0)
        out = self.single("costly/opus", allow_paid=True)
        self.assertNotIn("error", out)
        self.assertEqual(self.calls, ["costly/opus"])

    def test_a_free_model_is_never_caught_by_the_gate(self):
        config.set_model_tier("free")
        config.set_spend_cap(0.0)
        out = self.single("cheap/one:free")
        self.assertNotIn("error", out)
        self.assertEqual(self.calls, ["cheap/one:free"])

    def test_the_openai_endpoint_is_gated_too(self):
        """Anything speaking the dialect can name a model. It walked straight past the gate."""
        for allow, expect_status, expect_calls in ((False, 403, []), (True, 200, ["costly/opus"])):
            with self.subTest(allow_paid=allow):
                self.calls.clear()
                _, status = openai_api.run(
                    {"model": "costly/opus", "messages": [{"role": "user", "content": "x"}]},
                    [self.PAID, self.FREE], self.spy, allow_paid=allow, tier="free")
                self.assertEqual(status, expect_status)
                self.assertEqual(self.calls, expect_calls)

    def test_an_all_paid_board_says_so_instead_of_crashing(self):
        """Driven through the real endpoint, because the bug was in the handler.

        Paid members are filtered out AFTER seating, so a saved board of only paid models
        empties the list. `min()` over nothing is a ValueError, and the person gets a 500
        naming nothing they could act on.
        """
        import http.client
        config.set_model_tier("free")
        config.set_spend_cap(0.0)
        config.set_board([self.PAID["id"]])

        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        with open(os.path.join(folder, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        srv = server._Server(("127.0.0.1", 0), server.Handler)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)

        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request("POST", "/api/work",
                     json.dumps({"path": folder, "task": "tidy up"}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual(resp.status, 200, "an emptied board returned a server error")
        self.assertIn("every model on your board is paid", body.get("error", ""))
        self.assertEqual(self.calls, [], "it called out despite an all-paid board")


class TheOnlyPlaceThatWrites(unittest.TestCase):
    """/api/apply took `root` and `rel` from the request and joined them.

    Nothing checked. The parser refused traversal with real care, and then the writer -- the
    one function in the program that touches somebody's disk -- wrote wherever the path it
    was handed pointed, and reported it as success. Two rules where there should have been
    one, and the loose one was the one holding the pen.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old_home = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.proj = os.path.join(self.work, "proj")
        self.out = os.path.join(self.work, "outside")
        os.makedirs(self.proj)
        os.makedirs(self.out)
        self.victim = os.path.join(self.out, "authorized_keys")
        with open(self.victim, "w", encoding="utf-8") as f:
            f.write("original\n")
        with open(os.path.join(self.proj, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        self.srv = server._Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old_home
        importlib.reload(config)

    def apply(self, rel, new="PWNED\n"):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/apply",
                  json.dumps({"root": self.proj, "rel": rel, "new": new}),
                  {"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())

    def test_no_shape_of_path_escapes_the_folder(self):
        for rel in ("../outside/authorized_keys", "../../etc/hosts", "/etc/hosts",
                    "./../outside/authorized_keys", "sub/../../outside/authorized_keys"):
            with self.subTest(rel):
                r = self.apply(rel)
                self.assertIn("error", r, f"{rel} was accepted")
        with open(self.victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original\n", "a file outside the folder was written")

    def test_a_symlink_out_of_the_folder_is_not_a_way_in(self):
        symlink_or_skip(self, self.victim, os.path.join(self.proj, "link.py"))
        r = self.apply("link.py")
        self.assertIn("error", r)
        with open(self.victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "original\n")

    def test_an_ordinary_write_still_works(self):
        r = self.apply("a.py", "x = 2\n")
        self.assertIn("applied", r, r)
        with open(os.path.join(self.proj, "a.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "x = 2\n")

    def test_the_writer_refuses_a_change_whose_path_contradicts_its_rel(self):
        """`Change` carries both, and only one of them was ever checked."""
        ch = patch.Change(rel="a.py", path=self.victim, new="PWNED\n")
        with self.assertRaises(patch.Rejected):
            patch.apply(ch, self.proj)

    def test_the_parser_and_the_writer_share_one_rule(self):
        """They drifted apart once. Same function now, so they cannot again."""
        for rel in ("../x", "/etc/hosts", "../../../etc/passwd", "sub/../../out.txt",
                    "..\\..\\win.txt", "./../x"):
            with self.subTest(rel), self.assertRaises(patch.Rejected):
                patch.contained(self.proj, rel)
        self.assertEqual(patch.contained(self.proj, "a.py"),
                         os.path.join(self.proj, "a.py"))

    def test_a_tilde_is_a_filename_not_a_home_directory(self):
        """`~/.ssh/id_rsa` is allowed, and lands in a folder literally called `~` inside the
        project. That is right: the rule is containment, and `~` is a legal file name. It is
        only dangerous if something later calls expanduser on the result - nothing does, and
        this is the test that says so."""
        full = patch.contained(self.proj, "~/.ssh/id_rsa")
        self.assertTrue(full.startswith(os.path.abspath(self.proj) + os.sep))
        self.assertIn("~", full)
        self.assertNotEqual(full, os.path.expanduser("~/.ssh/id_rsa"))

    def test_a_symlink_pointing_out_of_the_folder_is_refused(self):
        """`.env -> ~/secrets/.env` is an ordinary thing to have in a project. Writing through
        one puts the board's text somewhere the owner never named."""
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as fh:
            fh.write("SECRET\n")
        try:
            os.symlink(os.path.join(outside, "secret.txt"),
                       os.path.join(self.proj, "link.txt"))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks unavailable: {e}")
        with self.assertRaises(patch.Rejected):
            patch.contained(self.proj, "link.txt")


class BlindMeansBlindToYourselfToo(unittest.TestCase):
    """The ranking prompt has always said "the other members' answers".

    The code sent all of them, own answer included, so every member quietly judged a line-up
    with itself in it. Models prefer their own text even with the name taken off -- that is
    the exact bias a jury drawn from different companies exists to kill, and it ran through
    every board this program ever convened.
    """

    def run_board(self):
        sent = {}

        class Spy(OfflineTransport):
            def ask(self, m, messages):
                sent.setdefault(m["id"], []).append(messages[-1]["content"])
                return super().ask(m, messages)

        s = board.ask("Should we ship on Friday?", transport=Spy(), models=POOL,
                      members=POOL[:3], live_catalogue=False)
        return s, sent

    def rank_prompt(self, sent, mid):
        got = [p for p in sent[mid] if "Rank them" in p]
        return got[0] if got else None

    def test_no_ranker_is_shown_its_own_answer(self):
        s, sent = self.run_board()
        own = {a.model: a.text for a in s.answers}
        for mid in own:
            rp = self.rank_prompt(sent, mid)
            if rp is None:
                continue
            self.assertNotIn(own[mid], rp, f"{mid} was asked to rank itself")

    def test_every_ranker_still_sees_everyone_else(self):
        """Excluding yourself must not become excluding anybody."""
        s, sent = self.run_board()
        own = {a.model: a.text for a in s.answers}
        for mid in own:
            rp = self.rank_prompt(sent, mid)
            if rp is None:
                continue
            for other, text in own.items():
                if other != mid:
                    self.assertIn(text, rp, f"{mid} was not shown {other}'s answer")

    def test_the_chair_still_reads_the_full_blind_set(self):
        """The chair did not answer, so nothing is its own; it needs all of them."""
        s, sent = self.run_board()
        chair_prompts = [p for p in sent[s.chair_model["id"]] if "BLIND RANKINGS" in p]
        self.assertTrue(chair_prompts)
        for a in s.answers:
            self.assertIn(a.text, chair_prompts[0])

    def test_labels_stay_global_across_rankers(self):
        """Member B must mean the same answer in every ranking the chair lines up."""
        s, sent = self.run_board()
        by_model = {v: k for k, v in s.labels.items()}
        own = {a.model: a.text for a in s.answers}
        for mid in own:
            rp = self.rank_prompt(sent, mid)
            if rp is None:
                continue
            for other in own:
                if other != mid:
                    block = f"--- {by_model[other]} ---"
                    self.assertIn(block, rp, f"{other}'s label changed for ranker {mid}")


class TheChairWalkIsBudgeted(unittest.TestCase):
    """Four failed chairs is a fallback; eighteen is a death march.

    At the edge of the daily limit this is the common case, not the strange one: the members
    spend the last requests answering, and then every chair candidate 429s. The reset is at
    midnight, not fifteen seconds from now -- walking the whole catalogue from there costs
    two requests and a backoff per candidate for a conclusion that is already foregone.
    """

    class ChairsAllDown(OfflineTransport):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.chairs_asked = []

        def ask(self, m, messages):
            if "BLIND RANKINGS" in messages[-1]["content"]:
                self.chairs_asked.append(m["id"])
                return Failure(m["id"], "rate limited", status=429)
            return super().ask(m, messages)

    def test_four_failed_chairs_end_the_walk(self):
        pool = [model(f"fam{i}/m:free") for i in range(20)]
        t = self.ChairsAllDown()
        s = board.ask("ship it?", transport=t, models=pool, members=pool[:4],
                      live_catalogue=False)
        self.assertEqual(len(t.chairs_asked), 4, "the walk was not capped")
        self.assertEqual(len(s.answers), 4, "the members' answers were thrown away")
        self.assertIn("chairs failed in a row", s.no_quorum)

    def test_one_bad_chair_still_falls_through_to_a_good_one(self):
        """The cap must not break the fallback it is capping."""
        pool = [model(f"fam{i}/m:free") for i in range(8)]

        class FirstChairDown(OfflineTransport):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fell = []

            def ask(self, m, messages):
                if "BLIND RANKINGS" in messages[-1]["content"] and not self.fell:
                    self.fell.append(m["id"])
                    return Failure(m["id"], "rate limited", status=429)
                return super().ask(m, messages)

        s = board.ask("ship it?", transport=FirstChairDown(), models=pool,
                      members=pool[:4], live_catalogue=False)
        self.assertIsNone(s.no_quorum)
        self.assertTrue(s.decision)
        self.assertEqual(len(s.chair_failures), 1)


class CredentialsInsideAUrl(unittest.TestCase):
    """A database URL with the password sitting inside it is how half the world writes
    credentials into a config file, and the seam let that shape straight through to six
    companies."""

    def hit(self, text):
        return any(f.rule == "url credentials" for f in redact.scan(text))

    def test_the_common_shapes_are_caught(self):
        # Hosts are RFC 2606 reserved (.invalid can never resolve) and the passwords say what
        # they are. The first version used a real-looking Atlas host, and GitHub's secret
        # scanner raised a "public leak" alert on the repository within the hour - correctly,
        # by its own rules. A fixture for a secret detector must not look like a secret to
        # anyone else's secret detector.
        for t in ("postgres://user:EXAMPLE-NOT-A-SECRET@db.example.invalid:5432/prod",
                  "DATABASE_URL=mysql://root:EXAMPLE-NOT-A-SECRET@db.example.invalid/app",
                  "amqp://guest:EXAMPLE-NOT-A-SECRET@queue.example.invalid:5672//",
                  "mongodb+srv://user:EXAMPLE-NOT-A-SECRET@cluster.example.invalid/db",
                  "redis://:EXAMPLE-NOT-A-SECRET@cache.example.invalid:6379"):   # password, no username
            with self.subTest(t):
                self.assertTrue(self.hit(t), f"let through: {t}")


    def test_no_fixture_here_looks_like_a_real_credential(self):
        """The generalising guard. A test fixture for a secret detector must not trip anybody
        else's secret detector - GitHub raised a public-leak alert on this repository over a
        connection string invented for the test above. Hosts must be RFC 2606 reserved, and a
        password must announce itself as one."""
        import pathlib
        src = pathlib.Path(__file__).read_text(encoding="utf-8")
        for m in re.finditer(r"[a-z][a-z0-9+.-]{1,20}://[^/\s:@\"']{0,64}:([^@\s\"']{1,128})@([^/\s\"']+)",
                             src):
            password, host = m.group(1), m.group(2).split(":")[0]      # drop any :port
            with self.subTest(host=host):
                self.assertTrue(host.endswith((".invalid", ".example", ".test", ".localhost"))
                                or host in ("localhost", "127.0.0.1"),
                                f"{host} is not a reserved example host")
                self.assertIn("EXAMPLE", password.upper(),
                              f"the password {password!r} does not announce itself as a fixture")

    def test_ordinary_urls_are_not_secrets(self):
        for t in ("http://localhost:8080/health",
                  "https://api.example.com/v1?x=1",
                  "git@github.com:user/repo.git",            # an @ but no scheme
                  "http://user@host/path",                   # a user but no password
                  "s3://bucket/key.txt",
                  "# see https://docs.python.org/3/library/re.html"):
            with self.subTest(t):
                self.assertFalse(self.hit(t), f"false positive: {t}")


class TheDocumentationRuns(unittest.TestCase):
    """Every python block in docs/library.md is executed, verbatim.

    A guide drifts the moment nothing runs it: an argument gets renamed, the example keeps
    the old name, and the first person to hit the difference is a stranger pasting the
    quickstart. Network calls are wrapped to the offline transport; nothing else is touched.
    """

    DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(board.__file__))),
                        "docs", "library.md")

    def test_every_example_runs_as_written(self):
        with open(self.DOCS, encoding="utf-8") as f:
            blocks = re.findall(r"```python\n(.*?)```", f.read(), re.S)
        self.assertGreater(len(blocks), 10, "the guide lost its examples")

        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = home
        self.addCleanup(lambda: (os.environ.__setitem__("BOARD_HOME", old) if old
                                 else os.environ.pop("BOARD_HOME", None)))

        _ask, _aic, _load = board.ask, board.ask_in_context, catalogue.load
        board.ask = lambda q, **kw: _ask(q, **{"transport": OfflineTransport(),
                                               "live_catalogue": False, **kw})
        board.ask_in_context = lambda q, **kw: _aic(q, **{"transport": OfflineTransport(),
                                                          "live_catalogue": False, **kw})
        catalogue.load = lambda live=True: _load(live=False)
        self.addCleanup(lambda: (setattr(board, "ask", _ask),
                                 setattr(board, "ask_in_context", _aic),
                                 setattr(catalogue, "load", _load)))

        # names the reference fragments leave to the reader ("whatever you have").
        # `m` is a REAL normalised model, so the guide's promised keys are checked against
        # what catalogue actually produces -- a doc that lists a field the code dropped fails.
        real_m = catalogue.snapshot()["models"][0]
        ns = {"__name__": "docexample", "text": "the plan looks fine",
              "answers": [], "m": real_m,
              "prompt_tokens": 1000, "completion_tokens": 400,
              "turns": [{"role": "user", "content": "hi"}]}
        with open(os.devnull, "w", encoding="utf-8") as null, \
                contextlib.redirect_stdout(null):
            for i, b in enumerate(blocks):
                if "OpenAI(" in b:            # the external-client example needs a console
                    continue
                if "~/Desktop/myproject" in b:  # the reader's folder, not ours
                    # as a LITERAL: a Windows temp path pasted raw into source turns \t into a tab
                    b = b.replace('"~/Desktop/myproject"', repr(home))
                with self.subTest(block=i, first_line=b.strip().splitlines()[0][:60]):
                    exec(compile(b, f"docs-example-{i}", "exec"), ns)


class Recipes(unittest.TestCase):
    """Presets are framings, not forks: each is one call into the same engine."""

    def run_(self, fn, *a, **kw):
        t = OfflineTransport()
        s = fn(*a, transport=t, models=POOL, live_catalogue=False, **kw)
        return s, t

    def test_dream_is_a_competition_and_keeps_every_dream(self):
        s, t = self.run_(recipes.dream, "a city that only exists while it is raining", size=4)
        self.assertEqual(s.kind, "make")
        self.assertEqual(len(s.answers), 4, "the point of dreaming as a board is keeping all of them")
        self.assertTrue(s.decision)
        first = next(p for _, p in t.calls if "THEME:" in p)
        self.assertIn("ONE complete piece", first)
        self.assertIn("Do the task", first)                 # wrapped by the competition prompt

    def test_check_idea_is_a_jury_with_a_count(self):
        s, t = self.run_(recipes.check_idea, "ship the dashboard first")
        self.assertEqual(s.kind, "decide")
        self.assertIn("FOR", s.tally)
        first = next(p for _, p in t.calls if "IDEA:" in p)
        self.assertIn("strongest objection", first)
        self.assertIn("VOTE: FOR", first)                   # wrapped by the jury prompt

    def test_review_asks_the_question_you_gave_it(self):
        s, t = self.run_(recipes.review, "We move to Postgres.", ask="Is the reason stated?")
        self.assertEqual(s.kind, "decide")
        first = next(p for _, p in t.calls if "TEXT:" in p)
        self.assertIn("Is the reason stated?", first)
        self.assertIn("We move to Postgres.", first)

    def test_audit_refuses_a_folder_with_a_secret_in_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "app.py"), "w", encoding="utf-8") as f:
            f.write('KEY = "sk-or-v1-' + "a" * 64 + '"\n')
        with self.assertRaises(redact.Refused):
            self.run_(recipes.audit, d)
        # the override has to be typed, and then it goes
        s, t = self.run_(recipes.audit, d, send_anyway=True)
        self.assertEqual(s.kind, "decide")
        self.assertTrue(any("app.py" in p for _, p in t.calls), "the code never reached the board")

    def test_audit_budget_follows_the_smallest_window_on_the_board(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with open(os.path.join(d, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        small = [model("a/1:free", ctx=10_000), model("b/1:free", ctx=50_000), model("c/1:free", ctx=90_000)]
        seen = {}
        real = codebase.audit_message
        codebase.audit_message = lambda sc, budget, ask="": seen.setdefault("budget", budget) and real(sc, budget, ask=ask)
        try:
            self.run_(recipes.audit, d, members=small)
        finally:
            codebase.audit_message = real
        self.assertEqual(seen["budget"], 6_000, "every member must be able to read the same tree")

    def test_every_recipe_passes_the_boards_knobs_through(self):
        """A recipe that swallowed `members=` or `peer_review=` would be a second interface."""
        s, _ = self.run_(recipes.check_idea, "x", members=POOL[:3], peer_review=False)
        self.assertEqual([m["id"] for m in s.members], [m["id"] for m in POOL[:3]])
        self.assertEqual(s.rankings, [])

    def test_brainstorm_build_and_red_team_are_competitions(self):
        for fn, arg, phrase in ((recipes.brainstorm, "x", "distinct ideas"),
                                (recipes.build, "x", "no placeholders"),
                                (recipes.red_team, "x", "Break this")):
            with self.subTest(fn.__name__):
                s, t = self.run_(fn, arg)
                self.assertEqual(s.kind, "make")
                self.assertTrue(any(phrase in p for _, p in t.calls), f"{fn.__name__} lost its framing")

    def test_brainstorm_asks_for_the_number_you_gave(self):
        _, t = self.run_(recipes.brainstorm, "x", ideas=7)
        self.assertTrue(any("Produce 7 distinct" in p for _, p in t.calls))


class TheSupplyChain(unittest.TestCase):
    """A different model works each step and hands its output down the line."""

    STEPS = ("outline it", "write it", "cut it by a third")

    def test_each_station_is_a_different_family_and_the_output_travels(self):
        t = OfflineTransport()
        line = recipes.supply_chain(self.STEPS, material="audience: engineers",
                                    transport=t, models=POOL, live_catalogue=False)
        self.assertEqual(len(line.steps), 3)
        self.assertEqual(len({x.model.split("/")[0] for x in line.steps}), 3, "a family worked twice")
        prompts = [p for _, p in t.calls]
        self.assertIn("audience: engineers", prompts[0])
        self.assertIn(line.steps[0].text, prompts[1], "station 2 was not handed station 1's work")
        self.assertIn(line.steps[1].text, prompts[2], "station 3 was not handed station 2's work")
        self.assertEqual(line.result, line.steps[-1].text)
        self.assertIsNone(line.broke_at)

    def test_a_failed_station_stops_the_line_and_is_named(self):
        """Nothing is glued together to look finished."""
        members = POOL[:3]
        t = OfflineTransport(fail={members[1]["id"]})
        line = recipes.supply_chain(self.STEPS, transport=t, models=POOL, members=members,
                                    live_catalogue=False)
        self.assertEqual(len(line.steps), 2, "the line kept running past a broken station")
        self.assertIsNone(line.result)
        self.assertEqual(line.broke_at.model, members[1]["id"])
        self.assertEqual(line.broke_at.step, "write it")

    def test_more_steps_than_families_repeats_in_order(self):
        t = OfflineTransport()
        line = recipes.supply_chain(["a", "b", "c", "d", "e"], transport=t, models=POOL,
                                    members=POOL[:2], live_catalogue=False)
        self.assertEqual([x.model for x in line.steps],
                         [POOL[0]["id"], POOL[1]["id"], POOL[0]["id"], POOL[1]["id"], POOL[0]["id"]])

    def test_the_seam_guards_the_line_too(self):
        with self.assertRaises(redact.Refused):
            recipes.supply_chain(["x"], material="key: sk-or-v1-" + "a" * 64,
                                 transport=OfflineTransport(), models=POOL, live_catalogue=False)


class SendAnywayMeansIt(unittest.TestCase):
    """The console's "I have looked at these. Send anyway." passed the server's check and was
    refused again by the engine's own seam. Box ticked, refusal twice. Driven through the real
    endpoint, because that is where it was broken."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        with open(os.path.join(self.folder, "app.py"), "w", encoding="utf-8") as f:
            f.write('KEY = "sk-or-v1-' + "a" * 64 + '"\n')
        self.srv = server._Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old
        importlib.reload(config)

    def post(self, path, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=60)
        c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        return json.loads(c.getresponse().read())

    def test_the_board_refuses_by_default_and_goes_when_told(self):
        base = {"offline": True, "mode": "board", "code_path": self.folder,
                "messages": [{"role": "user", "content": "audit this"}]}
        self.assertIn("refused", self.post("/api/chat", base))
        r = self.post("/api/chat", {**base, "send_anyway": True})
        self.assertNotIn("refused", r, "the override was ignored by the engine")
        self.assertIn("answers", r)

    def test_the_work_button_honours_it_too(self):
        base = {"offline": True, "path": self.folder, "task": "tidy up"}
        self.assertIn("refused", self.post("/api/work", base))
        r = self.post("/api/work", {**base, "send_anyway": True})
        self.assertNotIn("refused", r, "the override was ignored on the work path")
        self.assertIn("changes", r)

    def test_the_library_recipe_honours_it(self):
        with self.assertRaises(redact.Refused):
            recipes.audit(self.folder, transport=OfflineTransport(), models=POOL, live_catalogue=False)
        s = recipes.audit(self.folder, send_anyway=True, transport=OfflineTransport(),
                          models=POOL, live_catalogue=False)
        self.assertTrue(s.answers)


class BareBoardMeansShowMeTheConsole(unittest.TestCase):
    """Typing `board` with the console already up printed three lines about ports. The person
    typing it has usually just forgotten it is running; they want the tab, not the lecture."""

    def test_an_already_running_console_is_opened_not_described(self):
        srv = server._Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = server.serve(srv.server_address[1], open_browser=False)
        self.assertEqual(rc, 0)
        self.assertIn("already running", out.getvalue())

    def test_a_stranger_on_the_port_is_still_reported(self):
        import socket
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        self.addCleanup(other.close)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = server.serve(other.getsockname()[1], open_browser=False)
        self.assertEqual(rc, 1)
        self.assertIn("Something else", out.getvalue())


class ACutOffQuestionIsACancel(unittest.TestCase):
    """`board pick` and `board setup` ask questions. With stdin closed - a script, a pipe, a
    cron job - or on Ctrl-D, input() raises EOFError, and the person got a traceback for
    declining to answer."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old
        importlib.reload(config)

    def test_eof_and_ctrl_c_end_quietly(self):
        from unittest import mock

        from boardofdirectors import cli
        for cmd, exc in (("pick", EOFError), ("setup", KeyboardInterrupt), ("pick", KeyboardInterrupt)):
            with self.subTest(cmd=cmd, exc=exc.__name__):
                with mock.patch("builtins.input", side_effect=exc), \
                        mock.patch("getpass.getpass", side_effect=exc), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = cli.main(["--offline", cmd])
                self.assertEqual(rc, 130)
                self.assertIn("cancelled", out.getvalue())
                self.assertNotIn("Traceback", out.getvalue())


class DemoMode(unittest.TestCase):
    """`board --offline ui` accepted the flag and dropped it. The README promised the screenshots
    were reproducible without a key on the strength of that flag. Now it reaches the console."""

    def tearDown(self):
        server.DEMO = False

    def test_the_flag_switches_every_request_to_the_stub(self):
        server.DEMO = True
        t, live = server._transport(offline=False)
        self.assertIsInstance(t, OfflineTransport)
        self.assertFalse(live)
        self.assertTrue(server._state()["demo"])

    def test_the_cli_passes_it_through(self):
        from unittest import mock

        from boardofdirectors import cli
        with mock.patch.object(server, "serve", return_value=0) as srv:
            cli.main(["--offline", "ui", "--no-open", "--port", "1"])
        self.assertTrue(srv.call_args.kwargs.get("offline"), "the flag was dropped on the way")

    def test_the_page_does_not_ask_for_a_key_in_demo(self):
        with open(os.path.join(os.path.dirname(server.__file__), "web", "index.html"),
                  encoding="utf-8") as f:
            h = f.read()
        self.assertIn("!S.key_set && !S.demo", h)


class TheCeilingIsWhatTheMinuteCanServe(unittest.TestCase):
    """Twelve seats fire 25 requests inside a minute against a free-tier limit of 20. The
    rankings that lost used to vanish: not counted, not shown, not told to the chair."""

    def test_nine_is_the_most_anywhere(self):
        self.assertEqual(seats.MAX_SEATS, 9)
        self.assertEqual(openai_api.parse_model("board:12")["size"], 9)
        self.assertEqual(openai_api.parse_model("board:9")["size"], 9)
        with open(os.path.join(os.path.dirname(server.__file__), "web", "index.html"),
                  encoding="utf-8") as f:
            self.assertIn("Math.min(9,", f.read())

    def test_a_lost_ranking_is_counted_reported_and_told_to_the_chair(self):
        pool = [model(f"f{i}/m:free") for i in range(8)]
        loser = pool[1]["id"]
        chair_saw = []

        class OneRankerDown(OfflineTransport):
            def ask(self, m, messages):
                text = messages[-1]["content"]
                if "Rank them" in text and m["id"] == loser:
                    return Failure(m["id"], "rate limited", status=429)
                if "BLIND RANKINGS" in text:
                    chair_saw.append(text)
                return super().ask(m, messages)

        s = board.ask("ship it?", transport=OneRankerDown(), models=pool, members=pool[:4],
                      live_catalogue=False)
        self.assertEqual(len(s.ranking_failures), 1)
        self.assertEqual(s.ranking_failures[0].model, loser)
        self.assertEqual(len(s.rankings), 3)
        self.assertEqual(s.requests, 4 + 3 + 1 + 1, "a failed ranking was still a request")
        self.assertIn("RANKINGS: 3 of 4 received", s.report())
        self.assertIn("3 of 4 members returned a ranking", chair_saw[0])

    def test_identical_failures_are_said_once(self):
        pool = [model(f"f{i}/m:free") for i in range(8)]
        t = OfflineTransport(fail={m["id"] for m in pool[:4]})
        s = board.ask("ship it?", transport=t, models=pool, members=pool[:4], live_catalogue=False)
        rep = s.report()
        self.assertIn("all 4 failed the same way", rep)
        self.assertEqual(rep.count("rate limited (simulated)"), 1, "the same reason was listed per member")


class TheCountdownHonoursThe429(unittest.TestCase):
    """learn_from_429 stored X-RateLimit-Reset from the first week. Nothing read it: the header
    counted down to UTC midnight regardless, while the module docstring promised otherwise."""

    def test_a_stated_reset_two_hours_out_is_what_is_shown(self):
        for raw in (time.time() + 7200, (time.time() + 7200) * 1000, str(int(time.time() + 7200))):
            with self.subTest(form=type(raw).__name__ + ("-ms" if isinstance(raw, float) and raw > 1e12 else "")):
                self.assertIn(usage._resets_in({"reset": raw}), ("1h 59m", "2h 0m"))

    def test_noise_falls_back_to_midnight(self):
        midnight = usage._resets_in(None)
        for raw in (None, "soon", -5, time.time() - 60, time.time() + 40 * 86400):
            with self.subTest(raw=raw):
                self.assertEqual(usage._resets_in({"reset": raw}), midnight)


class TheSnapshotLivesWithTheUser(unittest.TestCase):
    """`board refresh` wrote into the installed package: silently on a pipx install, a traceback on
    a read-only one. The shipped copy is read-only by nature; the machine's own copy goes in HOME
    and any successful live fetch refreshes it."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        importlib.reload(catalogue)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old
        importlib.reload(config)
        importlib.reload(catalogue)

    def test_the_user_copy_is_in_home_and_the_shipped_one_is_never_written(self):
        self.assertTrue(catalogue.USER_SNAPSHOT.startswith(self.home))
        before = os.path.getmtime(catalogue.SNAPSHOT)
        catalogue.remember({"captured": "now", "models": [model("z/one:free")], "origin": "live"})
        self.assertEqual(os.path.getmtime(catalogue.SNAPSHOT), before, "the shipped file was touched")
        self.assertTrue(os.path.exists(catalogue.USER_SNAPSHOT))

    def test_the_user_copy_wins_and_a_corrupt_one_falls_back(self):
        shipped = catalogue.snapshot()["models"]
        catalogue.remember({"captured": "now", "models": [model("z/one:free")]})
        self.assertEqual([m["id"] for m in catalogue.snapshot()["models"]], ["z/one:free"])
        with open(catalogue.USER_SNAPSHOT, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(len(catalogue.snapshot()["models"]), len(shipped), "a corrupt copy was not an older snapshot")

    def test_refresh_lifts_the_unusable_gate(self):
        from unittest import mock

        from boardofdirectors import cli
        config.mark_unusable("a/b:free", "refused yesterday")
        self.assertIn("a/b:free", config.unusable())
        with mock.patch.object(catalogue, "fetch", return_value={"captured": "x", "models": [model("z/one:free")]}), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["refresh"])
        self.assertNotIn("a/b:free", config.unusable())
        self.assertTrue(os.path.exists(catalogue.USER_SNAPSHOT))


class TheWelcomeTextFollowsTheSeats(unittest.TestCase):
    def test_the_numbers_are_not_hardcoded(self):
        with open(os.path.join(os.path.dirname(server.__file__), "web", "index.html"), encoding="utf-8") as f:
            h = f.read()
        self.assertNotIn("five of them answer", h)
        self.assertIn('id="emptyN"', h)
        self.assertIn('id="emptyCost"', h)
        self.assertIn("2 * nSeats + 1", h)


class FreeMeansEveryPriceIsZero(unittest.TestCase):
    """OpenRouter's pricing block has a dozen fields. Checking two of them would seat a model
    that is free on tokens and metered on web search or images as free, and bill the owner."""

    def test_two_zero_prices_with_a_third_fee_is_not_free(self):
        for fee in ("web_search", "image", "audio", "internal_reasoning", "request"):
            with self.subTest(fee):
                m = {"pricing": {"prompt": "0", "completion": "0", fee: "0.002"}}
                self.assertFalse(catalogue.is_free(m), f"{fee} fee ignored")

    def test_all_zero_is_free_and_missing_extras_are_fine(self):
        self.assertTrue(catalogue.is_free({"pricing": {"prompt": "0", "completion": "0"}}))
        self.assertTrue(catalogue.is_free({"pricing": {"prompt": "0", "completion": "0",
                                                       "web_search": "0", "image": None,
                                                       "overrides": {"x": 1}}}))

    def test_missing_token_prices_are_not_free(self):
        self.assertFalse(catalogue.is_free({"pricing": {"prompt": "0"}}))
        self.assertFalse(catalogue.is_free({"pricing": {}}))
        self.assertFalse(catalogue.is_free({}))


class TheWallSeesTheWholeMessage(unittest.TestCase):
    """The estimate ran before the code message was built, at chat size. A 100k-token folder
    audit on a $15/M member passed a $0.25 cap as "$0.15". Driven through _board, because
    the order of two lines in it was the bug."""

    PAID = model("costly/opus", free=False, price_in=15.0, price_out=75.0, ctx=400_000)
    FREE = tuple(model(f"f{i}/m:free", ctx=400_000) for i in range(4))

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        self.old_cache = dict(server._CACHE)
        server._CACHE["models"] = [self.PAID, *self.FREE]
        server._CACHE["at"] = time.time() + 10_000
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        # sixty ordinary-sized files, because the scanner skips any single file over its limit -
        # the first version of this fixture was one 480 KB file, and it never reached the board
        for i in range(60):
            with open(os.path.join(self.folder, f"mod{i:02d}.py"), "w", encoding="utf-8") as f:
                f.write("x = 1  # a line of code that is about forty characters long\n" * 130)   # ~2k tokens each
        config.set_model_tier("both")
        config.set_spend_cap(0.25)

    def tearDown(self):
        server._CACHE.clear()
        server._CACHE.update(self.old_cache)
        if self.old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old
        importlib.reload(config)

    def run_board(self, code_path=None):
        payload = {"offline": True, "allow_paid": True, "peer_review": False,
                   "board": [self.PAID["id"], *(m["id"] for m in self.FREE[:2])],
                   "messages": [{"role": "user", "content": "audit this"}]}
        if code_path:
            payload["code_path"] = code_path
            payload["send_anyway"] = True
        return server._board(payload)

    def test_a_big_folder_on_a_paid_member_hits_the_cap(self):
        r = self.run_board(self.folder)
        self.assertIn("error", r, "a $1.80 audit was waved through a $0.25 cap")
        self.assertIn("cap", r["error"])

    def test_a_chat_sized_question_on_the_same_board_is_fine(self):
        r = self.run_board()
        self.assertNotIn("error", r)
        self.assertLess(r["estimated_usd"], 0.25)


class SettingsThatGateMoneyAreValidated(unittest.TestCase):
    """"1e400" parsed to infinity and was stored as the cap - the wall removed by a typo. And a
    bad tier or a non-numeric cap came back as a 500, which reads as a crash, not a mistake."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        self.srv = server._Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old
        importlib.reload(config)

    def post(self, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=30)
        c.request("POST", "/api/paid", json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def test_infinity_is_not_a_cap(self):
        for raw in ("1e400", "inf", "Infinity", float("inf"), "nan"):
            with self.subTest(raw=raw):
                status, body = self.post({"cap": raw})
                self.assertEqual(status, 400, f"{raw!r} was accepted")
                self.assertIn("error", body)
                self.assertEqual(config.spend_cap(), 0.25, "the cap moved on bad input")

    def test_bad_input_is_a_400_not_a_500(self):
        for body in ({"tier": "xyz"}, {"tier": 5}, {"cap": "abc"}, {"tier": "PAID"}):
            with self.subTest(body=body):
                status, out = self.post(body)
                self.assertEqual(status, 400)
                self.assertIn("not saved", out["error"])
        self.assertEqual(config.model_tier(), "free")

    def test_good_input_still_works(self):
        status, _ = self.post({"tier": "both", "cap": 1.5})
        self.assertEqual(status, 200)
        self.assertEqual((config.model_tier(), config.spend_cap()), ("both", 1.5))
        status, _ = self.post({"cap": -3})
        self.assertEqual((status, config.spend_cap()), (200, 0.0), "negative means locked, not refused")


class TheWallHasAnOutputSide(unittest.TestCase):
    """The estimate assumed 700 output tokens a call; the transport sent the model's own limit,
    32k on the big ones. Three premium members were checked at $0.43 and permitted to bill
    $14.75. The ceilings below are what the API is now told, and they are enforced."""

    PREM = model("p/prem", free=False, price_in=15.0, price_out=75.0, out=32768)
    CHEAP = model("c/cheap:free")

    def test_ceilings_split_the_headroom_and_the_worst_case_is_the_cap(self):
        members = [self.PREM, model("q/prem2", free=False, price_in=15.0, price_out=75.0, out=32768),
                   model("r/prem3", free=False, price_in=15.0, price_out=75.0, out=32768)]
        ceilings, worst = cost.fit_under_cap(members, self.CHEAP, 0.25, peer_review=True, prompt_tokens=1200)
        self.assertEqual(set(ceilings), {m["id"] for m in members})
        self.assertTrue(all(cost.OUTPUT_FLOOR <= c < 32768 for c in ceilings.values()), ceilings)
        self.assertLessEqual(worst, 0.25 + 1e-6)
        self.assertGreater(worst, 0.2, "the headroom was not used")

    def test_a_cap_too_small_for_real_answers_is_refused_not_stubbed(self):
        members = [self.PREM] * 3
        ceilings, worst = cost.fit_under_cap(members, self.CHEAP, 0.15, peer_review=True, prompt_tokens=1200)
        self.assertTrue(all(c == cost.OUTPUT_FLOOR for c in ceilings.values()))
        self.assertGreater(worst, 0.15, "a session that cannot fit must come back over the cap")

    def test_free_members_are_untouched(self):
        ceilings, worst = cost.fit_under_cap([self.CHEAP, self.CHEAP], self.CHEAP, 0.25)
        self.assertEqual(ceilings, {})
        self.assertEqual(worst, 0.0)

    def test_the_capped_transport_lowers_but_never_raises(self):
        seen = {}

        class Spy(OfflineTransport):
            def ask(self, m, messages, **kw):
                seen[m["id"]] = kw.get("max_tokens")
                return super().ask(m, messages)

        t = Capped(Spy(), {"p/prem": 300})
        t.ask(self.PREM, [{"role": "user", "content": "x"}])
        self.assertEqual(seen["p/prem"], 300)
        t.ask(self.PREM, [{"role": "user", "content": "x"}], max_tokens=100)
        self.assertEqual(seen["p/prem"], 100, "a caller's smaller limit was overridden")
        t.ask(self.PREM, [{"role": "user", "content": "x"}], max_tokens=5000)
        self.assertEqual(seen["p/prem"], 300, "the ceiling did not lower a bigger request")
        t.ask(self.CHEAP, [{"role": "user", "content": "x"}])
        self.assertIsNone(seen["c/cheap:free"], "a free model was capped")


class EverySpendingPathIsFenced(unittest.TestCase):
    """Driven through the server, because the fence is only real where the money leaves."""

    PAID = model("costly/opus", free=False, price_in=15.0, price_out=75.0, out=32768, ctx=400_000)
    FREE = tuple(model(f"f{i}/m:free", ctx=400_000) for i in range(4))

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.old = os.environ.get("BOARD_HOME")
        os.environ["BOARD_HOME"] = self.home
        importlib.reload(config)
        self.old_cache = dict(server._CACHE)
        server._CACHE["models"] = [self.PAID, *self.FREE]
        server._CACHE["at"] = time.time() + 10_000
        self.seen = {}
        outer = self

        class Spy(OfflineTransport):
            def ask(self, m, messages, **kw):
                outer.seen.setdefault(m["id"], []).append(kw.get("max_tokens"))
                return super().ask(m, messages)

        self.old_transport = server._transport
        server._transport = lambda offline: (Spy(), True)
        config.set_model_tier("both")
        config.set_spend_cap(0.25)

    def tearDown(self):
        server._CACHE.clear()
        server._CACHE.update(self.old_cache)
        server._transport = self.old_transport
        if self.old is None:
            os.environ.pop("BOARD_HOME", None)
        else:
            os.environ["BOARD_HOME"] = self.old
        importlib.reload(config)

    def test_the_board_tells_the_api_a_ceiling_for_the_paid_member(self):
        r = server._board({"allow_paid": True, "board": [self.PAID["id"], *(m["id"] for m in self.FREE[:2])],
                           "messages": [{"role": "user", "content": "ship it?"}]})
        self.assertNotIn("error", r, r.get("error"))
        caps = [c for c in self.seen[self.PAID["id"]] if c is not None]
        self.assertTrue(caps and all(cost.OUTPUT_FLOOR <= c < 32768 for c in caps), self.seen)
        for m in self.FREE[:2]:
            self.assertTrue(all(c is None for c in self.seen.get(m["id"], [])), "a free member was capped")
        self.assertEqual(r["output_cap"], min(caps))

    def test_a_single_paid_turn_is_fenced_too(self):
        r = server._single({"allow_paid": True, "model": self.PAID["id"],
                            "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("error", r)
        c = self.seen[self.PAID["id"]][0]
        self.assertTrue(c is not None and cost.OUTPUT_FLOOR <= c < 32768, c)

    def test_a_single_paid_turn_over_the_cap_is_refused_before_the_wire(self):
        config.set_spend_cap(0.01)
        r = server._single({"allow_paid": True, "model": self.PAID["id"],
                            "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("error", r)
        self.assertEqual(self.seen, {}, "a refused turn still went to the wire")



class AMaskedKeyIsNotAKey(unittest.TestCase):
    """The three things a scanner flags as "clear-text logging of sensitive information" are
    all this function's output, so what it shows has to be worth nothing to a stranger."""

    def test_a_real_key_shows_its_public_prefix_and_nothing_usable(self):
        key = "sk-or-v1-" + "0123456789abcdef" * 4
        shown = config.mask(key)
        self.assertEqual(shown, "sk-or-v1..." + key[-4:])
        self.assertLess(len(shown.replace("...", "")), 16)
        self.assertNotIn(key[9:-4], shown)

    def test_a_short_secret_shows_nothing_at_all(self):
        """Four characters of a long key is a prefix everybody has. Four characters of a short
        one is a quarter of the secret."""
        for key in ("hunter2", "abcd", "0123456789abcdef"):
            with self.subTest(key=key):
                self.assertEqual(config.mask(key), "****")

    def test_no_key_is_a_different_answer_from_a_hidden_one(self):
        self.assertEqual(config.mask(None), "none")
        self.assertEqual(config.mask(""), "none")


class NothingShippedSaysWhenOrWhoseItWas(unittest.TestCase):
    """The licence copyrighted a project that does not exist, and three documents carried the
    day somebody read a web page.

    Prose drifts, and a rule kept in somebody's head drifts with it, so the rule is a test.

    What counts as a violation is prose, not data. A comment saying when a rate-limit page was
    read goes stale and dates the work; a fixture using a calendar day to test a day boundary
    is neither. So Python files are read through `tokenize` and `ast` - comments and docstrings
    only, never a string literal - and everything else is read whole.

    The patterns are assembled from fragments on purpose: a guard written with an example date
    in it is a guard that fails on itself.
    """

    PKG = os.path.dirname(os.path.abspath(catalogue.__file__))
    ROOT = os.path.dirname(PKG)

    _Y = r"(?:19|20)\d{2}"
    ISO = re.compile(_Y + r"-\d{2}-\d{2}")
    YEAR = re.compile(r"\b" + _Y + r"\b")
    MONTH = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
                       r"\d{1,2},?\s+" + _Y)

    def _documents(self):
        for name in sorted(os.listdir(self.ROOT)):
            if name.endswith(".md") or name == "LICENSE":
                yield os.path.join(self.ROOT, name)

    def _sources(self):
        for base in (self.PKG, os.path.join(self.ROOT, "tests"),
                     os.path.join(self.ROOT, ".github", "workflows")):
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in sorted(filenames):
                    if name.endswith((".py", ".yml", ".yaml")):
                        yield os.path.join(dirpath, name)

    def _prose(self, path):
        """(line, text) for the parts of a file a person reads as English."""
        import ast as _ast
        import tokenize as _tok
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if not path.endswith(".py"):
            return list(enumerate(src.splitlines(), 1))

        out = []
        for tok in _tok.generate_tokens(io.StringIO(src).readline):
            if tok.type == _tok.COMMENT:
                out.append((tok.start[0], tok.string))
        for node in _ast.walk(_ast.parse(src)):
            if not isinstance(node, (_ast.Module, _ast.ClassDef,
                                     _ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            doc = _ast.get_docstring(node, clean=False)
            if doc:
                first = getattr(node, "lineno", 1)
                out += [(first + i, ln) for i, ln in enumerate(doc.splitlines())]
        return out

    def _label(self, path):
        """`relpath` does not return a long answer across Windows drives, it raises. The
        control below writes to a temp file, and on a CI runner the checkout is on D: while
        the temp directory is on C:."""
        try:
            return os.path.relpath(path, self.ROOT)
        except ValueError:
            return path

    def _hits(self, path, patterns):
        rel = self._label(path)
        return [f"{rel}:{n}: {text.strip()[:90]}"
                for n, text in self._prose(path)
                if any(pat.search(text) for pat in patterns)]

    def test_no_document_carries_a_date_or_a_year(self):
        """A changelog heading is a version. A licence is a licence. Neither is a diary."""
        found = [h for p in self._documents() for h in self._hits(p, (self.YEAR, self.MONTH))]
        self.assertEqual(found, [], "a document dates the work:\n" + "\n".join(found))

    def test_no_comment_or_docstring_carries_a_date(self):
        """The one that got through: a module docstring recording the day its numbers were
        read off a web page. Bare four-digit numbers are left alone - a token cap is not a
        year - so this looks only for something written in the shape of a date."""
        found = [h for p in self._sources() for h in self._hits(p, (self.ISO, self.MONTH))]
        self.assertEqual(found, [], "a comment or docstring dates the work:\n" + "\n".join(found))

    def test_the_guard_can_tell_prose_from_data(self):
        """Without this, the test above passes by reading nothing at all."""
        f = os.path.join(tempfile.mkdtemp(), "sample.py")
        with open(f, "w") as fh:
            fh.write('"""Read ' + "2019-04-01" + '."""\n'
                     "DAY = " + repr("2019-04-02") + "\n"
                     "# and " + "2019-04-03" + "\n")
        hits = self._hits(f, (self.ISO,))
        self.assertEqual(len(hits), 2, f"expected the docstring and the comment only: {hits}")
        self.assertNotIn("DAY", " ".join(hits))

    def test_a_path_on_another_drive_still_gets_a_label(self):
        """Not reproducible on a mac, where `relpath` always answers. CI found it: three
        Windows jobs raised `path is on mount 'C:', start on mount 'D:'` from the control
        above, because the checkout is on one drive and the temp directory on another."""
        from unittest import mock
        outside = os.path.join(os.sep, "elsewhere", "x.py")
        with mock.patch("os.path.relpath", side_effect=ValueError("different drives")):
            self.assertEqual(self._label(outside), outside)

    def test_the_licence_names_the_handle_and_nobody_else(self):
        """It read `freeboard contributors` - a name from before this project was this
        project - through every earlier scrub, because nothing ever read the licence."""
        with open(os.path.join(self.ROOT, "LICENSE"), encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip().startswith("Copyright")]
        self.assertEqual(lines, ["Copyright (c) jedisolana"])


class TheReadmeCountsWhatIsActuallyHere(unittest.TestCase):
    """The README advertised 159 tests. There were 249 of them.

    A number written into prose is a claim, and this one had been wrong for ninety tests
    because nothing was ever going to notice. Counting is done from the classes already
    imported here rather than by re-running discovery, which would import this module a second
    time and re-apply its module-level network guard.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(catalogue.__file__)))

    def test_this_is_still_the_only_test_file(self):
        """Counting one module is only honest while there is one module."""
        here = os.path.join(self.ROOT, "tests")
        found = sorted(f for f in os.listdir(here)
                       if f.startswith("test") and f.endswith(".py"))
        self.assertEqual(found, ["test_board.py"],
                         "another test file exists, so the count below no longer sees everything")

    def test_the_readme_states_the_real_number(self):
        loader = unittest.TestLoader()
        actual = sum(len(loader.getTestCaseNames(obj))
                     for obj in vars(sys.modules[__name__]).values()
                     if isinstance(obj, type) and issubclass(obj, unittest.TestCase))
        with open(os.path.join(self.ROOT, "README.md"), encoding="utf-8") as f:
            claimed = re.search(r"\*\*(\d+) tests,", f.read())
        self.assertIsNotNone(claimed, "the README no longer states a test count")
        self.assertEqual(int(claimed.group(1)), actual,
                         f"the README says {claimed.group(1)} tests; there are {actual}")

    def test_the_readme_states_the_real_model_count(self):
        """"All 431 models become seatable" is a fact about the bundled catalogue, and the
        catalogue is refreshed - so the sentence is true until somebody runs `board refresh`
        and then it is quietly not. The test count beside it has been checked from the start;
        this number had only a comment mentioning it."""
        with open(os.path.join(self.ROOT, "boardofdirectors", "data", "free-models.json"),
                  encoding="utf-8") as f:
            actual = len(json.load(f)["models"])
        with open(os.path.join(self.ROOT, "README.md"), encoding="utf-8") as f:
            claimed = re.search(r"all (\d+) models", f.read())
        self.assertIsNotNone(claimed, "the README no longer states a model count")
        self.assertEqual(int(claimed.group(1)), actual,
                         f"the README says {claimed.group(1)} models; the snapshot has {actual}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
