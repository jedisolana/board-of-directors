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
    "one thing that would change your mind.\n\nQUESTION:\n{q}"
)

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
    "You are the chair of a board. You did not vote. Below are the members' independent "
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
            out.append(f"  [{label}] {a.model}")
            for line in a.text.splitlines():
                out.append(f"        {line}")
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


def _blind(answers: list[Answer]) -> tuple[str, dict[str, str]]:
    """Label the answers A, B, C... and keep the mapping for the audit trail."""
    labels, blocks = {}, []
    for i, a in enumerate(answers):
        label = f"Member {string.ascii_uppercase[i % 26]}"
        labels[label] = a.model
        blocks.append(f"--- {label} ---\n{a.text}")
    return "\n\n".join(blocks), labels


def ask(question: str, *, transport: Transport | None = None, models: list[dict] | None = None,
        size: int = 5, minimum: int = 3, peer_review: bool = True,
        live_catalogue: bool = True, kind: str = "decide") -> Session:
    """Run one board session. Raises `redact.Refused` before anything leaves the machine."""
    return ask_in_context(question, prior=None, transport=transport, models=models, size=size,
                          minimum=minimum, peer_review=peer_review,
                          live_catalogue=live_catalogue, kind=kind)


def ask_in_context(question: str, *, prior: list[dict] | None = None,
                   transport: Transport | None = None, models: list[dict] | None = None,
                   size: int = 5, minimum: int = 3, peer_review: bool = True,
                   live_catalogue: bool = True, kind: str = "decide",
                   members: list[dict] | None = None) -> Session:
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
    members = members or seats.seat(models, size=size)
    seats.quorum(members, minimum=minimum)
    chair_model = seats.chair(models, members)
    s = Session(question=question, members=members, chair_model=chair_model, kind=kind)

    # 1. independent answers -- each member reads the thread so far, then answers alone
    make = (kind == "make")
    prompt = (MAKE_PROMPT if make else ANSWER_PROMPT).format(q=question)
    for m in members:
        r = transport.ask(m, [*prior, {"role": "user", "content": prompt}])
        (s.answers if r.ok else s.failures).append(r)

    # A board is the members who actually spoke. Silence is not consent.
    if len(s.answers) < minimum:
        s.no_quorum = (f"only {len(s.answers)} of {len(members)} member(s) answered "
                       f"({len(s.failures)} failed); {minimum} needed. "
                       f"A missing member is not a vote either way.")
        return s

    blind_text, s.labels = _blind(s.answers)

    # 2. blind peer ranking
    if peer_review and len(s.answers) > 1:
        rp = (MAKE_RANK_PROMPT if make else RANK_PROMPT).format(q=question, answers=blind_text)
        for m in members:
            if not any(a.model == m["id"] for a in s.answers):
                continue          # a member who did not answer does not get to rank
            r = transport.ask(m, [*prior, {"role": "user", "content": rp}])
            if r.ok:
                s.rankings.append(r)

    # 3. the chair
    ranked = "\n\n".join(f"--- ranking by a member ---\n{r.text}" for r in s.rankings) or "(none)"
    cp = (MAKE_CHAIR_PROMPT if make else CHAIR_PROMPT).format(
        q=question, answers=blind_text, rankings=ranked)
    tried, reason = [], ""
    while True:
        r = transport.ask(chair_model, [*prior, {"role": "user", "content": cp}])
        if r.ok:
            s.decision = r.text
            s.chair_model = chair_model
            return s
        # The members have already spoken and their answers are worth keeping. Throwing the
        # whole session away because one model refused to chair it is the same mistake as
        # counting a throttled member as a vote: it lets one failure speak for the board.
        tried.append(chair_model["id"])
        reason = r.reason
        s.chair_failures.append({"model": chair_model["id"], "reason": r.reason})
        try:
            chair_model = seats.chair(models, members, exclude=set(tried))
        except seats.NoQuorum:
            s.no_quorum = (f"{len(s.answers)} member(s) answered, but no free model could "
                           f"chair the session (last: {reason}). Their answers are above; "
                           f"the synthesis is missing, not the board.")
            return s


# A cheap guess at which kind of question this is, used only to SUGGEST a mode in the UI.
# It never switches on its own: getting this wrong silently would be worse than asking.
_MAKE_HINTS = (
    "build", "write", "make", "create", "generate", "implement", "code", "draft",
    "design a", "refactor", "fix", "add a", "convert", "translate", "rewrite",
)


def looks_like_a_task(q: str) -> bool:
    """True when the question reads like work to do rather than a call to make."""
    first = (q or "").strip().lower()
    if first.startswith(("should ", "is ", "are ", "do we", "does ", "which ", "would ",
                         "can we", "why ", "what is", "what are")):
        return False
    return any(first.startswith(w) or f" {w} " in first[:80] for w in _MAKE_HINTS)
