"""The session: independent answers, a blind ranking, then a chair who synthesises.

Three stages, and the order is the point.

  1. ANSWER, independently. Every member gets the same question and none of them sees
     another's reply. The moment one member reads another, you no longer have several
     opinions -- you have one opinion and some agreement, and agreement is not evidence.

  2. RANK, blind. Members are shown the other answers with the names stripped: "Member A",
     "Member B". Models have favourites -- their own family included -- and a name on an
     answer is enough to move a ranking. The mapping is kept and revealed afterwards, so you
     can audit who was ranked where, but nobody ranks a label they recognise.

  3. CHAIR. One model that did NOT vote reads every answer plus the rankings and writes the
     decision. A member who also counts the votes is not a chair.

And the thing the board refuses to do: treat a member who failed as a member who agreed.
Failures are counted, named, and if too many members are missing the session returns
NO QUORUM rather than a confident-looking answer from whoever got through.
"""
from __future__ import annotations

import contextlib
import re
import string
from dataclasses import dataclass, field

from . import catalogue, redact, seats
from .transport import Answer, Failure, OfflineTransport, Transport

# TWO KINDS OF QUESTION, AND THEY NEED OPPOSITE PROMPTS.
#
# "Should we rewrite the parser?" wants a POSITION. "Write me a parser" wants the PARSER.
# The board shipped with only the first prompt, so asking it to build something got four
# models solemnly taking a position on whether building it was a good idea - they answered
# exactly what they were asked, and what they were asked was wrong.
#
# DECIDE is a jury. MAKE is a competition: everyone attempts the same task, the attempts are
# ranked blind, and the chair delivers the best one rather than summarising the field.

ANSWER_PROMPT = (
    "You are one member of an independent board. Answer the question on your own judgement.\n"
    "Be specific and brief: your position, then the single strongest reason for it, then the "
    "one thing that would change your mind.\n\n"
    "IF THE QUESTION IS ACTUALLY A REQUEST TO DO SOMETHING rather than a decision to make, "
    "do it, and let your position be your attempt. Do not vote on whether the request is "
    "permissible or possible - nobody asked you that.\n\n"
    "End your answer with a single line, exactly:\n"
    "VOTE: FOR      (you support the proposal)\n"
    "VOTE: AGAINST  (you oppose it)\n"
    "VOTE: DEPENDS  (you cannot support it as put, without a condition being met)\n\n"
    "QUESTION:\n{q}"
)

# A board that cannot show its own vote is a discussion. The tally is READ from what each
# member declared, never inferred from their prose: a member who did not state a vote is
# recorded UNCLEAR and shown as such. Guessing a position from wording would put words in a
# member's mouth and then count them - the same failure as counting a silent member as
# agreement, one step further along.
_VOTE = re.compile(r"^\s*(?:\*\*)?VOTE(?:\*\*)?\s*[:\-]\s*(?:\*\*)?\s*"
                   r"(FOR|AGAINST|DEPENDS)\b", re.I | re.M)
VOTES = ("FOR", "AGAINST", "DEPENDS", "UNCLEAR")


def read_vote(text: str) -> str:
    """The vote a member DECLARED, or UNCLEAR. Never inferred."""
    m = _VOTE.search(text or "")
    return m.group(1).upper() if m else "UNCLEAR"


def strip_vote(text: str) -> str:
    """The reasoning without the marker line, which the UI shows as a badge instead."""
    return _VOTE.sub("", text or "").rstrip()


def tally(answers: list) -> dict:
    """The count, plus whether it is even meaningful."""
    counts = dict.fromkeys(VOTES, 0)
    for a in answers:
        counts[read_vote(getattr(a, "text", "") or "")] += 1
    decided = counts["FOR"] + counts["AGAINST"]
    return {**counts, "decided": decided,
            "carried": counts["FOR"] > counts["AGAINST"] if decided else None,
            "split": counts["FOR"] == counts["AGAINST"] and decided > 0}

