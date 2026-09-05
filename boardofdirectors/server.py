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
import time
import webbrowser

from . import (
    board,
    budget,
    catalogue,
    codebase,
    config,
    cost,
    openai_api,
    patch,
    redact,
    seats,
    sessions,
    truecount,
    usage,
)
from .transport import OfflineTransport, OpenRouterTransport

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_CACHE: dict = {}


def build_stamp() -> str:
    """A short id for the page currently on disk, so "which version are you looking at?" has
    an answer. Without one, a fixed bug and a cached copy of the bug are indistinguishable
    from either side of the screen -- which is exactly how an hour went missing."""
    import hashlib
    try:
        with open(os.path.join(WEB, "index.html"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:7]
    except OSError:
        return "unknown"


CATALOGUE_TTL = 15 * 60          # free models expire; a console left open must notice


def _slug(title: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in title]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return (out or "session")[:60]


def _models(refresh: bool = False) -> list[dict]:
    stale = time.time() - _CACHE.get("at", 0) > CATALOGUE_TTL
    if refresh or stale or "models" not in _CACHE:
        c = catalogue.load(live=True)
        _CACHE["models"] = c["models"]
        _CACHE["origin"] = c["origin"]
        _CACHE["captured"] = c["captured"]
        _CACHE["at"] = time.time()
    return _CACHE["models"]


def _transport(offline: bool):
    key, _ = config.api_key()
    if offline or not key:
        return OfflineTransport(), False
    return OpenRouterTransport(key, app_title="Board of Directors"), True


TRUE_TTL = 60          # analytics is a real request; the header polls far more often


def _true_calls() -> tuple[int | None, str]:
    mk, _ = config.management_key()
    if not mk:
        return None, "no management key set"
    now = time.time()
    if now - _CACHE.get("true_at", 0) < TRUE_TTL and "true" in _CACHE:
        return _CACHE["true"], _CACHE.get("true_why", "")
    n, why = truecount.requests_today(mk)
    _CACHE["true"], _CACHE["true_why"], _CACHE["true_at"] = n, why, now
    return n, why


CREDITS_TTL = 120


def _credits() -> dict | None:
    key, _ = config.api_key()
    if not key:
        return None
    if time.time() - _CACHE.get("cr_at", 0) < CREDITS_TTL and "cr" in _CACHE:
        return _CACHE["cr"]
    c = config.credits(key)
    _CACHE["cr"], _CACHE["cr_at"] = c, time.time()
    return c


def _state() -> dict:
    key, where = config.api_key()
    models = _models()
    true_n, true_why = _true_calls()
    st = usage.status(true_calls=true_n)
    saved = config.board() or []
    paid_on = config.allow_paid()
    tier = config.model_tier()
    seatable = catalogue.deliberative(models, tier=tier)
    return {
        "key_set": bool(key), "key_masked": config.mask(key), "key_from": where,
        "catalogue": {"origin": _CACHE.get("origin"), "captured": _CACHE.get("captured"),
                      "free": len(models), "seatable": len(seatable),
                      "families": sorted({m["family"] for m in seatable})},
        "allow_paid": paid_on,
        "model_tier": tier,
        "credits": _credits(),
        "spend_cap": config.spend_cap(),
        "locked_to_free": cost.locked_to_free(config.spend_cap()),
        "models": [{**m, "json": catalogue.speaks_json(m),
                    "seatable": any(x["id"] == m["id"] for x in seatable)}
                   for m in models
                   if (m.get("free") and tier != "paid")
                   or (not m.get("free") and paid_on)],
        "board": saved,
        "tier_source": config.tier_source(),
        "build": build_stamp(),
        "management_key_set": bool(config.management_key()[0]),
        "usage": {"calls": st.calls, "since_reset": st.since_reset,
                  "source": st.source, "source_why": true_why,
                  "failed": st.failed, "provider_busy": st.provider_busy,
                  "allowance": st.allowance,
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
        # The code goes in ALONGSIDE what was typed, never instead of it. Replacing the last
        # message silently drops the actual question whenever a folder is attached.
        msgs = [*msgs, {"role": "user", "content": code}]
    r = transport.ask(model, msgs)
    if not r.ok:
        return {"mode": "single", "model": mid, "failed": True, "reason": r.reason, "calls": 1}
    return {"mode": "single", "model": mid, "text": r.text, "calls": 1, "live": live}


def _paid_ok(payload: dict) -> bool:
    """Paid seats need BOTH the stored permission and this request saying so.

    The setting alone is not consent to spend on a particular session. A stored flag from last
    week must not be what decides that today's question costs money, so the browser has to ask
    for it every time and the server has to have been told it is allowed at all.
    """
    # A zero cap overrules everything, including the toggle. Someone who has locked spending
    # off has said so about their MONEY, not about a checkbox, and a UI state must not be
    # able to override that.
    if cost.locked_to_free(config.spend_cap()):
        return False
    return bool(config.allow_paid()) and bool(payload.get("allow_paid"))


def _tier(payload: dict) -> str:
    """The tier this send may use. Never wider than the stored setting."""
    stored = config.model_tier()
    if not _paid_ok(payload):
        return "free"
    asked = payload.get("tier")
    return asked if asked in ("free", "paid", "both") and asked == stored else stored


def _board(payload: dict, on_event=None) -> dict:
    """The whole board, on this turn only, carrying the conversation so far."""
    models = _models()
    paid_ok = _paid_ok(payload)
    want = payload.get("board") or config.board() or []
    by_id = {m["id"]: m for m in models}
    members = [by_id[i] for i in want if i in by_id]
    if not paid_ok:
        # A chosen board can contain paid models from when the permission WAS on. Dropping
        # them is right: the alternative is charging for a session the caller did not consent
        # to on this send, which is the one mistake here that costs real money.
        blocked = [m["id"] for m in members if not m.get("free")]
        members = [m for m in members if m.get("free")]
        if blocked:
            # Say WHY, precisely. "Turn on paid models" is wrong and confusing when they are
            # already on and a zero cap is what actually refused.
            why = ("spending is locked off (cap $0.00) — your balance still buys the higher "
                   "free rate limit" if cost.locked_to_free(config.spend_cap())
                   else "turn on paid models to include them")
            return {"error": "paid seats not allowed on this send: "
                             + ", ".join(blocked) + f". {why}."}
    if not members:
        members = seats.seat(models, size=int(payload.get("size", 5)),
                             tier=_tier(payload))
    try:
        seats.quorum(members, int(payload.get("minimum", 3)))
    except seats.NoQuorum as e:
        return {"error": str(e)}

    # Cost is checked BEFORE anything is sent. A cap is a wall.
    try:
        chair_guess = seats.chair(models, members, tier=_tier(payload))
    except seats.NoQuorum:
        chair_guess = None
    try:
        est = cost.session(members, chair_guess,
                           peer_review=bool(payload.get("peer_review", True)))
    except cost.Unpriced as e:
        return {"error": f"cannot price this board: {e}"}
    cap = config.spend_cap()
    if cost.over_cap(est, cap):
        if cost.locked_to_free(cap):
            return {"error": "spending is locked off — this board would cost "
                             f"{est.human()}. Your balance still buys the higher free "
                             "rate limit; unlock spending only if you want to use it."}
        return {"error": f"this session would cost {est.human()}, over your "
                         f"${cap:,.2f} cap. Raise the cap or seat cheaper models."}

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

    s = board.ask_in_context(question, prior=prior, transport=transport, models=models,
                             members=members, minimum=int(payload.get("minimum", 3)),
                             peer_review=bool(payload.get("peer_review", True)),
                             kind=payload.get("kind", "decide"), on_event=on_event,
                             allow_paid=paid_ok, tier=_tier(payload))
    return {
        "mode": "board", "live": live, "kind": s.kind, "estimated_usd": est.usd,
        # the members the SESSION used, not the ones that were requested
        "members": [m["id"] for m in s.members],
        "chair": s.chair_model["id"],
        "chair_failures": s.chair_failures,
        "tally": s.tally,
        "answers": [{"label": next((k for k, v in s.labels.items() if v == a.model), "?"),
                     "model": a.model,
                     "vote": board.read_vote(a.text),
                     "text": board.strip_vote(a.text)} for a in s.answers],
        "failures": [{"model": f.model, "reason": f.reason} for f in s.failures],
        "rankings": len(s.rankings),
        "decision": s.decision, "no_quorum": s.no_quorum, "calls": s.requests,
    }


def _stream_board(handler, payload: dict) -> None:
    """Push each event down the wire the moment it happens.

    Newline-delimited JSON rather than Server-Sent Events: the browser has to POST a whole
    conversation to start this, and EventSource cannot POST. A plain chunked response read
    with a stream reader does the same job without the workaround.
    """
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    def push(ev):
        try:
            handler.wfile.write((json.dumps(ev) + "\n").encode())
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise                       # the tab closed; stop the session with it

    try:
        out = _board(payload, on_event=push)
    except redact.Refused as e:
        return push({"type": "refused", "findings": [str(f) for f in e.findings]})
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception as e:
        return push({"type": "error", "error": f"{type(e).__name__}: {e}"})
    if out.get("error"):
        return push({"type": "error", "error": out["error"]})
    push({"type": "done", **out, "usage": _state()["usage"]})


# Loopback keeps the NETWORK out. It does not keep out the browser, and saying "no auth is
# fine, it is only on localhost" skips over two ways a web page you merely visit can reach a
# local server:
#
#   CSRF            a page can POST to 127.0.0.1 from your browser. It cannot READ the reply
#                   without CORS, and none is sent - but a fire-and-forget POST is enough to
#                   spend your balance or write a file, and not reading the answer costs the
#                   attacker nothing.
#   DNS rebinding   a hostname the attacker controls, re-pointed at 127.0.0.1, makes their
#                   page same-origin with this server and defeats an Origin check on its own.
#
# Both are cheap to close and standard for a local server. What neither closes is another
# PROGRAM on this machine running as you - that needs a token, and is the honest limit.
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}


def _host_ok(header: str | None) -> bool:
    """The Host must be a loopback name. Blocks DNS rebinding, which Origin alone cannot.

    IPv6 is bracketed, so counting colons to find a port does not work: "[::1]:8420" has
    three. Take what is inside the brackets when they are there, and split a port off only
    when exactly one colon remains.
    """
    if not header:
        return True                      # HTTP/1.0 and some tools send none
    h = header.strip()
    if h.startswith("["):
        end = h.find("]")
        host = h[1:end] if end > 0 else ""      # unterminated bracket -> refuse
    else:
        host = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return host.lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _origin_ok(header: str | None, port: int) -> bool:
    """No Origin means it is not a browser. A cross-site Origin means it is, and it is not us."""
    if not header or header == "null":
        return True
    from urllib.parse import urlparse
    u = urlparse(header)
    return u.hostname in {"localhost", "127.0.0.1", "::1"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass                                # the console is the UI; the terminal stays quiet

    def _no_cache(self):
        """This page changes every few minutes. A browser holding yesterday's copy makes a
        fixed bug look unfixed -- the user reloads, sees the same failure, and reports it
        again. `Last-Modified` alone is not enough: with no Cache-Control, browsers apply
        heuristic caching and may serve from memory without ever revalidating."""
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def end_headers(self):
        self._no_cache()
        super().end_headers()

    def _guard(self) -> bool:
        """Refuse anything a web page could have sent. Returns False if it was refused."""
        if not _host_ok(self.headers.get("Host")):
            self._json({"error": "refused: this server only answers to localhost. A request "
                                 "arriving under another hostname is a rebinding attempt."}, 403)
            return False
        if not _origin_ok(self.headers.get("Origin"), self.server.server_address[1]):
            self._json({"error": "refused: cross-site request. A page you are visiting tried "
                                 "to use your local Board of Directors."}, 403)
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._guard():
            return
        if self.path.startswith("/v1/models"):
            return self._json(openai_api.model_list(_models(), config.model_tier()))
        if self.path.startswith("/api/sessions"):
            return self._json({"sessions": sessions.listing()})
        if self.path.startswith("/api/projects"):
            return self._json({"projects": codebase.suggest()})
        if self.path.startswith("/api/state"):
            if "refresh=1" in self.path:
                _models(refresh=True)
            return self._json(_state())
        return super().do_GET()

    def do_POST(self):
        if not self._guard():
            return
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
                # the account's own answer beats the user's guess about their own account
                config.set_measured_tier(acct.get("is_free_tier"))
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
            if self.path == "/api/session/save":
                sid = payload.get("id") or sessions.new_id()
                sessions.save(sid, payload.get("turns") or [], payload.get("meta") or {})
                return self._json({"id": sid, "sessions": sessions.listing()})
            if self.path == "/api/session/load":
                doc = sessions.load(payload.get("id", ""))
                return self._json(doc or {"error": "no such session"})
            if self.path == "/api/session/delete":
                sessions.delete(payload.get("id", ""))
                return self._json({"sessions": sessions.listing()})
            if self.path == "/api/session/export":
                doc = sessions.load(payload.get("id", ""))
                if not doc:
                    return self._json({"error": "no such session"})
                return self._json({"markdown": sessions.as_markdown(doc),
                                   "filename": _slug(doc.get("title", "session")) + ".md"})
            if self.path == "/api/paid":
                if "tier" in payload:
                    config.set_model_tier(payload["tier"])
                elif "on" in payload:
                    config.set_model_tier("both" if payload["on"] else "free")
                if "cap" in payload:
                    config.set_spend_cap(float(payload["cap"]))
                _CACHE.pop("models", None)          # the seatable set just changed
                _CACHE.pop("cr_at", None)           # and re-read the balance
                return self._json(_state())
            if self.path == "/api/estimate":
                mods = _models()
                by_id = {m["id"]: m for m in mods}
                ms = [by_id[i] for i in (payload.get("board") or []) if i in by_id]
                if not ms:
                    return self._json({"usd": 0.0, "human": "free", "paid_members": 0})
                try:
                    ch = seats.chair(mods, ms, allow_paid=config.allow_paid())
                except seats.NoQuorum:
                    ch = None
                try:
                    e = cost.session(ms, ch, peer_review=bool(payload.get("peer_review", True)))
                except cost.Unpriced as ex:
                    return self._json({"error": str(ex)})
                return self._json({"usd": e.usd, "human": e.human(),
                                   "paid_members": e.paid_members,
                                   "per_model": list(e.per_model),
                                   "cap": config.spend_cap(),
                                   "over_cap": cost.over_cap(e, config.spend_cap())})
            if self.path == "/api/mgmt_key":
                raw = payload.get("key", "")
                if not raw.strip():
                    config.forget_management_key()
                    _CACHE.pop("true", None)
                    _CACHE.pop("true_at", None)
                    return self._json(_state())
                try:
                    config.set_management_key(raw)
                except config.BadKey as e:
                    return self._json({"bad_key": str(e), **_state()})
                _CACHE.pop("true_at", None)
                n, why = _true_calls()
                st = _state()
                st["checked"] = why
                st["true_calls"] = n
                return self._json(st)
            if self.path == "/api/usage/reset":
                was = usage.reset_today()
                return self._json({"discarded": was, **_state()})
            if self.path in ("/v1/chat/completions", "/v1/completions"):
                # The one dialect every LLM tool already speaks, so anything can use the board.
                mods = _models()
                want = payload.get("board") or config.board() or []
                by_id = {m["id"]: m for m in mods}
                members = [by_id[i] for i in want if i in by_id] or None
                if members and not _paid_ok(payload):
                    members = [m for m in members if m.get("free")] or None
                transport, _ = _transport(payload.get("offline", False))
                body, status = openai_api.run(
                    payload, mods, transport, members=members,
                    minimum=int(payload.get("minimum", 3)),
                    allow_paid=_paid_ok(payload), tier=_tier(payload))
                return self._json(body, status)
            if self.path == "/api/work":
                # Propose changes to a folder. NOTHING is written here - this returns diffs.
                sc = codebase.scan(payload["path"])
                if sc.findings and not payload.get("send_anyway"):
                    raise redact.Refused([redact.Finding("code scan", r, w.split(": ")[-1])
                                          for r, w in sc.findings[:12]])
                mods = _models()
                want = payload.get("board") or config.board() or []
                by_id = {m["id"]: m for m in mods}
                members = [by_id[i] for i in want if i in by_id] or seats.seat(
                    mods, size=int(payload.get("size", 5)), tier=_tier(payload))
                if not _paid_ok(payload):
                    members = [m for m in members if m.get("free")]
                smallest = min((m.get("context_length") or 0) for m in members) or None
                body = codebase.pack(sc, int(smallest * 0.6) if smallest else None)
                msg = patch.WRITE_PROMPT.format(task=payload.get("task", ""), code=body)
                transport, _ = _transport(payload.get("offline", False))
                s_ = board.ask(msg, transport=transport, models=mods, members=members,
                               minimum=int(payload.get("minimum", 3)), kind="make",
                               peer_review=bool(payload.get("peer_review", True)),
                               allow_paid=_paid_ok(payload), tier=_tier(payload))
                allowed = {f.rel for f in sc.files}
                changes, notes = patch.parse(s_.decision or "", sc.root, allowed)
                return self._json({
                    "root": sc.root, "calls": s_.requests,
                    "no_quorum": s_.no_quorum, "notes": notes,
                    "answered": [a.model for a in s_.answers],
                    "failures": [{"model": f.model, "reason": f.reason} for f in s_.failures],
                    "chair": s_.chair_model["id"],
                    "changes": [{"rel": c.rel, "diff": c.diff(), "added": c.added,
                                 "removed": c.removed, "new": c.new,
                                 "was": patch.digest(c.old)} for c in changes],
                })
            if self.path == "/api/apply":
                # The ONLY place a file is written, and only one file per call.
                root = os.path.abspath(os.path.expanduser(payload["root"]))
                ch = patch.Change(rel=payload["rel"],
                                  path=os.path.join(root, payload["rel"]),
                                  new=payload["new"])
                try:
                    patch.apply(ch, expect_digest=payload.get("was"),
                                backup_dir=os.path.join(config.HOME, "backups"))
                except (patch.Rejected, OSError) as e:
                    return self._json({"error": str(e)})
                return self._json({"applied": ch.rel,
                                   "backup": os.path.join(config.HOME, "backups")})
            if self.path == "/api/scan":
                sc = codebase.scan(payload["path"])
                return self._json(sc.summary())
            if self.path == "/api/guess":
                return self._json({"task": board.looks_like_a_task(payload.get("q", ""))})
            if self.path == "/api/chat/stream":
                return _stream_board(self, payload)
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


def _in_use(port: int) -> bool:
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve(port: int = 8420, open_browser: bool = True) -> int:
    """Start the console. Returns a shell exit code rather than raising a traceback.

    Running it twice is the single most likely mistake, and the bare OSError for it is
    `[Errno 48] Address already in use` under twelve lines of socketserver internals. That is
    a stack trace as an error message: it names the syscall that failed and not one thing the
    reader can do. It should say the console is already running, and where.
    """
    url = f"http://127.0.0.1:{port}/"
    if _in_use(port):
        print(f"\n  Something is already listening on port {port}.")
        print(f"  If it is the console, it is already running: {url}")
        print(f"  Otherwise start this one somewhere else:  board ui --port {port + 1}\n")
        return 1

    _models()
    with _Server(("127.0.0.1", port), Handler) as httpd:
        print(f"\n  Board of Directors -> {url}   (build {build_stamp()})")
        print(f"  OpenAI-compatible  -> {url}v1   (model: board, board:make, board:3)")
        print("  local only: 127.0.0.1, your key stays on this machine.")
        print("  ctrl-c to stop.\n")
        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped.\n")
    return 0
