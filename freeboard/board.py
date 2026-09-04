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

ANSWER_PROMPT = (
    "You are one member of an independent board. Answer the question on your own judgement.\n"
    "Be specific and brief: your position, then the single strongest reason for it, then the "
    "one thing that would change your mind.\n\nQUESTION:\n{q}"
)

RANK_PROMPT = (
    "You are one member of an independent board. Below are the other members' answers, with "
    "their identities hidden.\nRank them best to worst on reasoning quality alone -- not on "
    "whether they agree with you.\nReply with the labels in order, best first, then one line "
    "saying why the top one won.\n\nQUESTION:\n{q}\n\nANSWERS:\n{answers}"
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
    labels: dict[str, str] = field(default_factory=dict)   # label -> model id
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
            out.append("DECISION (chair):")
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
        live_catalogue: bool = True) -> Session:
    """Run one board session. Raises `redact.Refused` before anything leaves the machine."""
    return ask_in_context(question, prior=None, transport=transport, models=models, size=size,
                          minimum=minimum, peer_review=peer_review,
                          live_catalogue=live_catalogue)


def ask_in_context(question: str, *, prior: list[dict] | None = None,
                   transport: Transport | None = None, models: list[dict] | None = None,
                   size: int = 5, minimum: int = 3, peer_review: bool = True,
                   live_catalogue: bool = True) -> Session:
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

    members = seats.seat(models, size=size)
    seats.quorum(members, minimum=minimum)
    chair_model = seats.chair(models, members)
    s = Session(question=question, members=members, chair_model=chair_model)

    # 1. independent answers -- each member reads the thread so far, then answers alone
    prompt = ANSWER_PROMPT.format(q=question)
    for m in members:
        r = transport.ask(m, prior + [{"role": "user", "content": prompt}])
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
        rp = RANK_PROMPT.format(q=question, answers=blind_text)
        for m in members:
            if not any(a.model == m["id"] for a in s.answers):
                continue          # a member who did not answer does not get to rank
            r = transport.ask(m, prior + [{"role": "user", "content": rp}])
            if r.ok:
                s.rankings.append(r)

    # 3. the chair
    ranked = "\n\n".join(f"--- ranking by a member ---\n{r.text}" for r in s.rankings) or "(none)"
    cp = CHAIR_PROMPT.format(q=question, answers=blind_text, rankings=ranked)
    r = transport.ask(chair_model, prior + [{"role": "user", "content": cp}])
    if r.ok:
        s.decision = r.text
    else:
        s.no_quorum = f"the chair could not answer ({r.reason}); no decision was synthesised"
    return s
