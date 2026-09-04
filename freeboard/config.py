"""Where your key and your chosen board live.

The key is a secret on your disk, so this does the boring things properly: the directory and
the file are created 0700/0600 before anything is written into them, the key is never printed
back to you in full, and it is never written anywhere near the repo.

Precedence is deliberate: the environment beats the file. That way a CI run or a one-off shell
can override without touching what you saved, and you can always tell which one is in play
because `status` says so.
"""
from __future__ import annotations

import json
import os

HOME = os.path.expanduser(os.environ.get("FREEBOARD_HOME", "~/.freeboard"))
CONFIG = os.path.join(HOME, "config.json")
ENV_KEY = "OPENROUTER_API_KEY"


def _ensure_home() -> None:
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    try:
        os.chmod(HOME, 0o700)
    except OSError:
        pass


def load() -> dict:
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> str:
    _ensure_home()
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)
    return CONFIG


def api_key() -> tuple[str | None, str]:
    """(key, where it came from). The environment wins, and status says which."""
    env = os.environ.get(ENV_KEY)
    if env:
        return env, f"${ENV_KEY}"
    key = load().get("api_key")
    return (key, CONFIG) if key else (None, "not set")


class BadKey(ValueError):
    """What was offered is not an API key."""


def check_key(key: str) -> str:
    """Refuse only what CANNOT be a key. OpenRouter decides the rest.

    The first version of this refused anything not starting with `sk-or-` and under 40
    characters. Both numbers were inferred from a single example -- OpenRouter documents no key
    format anywhere -- and a guess written as a rule then rejected a real key. A shape check
    can only ever be a guess about someone else's format; the service that issues the key is
    the only thing that knows, so `verify` asks it.

    What survives here is what no key can be: nothing, or something with whitespace in it. A
    sentence pasted from a chat window has spaces. A key does not.
    """
    k = (key or "").strip()
    if not k:
        raise BadKey("nothing was entered")
    if any(c.isspace() for c in k):
        raise BadKey("this has spaces in it, so it cannot be a key - it looks like pasted text")
    return k


def looks_unusual(key: str) -> str:
    """A WARNING, never a refusal. Says what is odd without pretending to know the format."""
    k = (key or "").strip()
    if k.startswith(("sk-ant-", "sk-proj-", "ghp_", "AIza", "xox")):
        return "that looks like a key for a different service - it will not work here"
    if not k.startswith("sk-or-"):
        return "OpenRouter keys usually start with `sk-or-`, and this does not"
    if len(k) < 40:
        return f"that is short for a key ({len(k)} characters) - check nothing was cut off"
    return ""


def verify(key: str, timeout: float = 15.0) -> tuple[bool, str, dict]:
    """Ask OpenRouter whether this key works. The only real validation there is.

    Returns (accepted, what to tell the user, whatever the service said about the account).
    A network failure is NOT a rejection -- it is unknown, and saying "bad key" when the wifi
    is down would send someone off to regenerate a perfectly good key.
    """
    import json as _json
    import urllib.error
    import urllib.request
    req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                 headers={"Authorization": f"Bearer {key.strip()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = (_json.load(r) or {}).get("data") or {}
        return True, "OpenRouter accepted this key", data
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "OpenRouter rejected this key - check it was copied whole", {}
        return False, f"OpenRouter answered {e.code} - the key may still be fine", {}
    except Exception as e:
        return False, f"could not reach OpenRouter ({type(e).__name__}) - key not checked", {}


def set_api_key(key: str) -> str:
    """Save a key, or raise. A key that fails the check NEVER replaces a stored one."""
    k = check_key(key)
    cfg = load()
    cfg["api_key"] = k
    return save(cfg)


def forget_api_key() -> None:
    cfg = load()
    cfg.pop("api_key", None)
    save(cfg)


def mask(key: str | None) -> str:
    """Enough to recognise it, never enough to use it."""
    if not key:
        return "none"
    return f"{key[:8]}...{key[-4:]}" if len(key) > 16 else key[:4] + "..."


def board(name: str = "default") -> list[str] | None:
    return (load().get("boards") or {}).get(name)


def set_board(members: list[str], name: str = "default") -> str:
    cfg = load()
    cfg.setdefault("boards", {})[name] = members
    return save(cfg)


def tier() -> float:
    """Credits ever purchased, as you told us. Decides 50/day vs 1000/day."""
    return float(load().get("credits_purchased_usd", 0.0))


def set_tier(usd: float) -> str:
    cfg = load()
    cfg["credits_purchased_usd"] = float(usd)
    return save(cfg)
