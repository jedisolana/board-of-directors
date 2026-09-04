"""Keeping what the board said.

Until now every thread lived in the browser tab and died with it. A board session costs nine
to eleven of a fifty-a-day allowance and produces the one thing a single model cannot -- a
position that survived being argued with -- and it was being thrown away on reload. That is
the difference between a demo and a tool.

WHAT IS STORED IS THE WHOLE PROCEEDING, not the conclusion. Each board turn keeps every
member's answer, who failed and why, who chaired, and the verdict. A stored session that kept
only the chair's text would reopen looking unanimous, which is exactly the dishonesty the
board exists to prevent: you must still be able to see that two members were rate limited and
that the vote was 3-1, a week later.

Sessions are files in the project home, 0600, on this machine only. They contain your
questions and your code, so they are treated like the key: never sent anywhere, never logged.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from . import config

DIR = os.path.join(config.HOME, "sessions")
MAX_SESSIONS = 200


def _dir() -> str:
    config._ensure_home()
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    return DIR


def _path(sid: str) -> str:
    # a session id is ours, but it still reaches this from an HTTP request
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")[:64]
    if not safe:
        raise ValueError("bad session id")
    return os.path.join(_dir(), safe + ".json")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def title_for(turns: list[dict]) -> str:
    """The first thing asked, trimmed. A list of 'Untitled' is a list of nothing."""
    for t in turns:
        if t.get("role") == "user" and (t.get("content") or "").strip():
            line = " ".join((t["content"] or "").split())
            return line[:70] + ("..." if len(line) > 70 else "")
    return "empty session"


def save(sid: str, turns: list[dict], meta: dict | None = None) -> str:
    """Write a session. Atomic, so a crash mid-write cannot leave a half file behind."""
    p = _path(sid)
    existing = load(sid) or {}
    doc = {
        "id": sid,
        "title": title_for(turns),
        "created": existing.get("created") or time.time(),
        "updated": time.time(),
        "turns": turns,
        "meta": {**(existing.get("meta") or {}), **(meta or {})},
    }
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, p)
    _prune()
    return p


def load(sid: str) -> dict | None:
    try:
        with open(_path(sid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete(sid: str) -> bool:
    try:
        os.remove(_path(sid))
        return True
    except OSError:
        return False


def listing(limit: int = 50) -> list[dict]:
    """Newest first, without reading every turn of every session into memory."""
    out = []
    try:
        names = os.listdir(_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_dir(), name)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        board_turns = sum(1 for t in d.get("turns", []) if t.get("board"))
        out.append({"id": d.get("id", name[:-5]), "title": d.get("title", "?"),
                    "updated": d.get("updated", 0), "turns": len(d.get("turns", [])),
                    "board_turns": board_turns,
                    "requests": (d.get("meta") or {}).get("requests", 0)})
    out.sort(key=lambda r: -r["updated"])
    return out[:limit]


def _prune() -> None:
    """Oldest out beyond the cap. A counter, not an archive -- same rule as the ledger."""
    rows = listing(limit=10_000)
    for r in rows[MAX_SESSIONS:]:
        delete(r["id"])


# ----------------------------------------------------------------- export

def as_markdown(doc: dict) -> str:
    """A session as something you can paste into a PR, an issue, or a decision log.

    The proceedings, not just the verdict: every member named, the ones that did not answer
    named too. A board's output is only worth keeping if the disagreement comes with it.
    """
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(doc.get("created", time.time())))
    out = [f"# {doc.get('title', 'Board session')}", "",
           f"_Board of Directors · {when}_", ""]
    for t in doc.get("turns", []):
        if t.get("role") == "user":
            out += ["## " + " ".join((t.get("content") or "").split()), ""]
            continue
        b = t.get("board")
        if not b:
            out += [f"**{t.get('model', 'model')}**", "", (t.get("content") or "").strip(), ""]
            continue
        out += [f"**Board · {b.get('kind', 'decide')} · {len(b.get('answers', []))} answered "
                f"· chaired by {b.get('chair', '?')} · {b.get('calls', 0)} requests**", ""]
        for a in b.get("answers", []):
            out += [f"### {a.get('label', '?')} — {a.get('model', '?')}", "",
                    (a.get("text") or "").strip(), ""]
        if b.get("failures"):
            out += ["### Did not answer — not counted as agreement", ""]
            out += [f"- **{f.get('model')}** — {f.get('reason')}" for f in b["failures"]]
            out += [""]
        if b.get("no_quorum"):
            out += ["### No quorum", "", b["no_quorum"], ""]
        elif b.get("decision"):
            out += ["### Decision (chair)", "", (b["decision"] or "").strip(), ""]
    return "\n".join(out).rstrip() + "\n"
