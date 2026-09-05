"""The call counter -- and why it has to guess.

You would think you could just ask how many free requests you have left. You cannot.
OpenRouter's own docs: "Successful inference responses do not include X-RateLimit-* headers."
And `/api/v1/key` reports CREDITS spent, which on the free tier is zero forever while you burn
through your daily allowance. So there is no number to read.

They tell you the truth exactly once: when you hit the wall. A 429 carries X-RateLimit-Limit,
X-RateLimit-Remaining and X-RateLimit-Reset. So this ledger counts every call it makes, calls
that an ESTIMATE and says so -- and the moment a 429 corrects it, it stops estimating and
reports the real figure.

Two things it cannot see, both stated plainly wherever the number is shown:

  * calls made with the same key by anything that is not this program. The limit is
    account-wide, so the real remaining is always <= this estimate, never more.
  * the exact reset moment. The day is taken as UTC midnight until a 429 tells us otherwise.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from . import atomic, config

LEDGER = os.path.join(config.HOME, "usage.json")


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _locked():
    """The ledger's lock, borrowed from `atomic` rather than kept as a second copy here.

    The copy was Unix-only - a bare `import fcntl` at module scope, which meant the whole
    package failed to import on Windows before anything ran. Two implementations of one lock
    is one too many, and this was the one nobody was maintaining.
    """
    return atomic.locked(LEDGER)



def _load() -> dict:
    return atomic.read_json(LEDGER, None) or {"days": {}, "truth": None}


def _save(d: dict) -> None:
    config._ensure_home()
    atomic.write_json(LEDGER, d)


def record(model: str, ok: bool, day: str | None = None, provider_side: bool = False) -> None:
    """One call made. Failures count too -- unless the provider, not OpenRouter, refused it."""
    with _locked():
        _record(model, ok, day, provider_side)


def _record(model: str, ok: bool, day: str | None = None, provider_side: bool = False) -> None:
    d = _load()
    day = day or _today()
    rec = d["days"].setdefault(day, {"calls": 0, "failed": 0, "provider_busy": 0, "models": {}})
    # A provider at capacity refused before OpenRouter spent anything of yours. It is still
    # worth showing -- it is why a member did not answer - but it must not move the meter.
    if provider_side:
        rec["provider_busy"] = rec.get("provider_busy", 0) + 1
    else:
        rec["calls"] += 1
        if not ok:
            rec["failed"] += 1
    rec["models"][model] = rec["models"].get(model, 0) + 1
    # keep a fortnight, no more -- this is a counter, not an archive
    for old in sorted(d["days"])[:-14]:
        d["days"].pop(old, None)
    _save(d)


def learn_from_429(limit: int | None, remaining: int | None, reset: str | None) -> None:
    """The one moment OpenRouter tells the truth. Keep it, WITH the call count at that moment.

    Without that count the measured figure is a photograph: correct when taken and never
    updated again, so the console would sit on "612 remaining" through another two hundred
    calls. The stored `calls_at` is what lets later calls be subtracted from it.
    """
    if limit is None and remaining is None:
        return
    day = _today()
    with _locked():
        d = _load()
        d["truth"] = {"at": time.time(), "day": day, "limit": limit, "remaining": remaining,
                      "reset": reset, "calls_at": (d["days"].get(day) or {}).get("calls", 0)}
        _save(d)


@dataclass
class Status:
    day: str
    calls: int
    since_reset: bool
    source: str          # "estimate" | "429" | "analytics"
    failed: int
    provider_busy: int
    per_model: dict
    allowance: int
    remaining: int
    measured: bool          # True once a 429 has told us the real number
    tier_usd: float
    resets_in: str

    @property
    def qualified(self) -> bool:
        from .budget import CREDIT_THRESHOLD_USD
        return self.tier_usd >= CREDIT_THRESHOLD_USD


def _resets_in() -> str:
    now = time.gmtime()
    secs = (23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec)
    return f"{secs // 3600}h {secs % 3600 // 60}m"


def reset_today(day: str | None = None) -> dict:
    """Forget today's count and start the meter honest.

    Needed because a counter can be WRONG, and a wrong count is not self-correcting: today's
    figure was inflated by a bug that counted retries and provider-side refusals as spent
    allowance, and no amount of correct counting afterwards repairs a number that was already
    too high. The alternative - quietly rewriting the file - would leave someone with a meter
    they cannot explain and no idea it had been touched.

    It clears the count, not the history of what the day was: the previous figure is returned
    so the caller can say what it discarded.
    """
    with _locked():
        d = _load()
        day = day or _today()
        was = d["days"].pop(day, {"calls": 0, "failed": 0, "provider_busy": 0, "models": {}})
        # Remember that this day's number starts from a reset. Otherwise the meter reads
        # "0 / 50" and promises fifty requests it has no basis for: clearing OUR count gives
        # back none of OpenRouter's allowance, and what was really spent before the reset is
        # now unknowable. A meter that cannot know must say so, not round its ignorance up.
        d.setdefault("reset_days", {})[day] = time.time()
        if (d.get("truth") or {}).get("day") == day:
            d["truth"] = None          # a measured figure from the discarded day is discarded too
        _save(d)
    return was


def status(tier_usd: float | None = None, true_calls: int | None = None) -> Status:
    """`true_calls`, when given, is OpenRouter's own figure and outranks everything here."""
    from .budget import Budget
    d = _load()
    day = _today()
    rec = d["days"].get(day, {"calls": 0, "failed": 0, "provider_busy": 0, "models": {}})
    tier_usd = config.tier() if tier_usd is None else tier_usd
    allowance = Budget(tier_usd).per_day

    truth = d.get("truth") or {}
    measured = bool(truth) and truth.get("day") == day and truth.get("remaining") is not None
    if measured:
        # OpenRouter's figure, minus everything this program has spent since it said so
        since = max(0, rec["calls"] - int(truth.get("calls_at", rec["calls"])))
        remaining = max(0, int(truth["remaining"]) - since)
        if truth.get("limit"):
            allowance = int(truth["limit"])
    else:
        remaining = max(0, allowance - rec["calls"])

    # The ledger stays truthful. If the count runs past the allowance that is a FACT worth
    # seeing - it means the allowance is not what we think, or something else is using the
    # key - and clamping it here would hide the very thing that needs explaining. The display
    # is where an over-count gets said in words.
    source = "estimate"
    if measured:
        source = "429"
    if true_calls is not None:
        # OpenRouter's own count. It ends the guessing entirely - including after a reset,
        # because the truth does not care what we discarded.
        rec = dict(rec, calls=true_calls)
        remaining = max(0, allowance - true_calls)
        measured = True
        source = "analytics"
    return Status(day=day, calls=rec["calls"],
                  since_reset=(day in (d.get("reset_days") or {})) and source != "analytics",
                  failed=rec["failed"], provider_busy=rec.get("provider_busy", 0),
                  per_model=rec.get("models", {}), allowance=allowance,
                  remaining=remaining, measured=measured, tier_usd=tier_usd,
                  source=source, resets_in=_resets_in())
