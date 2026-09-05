"""Presets: the shapes people keep reaching for, as one call each.

A recipe is a framing of the QUESTION plus the right kind of session. Nothing here changes the
engine -- the members still answer independently, rank each other blind, and a chair that did
not vote writes the decision. What a recipe adds is the part people got wrong on their own:
asking a jury to dream, or asking a competition to judge.

Every recipe returns the same `Session` `board.ask` does, and takes the same keyword arguments
(`transport=`, `members=`, `minimum=`, `peer_review=`, `on_event=`, ...), so a program that
already uses the board does not learn a second interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import board, catalogue, codebase, redact, seats
from .transport import OfflineTransport, Transport

# ------------------------------------------------------------------- creative

DREAM = (
    "Dream on this theme. Write ONE complete piece - a scene, a story, an image in words, a "
    "vision of how something could be - not a plan for one and not a list of ideas. Be vivid "
    "and specific, and let it go somewhere the theme did not say. Length: whatever the piece "
    "needs, and no longer.\n\nTHEME:\n{theme}"
)


def dream(theme: str, *, size: int = 6, **kw) -> board.Session:
    """Several models from several companies each dream, independently, on one theme.

    A competition, not a jury: every member produces a whole piece, the blind ranking judges
    which actually did it, and the chair delivers the winner. The point of running it as a
    board is that you keep all of them -- `session.answers` is six dreams, not one -- and the
    chair's `decision` is the one the board thought best.
    """
    return board.ask(DREAM.format(theme=theme), kind="make", size=size, **kw)


BRAINSTORM = (
    "Produce {n} distinct ideas for this. For each: a name, one sentence on what it is, one on "
    "why it might work, and its biggest risk. Distinct means a different mechanism, not the same "
    "idea in other words. Put your best idea first.\n\nFOR:\n{topic}"
)


def brainstorm(topic: str, *, ideas: int = 5, size: int = 6, **kw) -> board.Session:
    """An idea-making team. Every member brings its own list; you keep them all.

    Six members with five ideas each is thirty ideas from six companies' worth of training
    data, and the chair's `decision` is the list the blind ranking put on top. Read all of
    `session.answers` -- the second-best list usually has the idea the winner missed.
    """
    return board.ask(BRAINSTORM.format(n=ideas, topic=topic), kind="make", size=size, **kw)


BUILD = (
    "Write the code for this. Return complete, runnable code: no placeholders, no `...`, no "
    "'implement here'. One line above it naming the language and any assumption you had to "
    "make. If it is too large to finish, finish the most useful complete part and say in one "
    "line what is missing.\n\nBUILD:\n{task}"
)


def build(task: str, *, size: int = 6, **kw) -> board.Session:
    """A code-making team. A competition: each member writes it, the ranking judges which
    attempts actually run and actually do the task, the chair delivers the winner. For
    changing an existing folder rather than writing something new, see `patch` and the
    console's work button -- that path diffs against disk and never writes on its own."""
    return board.ask(BUILD.format(task=task), kind="make", size=size, **kw)


RED_TEAM = (
    "Break this. Below is a plan, and your one job is to find how it fails. Produce the single "
    "most damaging failure that is also likely: what goes wrong, the exact sequence of events "
    "that gets there, and what the plan would need in order to survive it. Concrete beats "
    "clever; a failure you can name a date for beats one you cannot.\n\nPLAN:\n{plan}"
)


def red_team(plan: str, *, size: int = 6, **kw) -> board.Session:
    """Six attackers, one plan. Run as a competition so the ranking rewards the most damaging
    attack rather than the most agreeable one, and the chair delivers the one to fix first."""
    return board.ask(RED_TEAM.format(plan=plan), kind="make", size=size, **kw)


# ------------------------------------------------------------------- judgement

CHECK_IDEA = (
    "Judge this idea as if it will be built exactly as described. Take a position: is it "
    "worth doing? Name the single strongest objection, whether or not you share it, and say "
    "what evidence would change your mind. Judge the idea, not the wording of it.\n\n"
    "IDEA:\n{idea}"
)


def check_idea(idea: str, *, size: int = 6, **kw) -> board.Session:
    """An idea-checking team. A jury: each member takes a position and declares a vote.

    `session.tally` is the count, `session.decision` the chair's synthesis with the strongest
    dissent kept in. A member that failed to answer is a failure, not a vote either way.
    """
    return board.ask(CHECK_IDEA.format(idea=idea), kind="decide", size=size, **kw)