MAKE_PROMPT = (
    "You are one of several people given the same task, working independently. Do the task.\n"
    "Produce the actual thing that was asked for - the code, the text, the answer - not a plan "
    "for it, not advice on how to approach it, and not a list of tools someone could use.\n"
    "If the task is too large to finish, do the most useful complete PART of it and say in one "
    "line what you left out. A finished piece beats an outline of the whole.\n\nTASK:\n{q}"
)

RANK_PROMPT = (
    "You are one member of an independent board. Below are the other members' answers, with "
    "their identities hidden.\nRank them best to worst on reasoning quality alone -- not on "
    "whether they agree with you.\nReply with the labels in order, best first, then one line "
    "saying why the top one won.\n\nQUESTION:\n{q}\n\nANSWERS:\n{answers}"
)

MAKE_RANK_PROMPT = (
    "Several people attempted the same task independently. Their attempts are below with "
    "identities hidden.\nRank them best to worst on how well each one ACTUALLY DOES THE TASK "
    "- correctness first, then completeness. An attempt that describes what it would do ranks "
    "below one that does it, however well written.\nReply with the labels in order, best "
    "first, then one line on what the winner got right.\n\nTASK:\n{q}\n\nATTEMPTS:\n{answers}"
)

MAKE_CHAIR_PROMPT = (
    "You are the chair. You did not attempt this task. Below are independent attempts at it "
    "and the members' blind rankings of each other.\n"
    "DELIVER THE FINISHED WORK. Take the strongest attempt and improve it with anything the "
    "others got right that it missed. Output the thing itself - the code, the text, the "
    "answer - as one coherent piece.\n"
    "Do not review the attempts. Do not describe what you merged. Do not say which member "
    "won. The reader wants the work, not the minutes of the meeting.\n"
    "If every attempt refused or produced only advice, say that plainly in one line rather "
    "than inventing something none of them wrote.\n\n"
    "TASK:\n{q}\n\nATTEMPTS:\n{answers}\n\nBLIND RANKINGS:\n{rankings}"
)

CHAIR_PROMPT = (
    "You are the chair of a board. You did not vote.\n"
    "The members' declared votes have already been counted for you: {tally}. Use that count; "
    "do not recount it from their prose, and do not attribute a vote to a member who did not "
    "declare one.\n Below are the members' independent "
    "answers and their blind rankings of each other.\nWrite the board's decision: the "
    "position, the vote as you read it, the strongest dissent and why it did not carry, and "
    "what would change the decision.\nDo not invent agreement that is not there. If the board "
    "is genuinely split, say it is split.\n\nQUESTION:\n{q}\n\nANSWERS:\n{answers}\n\n"
    "BLIND RANKINGS:\n{rankings}"
)


@dataclass
class Session:
    question: str
    members: list[dict]
    chair_model: dict
    answers: list[Answer] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    rankings: list[Answer] = field(default_factory=list)
    chair_failures: list[dict] = field(default_factory=list)
    tally: dict = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)   # label -> model id
    kind: str = "decide"
    decision: str | None = None
    no_quorum: str | None = None

    @property
    def voted(self) -> int:
        return len(self.answers)

    @property
    def requests(self) -> int:
        return len(self.answers) + len(self.failures) + len(self.rankings) + (1 if self.decision else 0)

    def report(self) -> str:
        out = [f"QUESTION: {self.question}", ""]
        out.append(f"BOARD: {len(self.members)} member(s) from "
                   f"{len({m['family'] for m in self.members})} families, "
                   f"chaired by {self.chair_model['id']}")
        out.append("")
        for a in self.answers:
            label = next((k for k, v in self.labels.items() if v == a.model), "?")
            out.append(f"  [{label}] {a.model}   VOTE: {read_vote(a.text)}")
            for line in strip_vote(a.text).splitlines():
                out.append(f"        {line}")
            out.append("")
        if self.tally and self.kind != "make":
            t = self.tally
            out.append(f"  VOTE: {t['FOR']} for · {t['AGAINST']} against · "
                       f"{t['DEPENDS']} conditional"
                       + (f" · {t['UNCLEAR']} undeclared" if t["UNCLEAR"] else ""))
            out.append("")
        if self.failures:
            out.append(f"  DID NOT VOTE ({len(self.failures)}) -- not counted as agreement:")
            for f in self.failures:
                out.append(f"        {f.model}: {f.reason}")
            out.append("")
        if self.no_quorum:
            out.append(f"NO QUORUM: {self.no_quorum}")
        else:
            out.append("RESULT (chair):" if self.kind == "make" else "DECISION (chair):")
            for line in (self.decision or "").splitlines():
                out.append(f"    {line}")
        out.append("")
        out.append(f"cost: {self.requests} request(s)")
        return "\n".join(out)


