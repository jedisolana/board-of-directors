"""What a free board actually costs you in requests, and how many you get.

THE NUMBERS (openrouter.ai/docs/api-reference/limits, read 2026-09-04):

    20 requests per minute       across free models, uniform
    50 requests per day          if you have purchased less than $10 of credits, all time
    1000 requests per day        if you have purchased at least $10 of credits, all time

The $10 is a one-time, all-time threshold, not a balance you burn down: it changes which row
of the table you are on, permanently. That is a 20x on your daily free capacity for ten
dollars, and it is the single largest lever there is on this platform.

SCOPE, HONESTLY: the docs give the daily figure as "your free model rate limit" and key it on
credits purchased by the ACCOUNT, so it reads as account-wide across all `:free` models --
they do not say so in as many words. This module assumes account-wide, which is the
conservative reading. If you assume per-model instead and you are wrong, you plan a board you
cannot run; assume account-wide and the worst case is you have capacity spare.

So: SPREADING A BOARD ACROSS MANY FREE MODELS DOES NOT MULTIPLY YOUR DAILY BUDGET. It buys
independence (disjoint families vote independently) and it routes around one provider being
slow or down. The daily ceiling is one number for the whole account, and the only thing that
moves it is the $10.
"""
from __future__ import annotations

from dataclasses import dataclass

# OpenRouter answers this itself. GET /api/v1/key returns `is_free_tier`, which is exactly
# the fact the daily limit turns on -- so the question "have you ever put in $10?" never had
# to be asked. It was asked anyway, of a person who cannot be expected to know, and a wrong
# answer silently sets the allowance twenty times too high.
RPM = 20
RPD_WITHOUT_CREDITS = 50
RPD_WITH_CREDITS = 1000
CREDIT_THRESHOLD_USD = 10


@dataclass(frozen=True)
class Budget:
    """The free-tier allowance for one account."""
    credits_purchased_usd: float = 0.0

    @property
    def qualified(self) -> bool:
        return self.credits_purchased_usd >= CREDIT_THRESHOLD_USD

    @property
    def per_day(self) -> int:
        return RPD_WITH_CREDITS if self.qualified else RPD_WITHOUT_CREDITS

    @property
    def per_minute(self) -> int:
        return RPM


def requests_per_session(members: int, rounds: int = 1, chair: bool = True,
                         peer_review: bool = False) -> int:
    """How many API calls one board session costs.

    round 1      each member answers                     -> members
    peer review  each member ranks the others, blind     -> members  (optional)
    chair        one synthesis pass                      -> 1
    """
    n = members * rounds
    if peer_review:
        n += members
    if chair:
        n += 1
    return n


def sessions(budget: Budget, members: int = 5, rounds: int = 1, chair: bool = True,
             peer_review: bool = False) -> dict:
    """How many board sessions the free tier will actually give you.

    The per-minute limit is the one people meet first: a board is BURSTY -- every member is
    asked at once -- so a 6-member board with peer review and a chair is 13 requests fired in
    a second or two, and three of those bursts is your minute gone.
    """
    per = requests_per_session(members, rounds, chair, peer_review)
    return {
        "requests_per_session": per,
        "sessions_per_day": budget.per_day // per,
        "sessions_per_minute": budget.per_minute // per,
        "burst_exceeds_rpm": per > budget.per_minute,
        "per_day": budget.per_day,
        "per_minute": budget.per_minute,
        "qualified": budget.qualified,
    }


def upgrade(members: int = 5, rounds: int = 1, chair: bool = True,
            peer_review: bool = False) -> dict:
    """What the $10 buys, in board sessions rather than in requests."""
    before = sessions(Budget(0), members, rounds, chair, peer_review)
    after = sessions(Budget(CREDIT_THRESHOLD_USD), members, rounds, chair, peer_review)
    gain = (after["sessions_per_day"] / before["sessions_per_day"]
            if before["sessions_per_day"] else float("inf"))
    return {"before": before, "after": after, "cost_usd": CREDIT_THRESHOLD_USD,
            "multiplier": round(gain, 1)}


def pace(members: int, peer_review: bool = False, chair: bool = True) -> float:
    """Seconds to wait between sessions to stay under the per-minute limit."""
    per = requests_per_session(members, 1, chair, peer_review)
    return round(60.0 * per / RPM, 2)
