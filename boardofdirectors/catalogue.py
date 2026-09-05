"""The catalogue: which models are free right now, and what each one can actually do.

Free models come and go, and every hard-coded free list rots. So this reads the live
catalogue when it can and falls back to the snapshot shipped in `data/` when it can't --
and it always tells you which one it used, and how old it is.

The fields kept here are the ones that decide whether a model can hold a board seat:

  context_length / max_completion_tokens
      ASYMMETRIC. A model with room for your prompt may still refuse your output length.
      Both have to be checked; checking only the context window is the common bug.

  supported_parameters
      Free variants are NOT the paid model with a zero price. They are the paid model with
      parameters removed -- `response_format` and `structured_outputs` are the two that
      matter for a board, because that is how you get a vote back as data instead of prose.
      Sending a parameter a model does not support is not an error (OpenRouter drops it),
      which is worse than an error: you ask for JSON, you silently get an essay.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

MODELS_URL = "https://openrouter.ai/api/v1/models"
SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "free-models.json")

# Free-priced rows that are not board material. A seat needs a model that can hold an
# argument; these can't, and seating them looks like a working board that never deliberates.
NOT_DELIBERATIVE = {
    "nvidia/nemotron-3.5-content-safety",    # a guardrail classifier, not a reasoner
}
# Every `openrouter/*` id is a router rather than a model: auto, free, fusion, pareto-code and
# the rest all pick something else and answer as themselves. A board seat has to be a named
# model - otherwise "one seat per family" guarantees nothing, because two routers can quietly
# choose the same underlying model and the independence the whole thing rests on is gone.
NOT_DELIBERATIVE_FAMILY = {"openrouter"}
NOT_DELIBERATIVE_PREFIX = ("google/lyria-",)  # music/audio generation


def _per_million(v) -> float | None:
    """Price per million tokens, or None when there isn't one.

    OpenRouter uses -1 for routers whose price depends on which model they end up choosing.
    Multiplied out that reads as MINUS A MILLION DOLLARS per million tokens, and any
    "cheapest first" sort puts it at the top - a cost estimate that pays you to use it. A
    price that is not a price must be None, not a number pointing the wrong way.
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return round(n * 1_000_000, 4) if n >= 0 else None