def _ask_all(transport, models: list[dict], messages: list[dict], deadline: float):
    """Ask every model at once; yield (model, result) as each lands, slowest last.

    A model that misses the deadline yields a Failure like any other refusal, because from
    the board's point of view it is one: it did not answer. Its thread is left to finish and
    be discarded rather than being killed, since there is no safe way to interrupt a socket
    read mid-flight and a leaked answer is cheaper than a corrupted one.
    """
    import concurrent.futures as cf
    out = []
    # NOT a `with` block. Its __exit__ calls shutdown(wait=True), which blocks on exactly the
    # slow thread the deadline exists to walk away from - the timeout would report correctly
    # and the board would sit there anyway. Measured: a 1s deadline against a 3s member still
    # took 3s until this was unwound.
    pool = cf.ThreadPoolExecutor(max_workers=max(1, len(models)))
    try:
        # `messages` is one conversation for everyone, or a function of the member when
        # each seat must see something different -- a ranker must not see its own answer.
        msgs_for = messages if callable(messages) else (lambda _m: messages)
        futures = {pool.submit(transport.ask, m, msgs_for(m)): m for m in models}
        try:
            for fut in cf.as_completed(futures, timeout=deadline):
                out.append((futures[fut], fut.result()))
        except cf.TimeoutError:
            pass
        done = {id(m) for m, _ in out}
        for fut, m in futures.items():
            if id(m) not in done:
                fut.cancel()
                out.append((m, Failure(m["id"],
                                       f"no answer within {deadline:.0f}s — the board did "
                                       f"not wait")))
    finally:
        # Threads still in a socket read cannot be interrupted safely; they finish and their
        # answers are discarded. A leaked answer is cheaper than a corrupted one, and the
        # process is a local server that will outlive them either way.
        pool.shutdown(wait=False)
    order = {m["id"]: i for i, m in enumerate(models)}
    out.sort(key=lambda pair: order.get(pair[0]["id"], 0))
    return out


def _blind(answers: list[Answer]) -> tuple[str, dict[str, str]]:
    """Label the answers A, B, C... and keep the mapping for the audit trail."""
    labels, blocks = {}, []
    for i, a in enumerate(answers):
        label = f"Member {string.ascii_uppercase[i % 26]}"
        labels[label] = a.model
        blocks.append(f"--- {label} ---\n{a.text}")
    return "\n\n".join(blocks), labels


def ask(question: str, *, transport: Transport | None = None, models: list[dict] | None = None,
        size: int = seats.DEFAULT_SEATS, minimum: int = 3, peer_review: bool = True,
        live_catalogue: bool = True, kind: str = "decide", on_event=None,
        members: list[dict] | None = None, allow_paid: bool = False,
        tier: str | None = None, deadline: float = 90.0) -> Session:
    """Run one board session. Raises `redact.Refused` before anything leaves the machine."""
    return ask_in_context(question, prior=None, transport=transport, models=models, size=size,
                          minimum=minimum, peer_review=peer_review,
                          live_catalogue=live_catalogue, kind=kind, on_event=on_event,
                          members=members, allow_paid=allow_paid, tier=tier,
                          deadline=deadline)


