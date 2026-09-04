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

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import config

LEDGER = os.path.join(config.HOME, "usage.json")


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@contextmanager
def _locked():
    """Serialise read-modify-write on the ledger.

    Every counter update is read, add one, write. Two consoles against the same home - or a
    board firing members concurrently - interleave those and calls vanish. An undercount is
    the dangerous direction here: it reports headroom that is not there.
    """
    config._ensure_home()
    # Held open deliberately across the yield: the advisory lock lives as long as the
    # descriptor does, so a context manager here would release it before the caller writes.
    fh = open(LEDGER + ".lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _load() -> dict:
    try:
        with open(LEDGER) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"days": {}, "truth": None}


def _save(d: dict) -> None:
    config._ensure_home()
    tmp = LEDGER + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, LEDGER)


def record(model: str, ok: bool, day: str | None = None) -> None:
    """One call made. Failures count too -- a rejected request still hit the platform."""
    with _locked():
        _record(model, ok, day)


def _record(model: str, ok: bool, day: str | None = None) -> None:
    d = _load()
    day = day or _today()
    rec = d["days"].setdefault(day, {"calls": 0, "failed": 0, "models": {}})
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
    failed: int
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


def status(tier_usd: float | None = None) -> Status:
    from .budget import Budget
    d = _load()
    day = _today()
    rec = d["days"].get(day, {"calls": 0, "failed": 0, "models": {}})
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

    return Status(day=day, calls=rec["calls"], failed=rec["failed"],
                  per_model=rec.get("models", {}), allowance=allowance,
                  remaining=remaining, measured=measured, tier_usd=tier_usd,
                  resets_in=_resets_in())
