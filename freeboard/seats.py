"""Seating the board: who gets a vote, and why they are allowed to disagree.

The finding this is built on (Verga et al., "Replacing Judges with Juries"): a panel of
several smaller models beats one big judge, *because* the panel is drawn from disjoint model
families -- less intra-model bias, at a fraction of the cost. The word doing the work there
is DISJOINT. Seat three checkpoints of the same family and you have not built a jury, you
have built one model with a stutter, and it will agree with itself and look like consensus.

So the seating rule is a hard one: at most one seat per family. If you ask for more seats
than there are families, you get fewer seats and a reason -- never a padded board.
"""
from __future__ import annotations

from . import catalogue


class NoQuorum(Exception):
    """Not enough independent members to hold a vote."""


def _rank(m: dict) -> tuple:
    """Prefer members that can return a vote as data, then bigger context, then name."""
    return (0 if catalogue.speaks_json(m) else 1,
            -(m.get("context_length") or 0),
            m["id"])


def seat(models: list[dict], size: int = 5, need_json: bool = False,
         prompt_tokens: int = 0, completion_tokens: int = 0,
         exclude: set[str] | None = None) -> list[dict]:
    """Pick up to `size` members, one per family, best first.

    `prompt_tokens`/`completion_tokens` apply the asymmetric fit check, so a member is never
    seated for a job it will refuse halfway through.
    """
    exclude = exclude or set()
    pool = []
    for m in catalogue.deliberative(models):
        if m["id"] in exclude:
            continue
        if need_json and not catalogue.speaks_json(m):
            continue
        ok, _ = catalogue.fits(m, prompt_tokens, completion_tokens)
        if not ok:
            continue
        pool.append(m)

    chosen, seen = [], set()
    for m in sorted(pool, key=_rank):
        if m["family"] in seen:
            continue
        seen.add(m["family"])
        chosen.append(m)
        if len(chosen) >= size:
            break
    return chosen


def quorum(members: list[dict], minimum: int = 3) -> None:
    """A board needs a real majority to be possible.

    Two members cannot outvote each other, so a 2-member 'board' is a coin toss dressed as a
    decision. Three disjoint families is the smallest thing that can actually break a tie.
    """
    if len(members) < minimum:
        fams = sorted({m["family"] for m in members})
        raise NoQuorum(
            f"{len(members)} independent member(s) available, need {minimum} "
            f"(families seated: {', '.join(fams) or 'none'})")


def chair(models: list[dict], members: list[dict], prompt_tokens: int = 0,
          completion_tokens: int = 0) -> dict:
    """The chair reads every member's answer at once, so it is chosen for CONTEXT.

    It is also excluded from the members it will judge: a model that both votes and counts
    the votes is not a chair, it is a thumb on the scale.
    """
    seated = {m["id"] for m in members}
    pool = [m for m in catalogue.deliberative(models)
            if m["id"] not in seated
            and catalogue.fits(m, prompt_tokens, completion_tokens)[0]]
    if not pool:
        raise NoQuorum("no free model left to chair that is not already a voting member")
    return max(pool, key=lambda m: (m.get("context_length") or 0,
                                    m.get("max_completion_tokens") or 0))