REVIEW = (
    "Review the text below and answer this question about it: {ask}\n"
    "Point at specific passages. Say what would have to change for your answer to change.\n\n"
    "TEXT:\n{text}"
)


def review(text: str, ask: str = "Is it ready to go out as written?", *, size: int = 6,
           **kw) -> board.Session:
    """A jury over a document -- a plan, a post, a spec, a message before it is sent."""
    return board.ask(REVIEW.format(ask=ask, text=text), kind="decide", size=size, **kw)


# ------------------------------------------------------------------- code

def audit(path: str, ask: str = "", *, budget_tokens: int | None = None,
          send_anyway: bool = False, size: int = 6, **kw) -> board.Session:
    """An audit team over a folder of code. The same thing the console's button does.

    The folder is scanned first and every file is run through the secret seam. A folder with
    findings is REFUSED -- `redact.Refused` is raised naming them -- unless `send_anyway=True`,
    because the alternative is six outside companies receiving a credentials file that was
    sitting in the project. The refusal is the default; the override has to be typed.

    Files that do not fit the budget are named in the message rather than silently dropped.
    With `members=` given, the budget defaults to sixty percent of the smallest window on the
    board, so every member reads the same tree.
    """
    sc = codebase.scan(path)
    if sc.findings and not send_anyway:
        raise redact.Refused([redact.Finding("code scan", rel, what.split(": ")[-1])
                              for rel, what in sc.findings[:12]])
    if budget_tokens is None:
        members = kw.get("members") or []
        smallest = min((m.get("context_length") or 0) for m in members) if members else 0
        budget_tokens = int(smallest * 0.6) if smallest else 100_000
    msg = codebase.audit_message(sc, budget_tokens, ask=ask)
    # The engine has its own seam and refuses the same text. The override travels with it;
    # otherwise "send anyway" passes here and is refused one call deeper, twice.
    return board.ask(msg, kind="decide", size=size, send_anyway=send_anyway, **kw)


# ------------------------------------------------------------------- the line

SUPPLY_STEP = (
    "You are station {i} of {n} on a production line. Every station is a different model, and "
    "each does one step.\n\nYOUR STEP: {step}\n\nWHAT THE PREVIOUS STATION HANDED YOU:\n{carry}"
    "\n\nDo your step completely and hand forward a result the next station can work from "
    "directly. Do not redo earlier steps and do not start later ones."
)


@dataclass
class Link:
    step: str
    model: str
    text: str | None
    failed: str | None = None        # why, when the station did not deliver


@dataclass
class Chain:
    steps: list[Link] = field(default_factory=list)

    @property
    def result(self) -> str | None:
        """What came off the end of the line - or None if it broke before the end."""
        done = [x for x in self.steps if x.text is not None]
        return done[-1].text if len(done) == len(self.steps) and done else None

    @property
    def broke_at(self) -> Link | None:
        return next((x for x in self.steps if x.failed), None)


def supply_chain(steps: list[str], material: str = "", *, transport: Transport | None = None,
                 models: list[dict] | None = None, members: list[dict] | None = None,
                 live_catalogue: bool = True, allow_paid: bool = False,
                 tier: str | None = None, send_anyway: bool = False) -> Chain:
    """A different model works each step, handing its output down the line.

    Not a board: nobody votes and nobody ranks. It is the other thing a room full of models
    from different companies can do -- an assembly line, where the model that is good at
    outlining hands to the one that is good at writing, who hands to the one that is good at
    cutting. Seats are one per family, like a board; with more steps than families, the
    stations repeat in order and `Link.model` says who did what.

    A station that fails stops the line -- the later steps had nothing to work from -- and
    `broke_at` names it. Nothing is glued together to look finished.
    """
    if not send_anyway:
        redact.check(material + "\n" + "\n".join(steps))
    if models is None:
        models = catalogue.load(live=live_catalogue)["models"]
    transport = transport or OfflineTransport()
    members = members or seats.seat(models, size=len(steps), allow_paid=allow_paid, tier=tier)
    if not members:
        raise seats.NoQuorum("no model available to work the line")
    chain, carry, n = Chain(), material, len(steps)
    for i, step in enumerate(steps):
        m = members[i % len(members)]
        prompt = SUPPLY_STEP.format(i=i + 1, n=n, step=step,
                                    carry=carry or "(nothing - you are the first station)")
        r = transport.ask(m, [{"role": "user", "content": prompt}])
        if not r.ok:
            chain.steps.append(Link(step, m["id"], None, failed=r.reason))
            break
        chain.steps.append(Link(step, m["id"], r.text))
        carry = r.text
    return chain