def ask_in_context(question: str, *, prior: list[dict] | None = None,
                   transport: Transport | None = None, models: list[dict] | None = None,
                   size: int = seats.DEFAULT_SEATS, minimum: int = 3, peer_review: bool = True,
                   live_catalogue: bool = True, kind: str = "decide",
                   members: list[dict] | None = None, on_event=None,
                   allow_paid: bool = False, tier: str | None = None,
                   deadline: float = 90.0) -> Session:
    """A board session that picks up an existing conversation.

    This is what makes the mode switch worth having. You talk to ONE model for a while at one
    request a turn; when a question deserves more, the same thread convenes the board, and
    every member reads the conversation so far before answering. The chair's verdict is what
    goes back into the thread, so switching back to a single model continues from the board's
    decision rather than from a hole in the history.

    Only the chair's verdict enters the history, never the five raw answers. Otherwise every
    board turn would multiply what each later turn has to re-read, and a long conversation
    would price itself out of the free tier.
    """
    redact.check(question)
    prior = prior or []

    if models is None:
        models = catalogue.load(live=live_catalogue)["models"]
    transport = transport or OfflineTransport()

    # A board CHOSEN by the caller is used exactly as chosen. This used to re-seat
    # unconditionally, so a hand-picked board was quietly replaced by the automatic pick
    # whenever the two differed - and the caller was handed back the list it had ASKED for,
    # which is why it looked like it had been honoured.
    members = members or seats.seat(models, size=size, allow_paid=allow_paid, tier=tier)
    seats.quorum(members, minimum=minimum)
    chair_model = seats.chair(models, members, allow_paid=allow_paid, tier=tier)
    s = Session(question=question, members=members, chair_model=chair_model, kind=kind)

    # A board session takes a minute. Reporting nothing until it is over turns deliberation
    # into a spinner, and hides the one thing worth watching: members disagreeing one at a
    # time. `on_event` is fired as each thing actually happens; it is optional and any
    # exception in it is swallowed, because a display must never be able to fail a session.
    def emit(**ev):
        if on_event:
            with contextlib.suppress(Exception):
                on_event(ev)

    emit(type="seated", members=[m["id"] for m in members], chair=chair_model["id"], kind=kind)

    # 1. independent answers -- each member reads the thread so far, then answers alone
    #
    # ASKED IN PARALLEL, because they are independent. That independence is the entire premise
    # of the thing, and asking them in a queue threw away the one property that makes the
    # parallelism free. It also made a slow member block every member behind it: one call is
    # up to four attempts at a two-minute timeout, so a single stuck model could hold the
    # whole board for eight minutes with nothing on screen. That is what "the board never
    # works" looked like from the outside - not a crash, a queue.
    #
    # A DEADLINE, because a member who has not answered in time is a member who did not
    # answer, and the board already knows exactly what to do with one of those.
    make = (kind == "make")
    prompt = (MAKE_PROMPT if make else ANSWER_PROMPT).format(q=question)
    for m in members:
        emit(type="asking", model=m["id"])
    for _m, r in _ask_all(transport, members,
                          [*prior, {"role": "user", "content": prompt}], deadline):
        (s.answers if r.ok else s.failures).append(r)
        if r.ok:
            emit(type="answer", model=r.model, vote=read_vote(r.text), text=strip_vote(r.text))
        else:
            emit(type="failure", model=r.model, reason=r.reason)

    # A board is the members who actually spoke. Silence is not consent.
    emit(type="tally", tally=tally(s.answers))
    if len(s.answers) < minimum:
        s.no_quorum = (f"only {len(s.answers)} of {len(members)} member(s) answered "
                       f"({len(s.failures)} failed); {minimum} needed. "
                       f"A missing member is not a vote either way.")
        emit(type="no_quorum", reason=s.no_quorum)
        return s

    blind_text, s.labels = _blind(s.answers)
    emit(type="labels", labels=s.labels)

    # 2. blind peer ranking
    if peer_review and len(s.answers) > 1:
        rankers = [m for m in members if any(a.model == m["id"] for a in s.answers)]
        by_model = {v: k for k, v in s.labels.items()}

        def rank_messages(m):
            # Everyone's answer but the ranker's own. The prompt has always said "the other
            # members' answers"; the code used to send all of them, so each member quietly
            # judged a line-up with itself in it -- and models prefer their own text even
            # blinded, which is the exact bias a jury of different companies exists to kill.
            # The labels stay global, so the chair can still line the rankings up.
            others = "\n\n".join(f"--- {by_model[a.model]} ---\n{a.text}"
                                  for a in s.answers if a.model != m["id"])
            rp = (MAKE_RANK_PROMPT if make else RANK_PROMPT).format(q=question, answers=others)
            return [*prior, {"role": "user", "content": rp}]

        for m in rankers:
            emit(type="ranking", model=m["id"])
        for m, r in _ask_all(transport, rankers, rank_messages, deadline):
            if r.ok:
                s.rankings.append(r)
                emit(type="ranked", model=m["id"])

    # 3. the chair
    ranked = "\n\n".join(f"--- ranking by a member ---\n{r.text}" for r in s.rankings) or "(none)"
    t = tally(s.answers)
    s.tally = t
    tally_line = (f"{t['FOR']} for, {t['AGAINST']} against, {t['DEPENDS']} conditional"
                  + (f", {t['UNCLEAR']} did not declare a vote" if t["UNCLEAR"] else ""))
    cp = (MAKE_CHAIR_PROMPT.format(q=question, answers=blind_text, rankings=ranked) if make
          else CHAIR_PROMPT.format(q=question, answers=blind_text, rankings=ranked,
                                   tally=tally_line))
    emit(type="chairing", model=chair_model["id"])
    tried, reason = [], ""
    while True:
        r = transport.ask(chair_model, [*prior, {"role": "user", "content": cp}])
        if r.ok:
            s.decision = r.text
            s.chair_model = chair_model
            emit(type="decision", text=r.text, chair=chair_model["id"])
            return s
        # The members have already spoken and their answers are worth keeping. Throwing the
        # whole session away because one model refused to chair it is the same mistake as
        # counting a throttled member as a vote: it lets one failure speak for the board.
        tried.append(chair_model["id"])
        reason = r.reason
        s.chair_failures.append({"model": chair_model["id"], "reason": r.reason})
        emit(type="chair_failed", model=chair_model["id"], reason=r.reason)
        try:
            chair_model = seats.chair(models, members, exclude=set(tried),
                                      allow_paid=allow_paid, tier=tier)
            emit(type="chairing", model=chair_model["id"])
        except seats.NoQuorum:
            s.no_quorum = (f"{len(s.answers)} member(s) answered, but no free model could "
                           f"chair the session (last: {reason}). Their answers are above; "
                           f"the synthesis is missing, not the board.")
            emit(type="no_quorum", reason=s.no_quorum)
            return s


