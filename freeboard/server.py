"""The local console: a small server on your own machine, and a page in your own browser.

WHY LOCAL. The page needs your OpenRouter key to be useful, and a page served from the
internet cannot hold a key -- whoever opens it gets it. So the key stays in a 0600 file on
your disk, this server reads it, and the browser never sees it. The browser talks to this;
this talks to OpenRouter. Your key does not cross the gap.

It binds to 127.0.0.1 only. Nothing on your network can reach it, let alone the internet.

ONE CONVERSATION, TWO MODES. The thread is the spine. Most turns go to a single model for one
request. When a question is worth more, the same thread switches to board mode: every member
gets the conversation so far and answers independently, and the chair's verdict is what lands
back in the thread. Switch back and the next single model picks up from that verdict. You pay
11 requests only on the turns you choose to.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
import webbrowser

from . import board, budget, catalogue, codebase, config, redact, seats, usage
from .transport import OfflineTransport, OpenRouterTransport

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_CACHE: dict = {}


def _models(refresh: bool = False) -> list[dict]:
    if refresh or "models" not in _CACHE:
        c = catalogue.load(live=True)
        _CACHE["models"] = c["models"]
        _CACHE["origin"] = c["origin"]
        _CACHE["captured"] = c["captured"]
    return _CACHE["models"]


def _transport(offline: bool):
    key, _ = config.api_key()
    if offline or not key:
        return OfflineTransport(), False
    return OpenRouterTransport(key, app_title="freeboard"), True


def _state() -> dict:
    key, where = config.api_key()
    models = _models()
    st = usage.status()
    saved = config.board() or []
    seatable = catalogue.deliberative(models)
    return {
        "key_set": bool(key), "key_masked": config.mask(key), "key_from": where,
        "catalogue": {"origin": _CACHE.get("origin"), "captured": _CACHE.get("captured"),
                      "free": len(models), "seatable": len(seatable),
                      "families": sorted({m["family"] for m in seatable})},
        "models": [{**m, "json": catalogue.speaks_json(m),
                    "seatable": any(x["id"] == m["id"] for x in seatable)} for m in models],
        "board": saved,
        "usage": {"calls": st.calls, "failed": st.failed, "allowance": st.allowance,
                  "remaining": st.remaining, "measured": st.measured,
                  "resets_in": st.resets_in, "qualified": st.qualified,
                  "per_model": st.per_model},
        "limits": {"rpm": budget.RPM, "free_day": budget.RPD_WITHOUT_CREDITS,
                   "paid_day": budget.RPD_WITH_CREDITS,
                   "threshold": budget.CREDIT_THRESHOLD_USD},
    }


def _code_message(payload: dict, model: dict | None) -> str | None:
    """Turn a folder into the message text, or None if no folder was named.

    The seam has already run over every file at scan time. A tree with findings is refused
    unless the caller has explicitly ticked `send_anyway` FOR THIS SEND -- test fixtures and
    example keys are common enough in real repos that a flat refusal would make the feature
    unusable, but the override has to be a deliberate act each time, never a saved setting.
    """
    path = (payload.get("code_path") or "").strip()
    if not path:
        return None
    sc = codebase.scan(path)
    if sc.findings and not payload.get("send_anyway"):
        raise redact.Refused([redact.Finding("code scan", f"{r}", w.split(": ")[-1])
                              for r, w in sc.findings[:12]])
    # leave the model room to answer: never fill more than two thirds of its window
    budget_tokens = None
    if model and model.get("context_length"):
        budget_tokens = int(model["context_length"] * 0.66)
    return codebase.audit_message(sc, budget_tokens, ask=payload.get("ask", ""))


def _single(payload: dict) -> dict:
    """One model, one request. The everyday turn."""
    models = _models()
    mid = payload.get("model")
    model = next((m for m in models if m["id"] == mid), None)
    if not model:
        return {"error": f"{mid} is not on today's free list -- it may have expired."}
    transport, live = _transport(payload.get("offline", False))
    msgs = list(payload.get("messages") or [])
    code = _code_message(payload, model)
    redact.check("\n".join(m.get("content", "") for m in msgs))
    if code:
        msgs = msgs[:-1] + [{"role": "user", "content": code}] if msgs else \
               [{"role": "user", "content": code}]
    r = transport.ask(model, msgs)
    if not r.ok:
        return {"mode": "single", "model": mid, "failed": True, "reason": r.reason, "calls": 1}
    return {"mode": "single", "model": mid, "text": r.text, "calls": 1, "live": live}


def _board(payload: dict) -> dict:
    """The whole board, on this turn only, carrying the conversation so far."""
    models = _models()
    want = payload.get("board") or config.board() or []
    by_id = {m["id"]: m for m in models}
    members = [by_id[i] for i in want if i in by_id]
    if not members:
        members = seats.seat(models, size=int(payload.get("size", 5)))
    try:
        seats.quorum(members, int(payload.get("minimum", 3)))
    except seats.NoQuorum as e:
        return {"error": str(e)}

    transport, live = _transport(payload.get("offline", False))
    history = list(payload.get("messages") or [])
    redact.check("\n".join(m.get("content", "") for m in history))
    question = history[-1].get("content", "") if history else ""
    prior = history[:-1]
    # the smallest window on the board decides how much code every member can be given,
    # so each of them reads the SAME tree -- otherwise they are not auditing the same thing
    smallest = min((m.get("context_length") or 0) for m in members) or None
    code = _code_message(payload, {"context_length": smallest} if smallest else None)
    if code:
        question = code

    ordered = members + [m for m in models if m["id"] not in {x["id"] for x in members}]
    s = board.ask_in_context(question, prior=prior, transport=transport, models=ordered,
                             size=len(members), minimum=int(payload.get("minimum", 3)),
                             peer_review=bool(payload.get("peer_review", True)),
                             kind=payload.get("kind", "decide"))
    return {
        "mode": "board", "live": live, "kind": s.kind,
        "members": [m["id"] for m in members],
        "chair": s.chair_model["id"],
        "answers": [{"label": next((k for k, v in s.labels.items() if v == a.model), "?"),
                     "model": a.model, "text": a.text} for a in s.answers],
        "failures": [{"model": f.model, "reason": f.reason} for f in s.failures],
        "rankings": len(s.rankings),
        "decision": s.decision, "no_quorum": s.no_quorum, "calls": s.requests,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass                                # the console is the UI; the terminal stays quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/projects"):
            return self._json({"projects": codebase.suggest()})
        if self.path.startswith("/api/state"):
            if "refresh=1" in self.path:
                _models(refresh=True)
            return self._json(_state())
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        try:
            if self.path == "/api/key":
                raw = payload.get("key", "")
                try:
                    k = config.check_key(raw)
                except config.BadKey as e:
                    return self._json({"bad_key": str(e), **_state()})   # stored key untouched
                ok, why, acct = config.verify(k)
                if not ok and not payload.get("save_unverified"):
                    # Not saved, and the reason is OpenRouter's, not ours. Offer to save
                    # anyway, because "could not reach OpenRouter" must not block a good key.
                    return self._json({"bad_key": why, "warn": config.looks_unusual(k),
                                       "offer_save": True, **_state()})
                config.set_api_key(k)
                st = _state()
                st["verified"] = ok
                st["why"] = why
                st["account"] = acct
                return self._json(st)
            if self.path == "/api/tier":
                config.set_tier(float(payload.get("usd", 0)))
                return self._json(_state())
            if self.path == "/api/board":
                config.set_board(list(payload.get("board") or []))
                return self._json(_state())
            if self.path == "/api/scan":
                sc = codebase.scan(payload["path"])
                return self._json(sc.summary())
            if self.path == "/api/guess":
                return self._json({"task": board.looks_like_a_task(payload.get("q", ""))})
            if self.path == "/api/chat":
                out = _board(payload) if payload.get("mode") == "board" else _single(payload)
                out["usage"] = _state()["usage"]
                return self._json(out)
        except redact.Refused as e:
            # The seam fired. Nothing was sent; say exactly what stopped it.
            return self._json({"refused": [str(f) for f in e.findings]}, 200)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"error": "unknown endpoint"}, 404)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = 8420, open_browser: bool = True) -> None:
    _models()
    url = f"http://127.0.0.1:{port}/"
    with _Server(("127.0.0.1", port), Handler) as httpd:
        print(f"\n  freeboard console -> {url}")
        print("  local only: 127.0.0.1, your key stays on this machine.")
        print("  ctrl-c to stop.\n")
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped.\n")
