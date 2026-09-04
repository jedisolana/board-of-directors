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
    "openrouter/free",                       # a router, not a model -- it hides which member answered
    "nvidia/nemotron-3.5-content-safety",    # a guardrail classifier, not a reasoner
}
NOT_DELIBERATIVE_PREFIX = ("google/lyria-",)  # music/audio generation


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
    return {
        "id": m["id"],
        "name": m.get("name") or m["id"],
        "family": m["id"].split("/")[0],
        "context_length": m.get("context_length"),
        "max_completion_tokens": tp.get("max_completion_tokens"),
        "is_moderated": tp.get("is_moderated"),
        "input_modalities": arch.get("input_modalities") or [],
        "supported_parameters": sorted(m.get("supported_parameters") or []),
    }


def fetch(timeout: float = 20.0) -> dict:
    """The live catalogue. Public endpoint -- no key needed, and none is sent."""
    req = urllib.request.Request(MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)["data"]
    return {
        "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": MODELS_URL,
        "total_models_seen": len(data),
        "models": [_normalise(m) for m in sorted(data, key=lambda x: x["id"]) if is_free(m)],
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


def deliberative(models: list[dict]) -> list[dict]:
    """Free models that could actually sit on a board."""
    out = []
    for m in models:
        bid = base_id(m["id"])
        if bid in NOT_DELIBERATIVE or m["id"] in NOT_DELIBERATIVE:
            continue
        if bid.startswith(NOT_DELIBERATIVE_PREFIX):
            continue
        if "text" not in (m.get("input_modalities") or ["text"]):
            continue
        out.append(m)
    return out


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