# WHICH KIND OF QUESTION IS THIS. Used only to SUGGEST a mode in the console; it never
# switches on its own.
#
# The first version listed task verbs - build, write, make, create - and missed four out of
# five real ones. "draw a picture using characters" went to DECIDE, so a board that was asked
# to draw a dollar sign spent eleven requests VOTING ON WHETHER DRAWING IT WAS PERMITTED, and
# resolved that the request "is approved". Absurd, and entirely my doing: a list of verbs can
# never be complete, and every word missing from it fails in that direction.
#
# So the test is the other way round. A DECISION is phrased as a question - "should we", "is
# X better", "which one". Everything else is work. Ambiguity now lands on MAKE, which is the
# safe direction here: being handed the work when you wanted an opinion is a mild
# disappointment, while being handed a vote on whether your request is allowed is useless.
QUESTION_OPENERS = (
    "should ", "shall ", "is ", "are ", "was ", "were ", "do ", "does ", "did ", "can ",
    "could ", "would ", "will ", "which ", "why ", "who ", "whose ", "am i", "have we",
    "has ", "must ", "ought ", "may we", "might ",
)
# "what is X" asks; "what would you build" is still a question. Both are decisions.
QUESTION_PHRASES = ("what is", "what are", "what would", "what should", "how should",
                    "how do you feel", "worth it", "better than", "or should")


def looks_like_a_task(q: str) -> bool:
    """True when the text reads like work to do rather than a call to make."""
    t = " ".join((q or "").split()).strip().lower()
    if not t:
        return False
    if t.startswith(QUESTION_OPENERS) or any(p in t[:80] for p in QUESTION_PHRASES):
        return False
    # A bare question mark and nothing else to go on: treat it as a question.
    return not t.rstrip().endswith("?")
