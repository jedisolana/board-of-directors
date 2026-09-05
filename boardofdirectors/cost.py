"""What a session will cost, said before it is sent.

This is the first thing here that can spend money, so the rules are different from everywhere
else in the program.

  * PAID IS OFF BY DEFAULT and has to be turned on deliberately. There is a stored setting,
    but it only ever unlocks the option - a session never becomes paid because of something
    decided last week.
  * THE ESTIMATE IS SHOWN BEFORE THE SEND, not reported after it. A cost you learn afterwards
    is a bill, not a decision.
  * IT ROUNDS UP, ALWAYS. An estimate that flatters itself is worse than none: the whole point
    is to be trusted at the moment somebody decides whether to press the button, and being
    pleasantly surprised is the only acceptable direction to be wrong in.
  * A MODEL WITH NO PRICE IS NOT FREE. It is unknown, and unknown cannot be consented to, so
    it is refused rather than costed at zero.

Token counts here are estimates too (characters over four), and everything downstream says so.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# What a board turn actually sends, per member: the conversation plus the question, and an
# answer back. Deliberately generous - see "rounds up, always".
DEFAULT_PROMPT_TOKENS = 1200
DEFAULT_OUTPUT_TOKENS = 700


class Unpriced(ValueError):
    """A model that cannot be costed, and so cannot be paid for knowingly."""


@dataclass(frozen=True)
class Estimate:
    usd: float
    free_members: int
    paid_members: int
    per_model: tuple

    @property
    def is_free(self) -> bool:
        return self.paid_members == 0

    def human(self) -> str:
        if self.is_free:
            return "free"
        if self.usd < 0.01:
            return "under $0.01"
        return f"about ${self.usd:,.2f}"


def model_cost(model: dict, prompt_tokens: int, output_tokens: int) -> float:
    """Cost of one call, in dollars. Zero for a free model, raises if unpriced."""
    if model.get("free"):
        return 0.0
    pin, pout = model.get("price_in"), model.get("price_out")
    if pin is None or pout is None:
        raise Unpriced(f"{model.get('id')} has no published price")
    return (prompt_tokens / 1e6) * pin + (output_tokens / 1e6) * pout


def session(members: list[dict], chair: dict | None = None, *, peer_review: bool = True,
            prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
            output_tokens: int = DEFAULT_OUTPUT_TOKENS) -> Estimate:
    """What one board session costs. Every call it will make, priced and summed."""
    rows, total = [], 0.0
    calls = [(m, 1 + (1 if peer_review else 0)) for m in members]
    if chair is not None:
        # the chair reads every answer, so its prompt is the whole meeting
        calls.append((chair, 1))
    for m, n in calls:
        pt = prompt_tokens * (len(members) if m is chair else 1)
        c = model_cost(m, pt, output_tokens) * n
        total += c
        if c:
            rows.append((m["id"], round(c, 6)))
    # round UP to the cent so the number shown is never less than the number charged
    total = math.ceil(total * 100) / 100 if total >= 0.01 else total
    return Estimate(usd=round(total, 6),
                    free_members=sum(1 for m in members if m.get("free")),
                    paid_members=sum(1 for m in members if not m.get("free")),
                    per_model=tuple(sorted(rows, key=lambda r: -r[1])))


def over_cap(estimate: Estimate, cap_usd: float | None) -> bool:
    """A cap is a wall, not a warning. None means no cap set.

    A cap of ZERO is the important case, and it is not a degenerate one. Plenty of people buy
    the $10 for the rate limit alone -- it moves free models from 50 to 1000 requests a day
    and is never meant to be spent. For them the balance is a key, not a wallet, and "paid is
    merely switched off" is one stray click away from being wrong. A zero cap makes it
    impossible rather than merely unselected.
    """
    return cap_usd is not None and estimate.usd > cap_usd


def locked_to_free(cap_usd: float | None) -> bool:
    """True when nothing costing money can run at all."""
    return cap_usd is not None and cap_usd <= 0