def is_free(model: dict) -> bool:
    """Both prices zero. A model free on prompt but metered on completion is not free."""
    p = model.get("pricing") or {}
    try:
        return float(p.get("prompt", 1)) == 0 and float(p.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


def _normalise(m: dict) -> dict:
    tp = m.get("top_provider") or {}
    arch = m.get("architecture") or {}
    # Artificial Analysis scores, when OpenRouter has them. THREE separate indices, because
    # "good" is not one thing: a coding specialist can sit near the top on code and at the
    # very bottom on agentic work. Missing is common and must stay missing -- a model with no
    # score is unmeasured, not bad, and filling it with a zero would rank it last on purpose.
    aa = ((m.get("benchmarks") or {}).get("artificial_analysis") or {})
    pricing = m.get("pricing") or {}
    return {
        "id": m["id"],
        "name": m.get("name") or m["id"],
        "free": is_free(m),
        # per-MILLION tokens, which is how everyone quotes them and how nobody stores them:
        # OpenRouter's own figures are per token, and reading one as the other is a
        # million-fold error in the direction of "this is basically free".
        "price_in": _per_million(pricing.get("prompt")),
        "price_out": _per_million(pricing.get("completion")),
        "family": m["id"].split("/")[0],
        "context_length": m.get("context_length"),
        "max_completion_tokens": tp.get("max_completion_tokens"),
        "is_moderated": tp.get("is_moderated"),
        "input_modalities": arch.get("input_modalities") or [],
        "supported_parameters": sorted(m.get("supported_parameters") or []),
        "score": {"thinking": aa.get("intelligence_index"),
                  "coding": aa.get("coding_index"),
                  "agentic": aa.get("agentic_index")},
    }


def fetch(timeout: float = 20.0) -> dict:
    """The live catalogue. Public endpoint -- no key needed, and none is sent."""
    req = urllib.request.Request(MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)["data"]
    # EVERY model is kept now, free and paid, with its price. Filtering paid ones out here
    # was fine while the board could only be free, but it meant "is this affordable?" was a
    # question the program could not even ask. Paid models are excluded at SEATING time
    # instead, by an explicit setting, so the default is unchanged and the reason is visible.
    return {
        "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": MODELS_URL,
        "total_models_seen": len(data),
        "models": [_normalise(m) for m in sorted(data, key=lambda x: x["id"])],
    }


def snapshot() -> dict:
    with open(SNAPSHOT) as f:
        return json.load(f)


def load(live: bool = True) -> dict:
    """The catalogue plus an honest note about where it came from.

    A network failure must not become a silent fallback to a stale list -- the board's whole
    premise is free capacity, and a model that stopped being free yesterday will bill you.
    """
    if live:
        try:
            c = fetch()
            c["origin"] = "live"
            return c
        except Exception as e:
            c = snapshot()
            c["origin"] = f"snapshot (live fetch failed: {type(e).__name__})"
            return c
    c = snapshot()
    c["origin"] = "snapshot (offline)"
    return c


def base_id(model_id: str) -> str:
    """The model without its variant suffix: `x/y:free` -> `x/y`.

    Every exclusion here is about what the model IS, not which variant you asked for, so the
    comparison has to drop the suffix. It did not, and `nemotron-3.5-content-safety:free` --
    a guardrail classifier -- was quietly seatable as a board member.
    """
    return model_id.split(":", 1)[0]


def deliberative(models: list[dict], allow_paid: bool = False) -> list[dict]:
    """Models that could actually sit on a board.

    Paid models are excluded unless explicitly allowed. This is the one place in the program
    where a wrong default costs real money, so the default is the free one and the caller has
    to say otherwise every time -- there is no remembered "allow paid" that could quietly
    apply to a session nobody meant to pay for.
    """
    out = []
    for m in models:
        bid = base_id(m["id"])
        if bid in NOT_DELIBERATIVE or m["id"] in NOT_DELIBERATIVE:
            continue
        if m["id"].split("/")[0] in NOT_DELIBERATIVE_FAMILY:
            continue
        if bid.startswith(NOT_DELIBERATIVE_PREFIX):
            continue
        if "text" not in (m.get("input_modalities") or ["text"]):
            continue
        if not allow_paid and not m.get("free", True):
            continue
        # A model whose price is unknown cannot be costed, so it cannot be consented to.
        if allow_paid and not m.get("free", True) and m.get("price_in") is None:
            continue
        out.append(m)
    return out


def score(m: dict, kind: str = "coding") -> float | None:
    """One benchmark index, or None when the model has never been measured."""
    return (m.get("score") or {}).get(kind)


def rank(models: list[dict], kind: str = "coding") -> list[dict]:
    """Best first on one index. Unmeasured models go last, in name order, never zeroed."""
    scored = [m for m in models if score(m, kind) is not None]
    unscored = [m for m in models if score(m, kind) is None]
    scored.sort(key=lambda m: -score(m, kind))
    unscored.sort(key=lambda m: m["id"])
    return scored + unscored


def speaks_json(m: dict) -> bool:
    """Can this model return a vote as data rather than prose?"""
    p = set(m.get("supported_parameters") or [])
    return bool(p & {"response_format", "structured_outputs"})


def fits(m: dict, prompt_tokens: int, completion_tokens: int) -> tuple[bool, str]:
    """The asymmetric check. Returns (ok, why-not)."""
    ctx = m.get("context_length") or 0
    out = m.get("max_completion_tokens")
    if ctx and prompt_tokens + completion_tokens > ctx:
        return False, f"needs {prompt_tokens + completion_tokens} of {ctx} context"
    if out is not None and completion_tokens > out:
        return False, f"wants {completion_tokens} out, cap is {out}"
    return True, ""
