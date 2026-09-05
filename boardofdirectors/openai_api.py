"""An OpenAI-shaped endpoint, so anything can use the board.

The library covers Python. Everything else - a Node bot, a shell script, an existing tool
someone already has - had no way in except reverse-engineering the page's internal endpoints.

So the board speaks the one dialect every LLM tool already speaks. Point any OpenAI-compatible
client at this server, ask for the model `board`, and what comes back is a whole board's
decision in the shape the client already parses:

    client = OpenAI(base_url="http://127.0.0.1:8420/v1", api_key="unused")
    client.chat.completions.create(model="board", messages=[...])

The model name selects the shape rather than a model:

    board            a jury: positions, blind ranking, a chair's decision
    board:make       a competition: attempts, ranked, the best one delivered
    board:3          three seats instead of five
    <any model id>   passed straight through to that one model

WHAT IT DOES NOT PRETEND. A board is not a chat completion, and the differences are reported
rather than flattened: `usage` carries the real request count, and a `board` object on the
response holds every member's answer, who failed and why, the tally, and the chair. A client
that ignores it gets a normal-looking completion; a client that reads it can see the vote.

It binds to 127.0.0.1 like the rest of the server. There is no auth, because there is nothing
to authenticate against on a loopback socket - and that is exactly why it must not be exposed
without one. See `serve(host=...)`, which says so loudly.
"""
from __future__ import annotations

import time
import uuid

from . import board as board_mod
from . import catalogue, seats


def parse_model(name: str) -> dict:
    """Turn the `model` field into what the board should do."""
    raw = (name or "board").strip()
    if not raw.lower().startswith("board"):
        return {"single": raw}
    spec = {"single": None, "kind": "decide", "size": seats.DEFAULT_SEATS}
    for part in raw.split(":")[1:]:
        part = part.strip().lower()
        if part in ("decide", "make"):
            spec["kind"] = part
        elif part.isdigit():
            # 9 is the most the free tier can serve in a minute: N answers, then N rankings
            # in one burst, then a chair - 9+9+1 = 19 under a limit of 20. Twelve fires 25.
            spec["size"] = max(1, min(int(part), seats.MAX_SEATS))
    return spec


def completion(text: str, model: str, requests: int, extra: dict | None = None) -> dict:
    """The OpenAI response shape, with the board's own detail alongside rather than instead.

    `usage` reports REQUESTS, not tokens. Tokens would be a guess here and a guess in that
    field would be read as a measurement - a board session's real cost is the number of calls
    it made, and that is the number a caller needs to budget against.
    """
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                  "requests": requests},
        **({"board": extra} if extra else {}),
    }


def error(msg: str, code: str = "invalid_request_error", status: int = 400) -> tuple[dict, int]:
    return {"error": {"message": msg, "type": code, "code": None}}, status


def run(payload: dict, models: list[dict], transport, *, members=None, minimum: int = 3,
        allow_paid: bool = False, tier: str | None = None) -> tuple[dict, int]:
    """Handle one /v1/chat/completions call. Returns (body, http status)."""
    messages = payload.get("messages") or []
    if not messages:
        return error("messages is required")
    spec = parse_model(payload.get("model", "board"))

    if spec.get("single"):
        chosen = next((m for m in models if m["id"] == spec["single"]), None)
        if chosen is None:
            return error(f"no such model: {spec['single']}", "model_not_found", 404)
        # The paid gate used to guard only the board path, and this one walked straight past
        # it: name a paid model in the request and it was called, with paid models switched
        # off in the console and the spend cap at zero. Anything holding this endpoint's
        # address could spend money the owner had explicitly locked. A gate that covers one
        # branch is not a gate, and this is the branch where the money actually leaves.
        if not chosen.get("free") and not allow_paid:
            return error(f"{chosen['id']} is a paid model, and paid models are not allowed "
                         "on this request", "paid_not_allowed", 403)
        r = transport.ask(chosen, messages)
        if not r.ok:
            return error(r.reason, "upstream_error", 502)
        return completion(r.text, chosen["id"], 1), 200

    question = messages[-1].get("content", "")
    prior = messages[:-1]
    try:
        s = board_mod.ask_in_context(
            question, prior=prior, transport=transport, models=models, members=members,
            size=spec["size"], minimum=minimum, kind=spec["kind"],
            peer_review=bool(payload.get("peer_review", True)),
            allow_paid=allow_paid, tier=tier)
    except seats.NoQuorum as e:
        return error(str(e), "no_quorum", 409)

    detail = {
        "kind": s.kind,
        "members": [m["id"] for m in s.members],
        "chair": s.chair_model["id"],
        "tally": s.tally,
        "answers": [{"label": next((k for k, v in s.labels.items() if v == a.model), "?"),
                     "model": a.model, "vote": board_mod.read_vote(a.text),
                     "text": board_mod.strip_vote(a.text)} for a in s.answers],
        # A member that failed is reported, never folded into the answer. A caller budgeting
        # on this needs to know the decision came from four models and not five.
        "failures": [{"model": f.model, "reason": f.reason} for f in s.failures],
        "rankings_received": len(s.rankings), "rankings_failed": len(s.ranking_failures),
        "no_quorum": s.no_quorum,
    }
    if s.no_quorum:
        body, status = error(s.no_quorum, "no_quorum", 409)
        body["board"] = detail
        return body, status
    return completion(s.decision or "", payload.get("model", "board"), s.requests, detail), 200


def model_list(models: list[dict], tier: str = "free") -> dict:
    """`GET /v1/models`, so a client's model picker shows the boards as well."""
    now = int(time.time())
    rows = [{"id": "board", "object": "model", "created": now, "owned_by": "board-of-directors"},
            {"id": "board:make", "object": "model", "created": now, "owned_by": "board-of-directors"},
            {"id": "board:3", "object": "model", "created": now, "owned_by": "board-of-directors"}]
    rows += [{"id": m["id"], "object": "model", "created": now,
              "owned_by": m["family"]} for m in catalogue.deliberative(models, tier=tier)]
    return {"object": "list", "data": rows}
