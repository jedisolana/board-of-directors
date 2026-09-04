"""Where your key and your chosen board live.

The key is a secret on your disk, so this does the boring things properly: the directory and
the file are created 0700/0600 before anything is written into them, the key is never printed
back to you in full, and it is never written anywhere near the repo.

Precedence is deliberate: the environment beats the file. That way a CI run or a one-off shell
can override without touching what you saved, and you can always tell which one is in play
because `status` says so.
"""
from __future__ import annotations

import contextlib
import json
import os

DEFAULT_HOME = os.path.expanduser("~/.board-of-directors")
HOME = os.path.expanduser(os.environ.get("BOARD_HOME", DEFAULT_HOME))
CONFIG = os.path.join(HOME, "config.json")
ENV_KEY = "OPENROUTER_API_KEY"

# Where this lived before the project was named. A rename must not cost someone their key and
# their day's call count -- they did nothing wrong, and "it forgot everything" is the worst
# possible first impression of a new version.
#
# The literal is split so that a project-wide find-and-replace on the old name cannot rewrite
# it. It already did once: the rename pass turned this into the NEW path, the guard against
# migrating a directory onto itself then matched, and the migration silently became a no-op --
# a rename that quietly ate the key it was written to preserve.
LEGACY_HOME = os.path.expanduser("~/." + "freeboard")


def _migrate() -> None:
    """Move a previous install's files across, once, without overwriting anything newer."""
    # ONLY into the default home. Setting BOARD_HOME is a request for that exact directory --
    # a test fixture, a second account, a sandbox - and importing a previous install's key and
    # call count into it is not a migration, it is contamination. The counter tests caught
    # this immediately by starting with someone else's calls already on the clock.
    if os.path.abspath(HOME) != os.path.abspath(DEFAULT_HOME):
        return
    if os.path.abspath(HOME) == os.path.abspath(LEGACY_HOME) or not os.path.isdir(LEGACY_HOME):
        return
    try:
        os.makedirs(HOME, mode=0o700, exist_ok=True)
        for name in ("config.json", "usage.json"):
            old, new = os.path.join(LEGACY_HOME, name), os.path.join(HOME, name)
            if os.path.exists(old) and not os.path.exists(new):
                with open(old) as a, open(os.open(new, os.O_WRONLY | os.O_CREAT, 0o600), "w") as b:
                    b.write(a.read())
    except OSError:
        pass


def _ensure_home() -> None:
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    _migrate()
    with contextlib.suppress(OSError):
        os.chmod(HOME, 0o700)


def load() -> dict:
    _migrate()
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


def unusable() -> dict:
    """Models the catalogue calls free that the API then refuses.

    `thinkingmachines/inkling-small:free` is listed free, priced zero, and answers a real
    request with 403 "only available on agentic harnesses. Try plugging it into a coding
    agent or productivity app". The catalogue cannot be trusted about usability, only about
    price - so usability is learned from what actually happened and remembered, otherwise
    the same model is chosen as chair on every single run.
    """
    return load().get("unusable") or {}


def mark_unusable(model_id: str, why: str) -> None:
    cfg = load()
    cfg.setdefault("unusable", {})[model_id] = {"why": why, "at": __import__("time").time()}
    save(cfg)


def forget_unusable(model_id: str | None = None) -> None:
    """A gate can be lifted; nothing here is permanent."""
    cfg = load()
    if model_id is None:
        cfg.pop("unusable", None)
    else:
        (cfg.get("unusable") or {}).pop(model_id, None)
    save(cfg)


def tier() -> float:
    """Credits ever purchased. Decides 50/day vs 1000/day.

    Prefers what OPENROUTER said over what the user said. `is_free_tier` from GET
    /api/v1/key is the same fact the limit turns on, and the account knows it while the
    person often does not.
    """
    cfg = load()
    measured = cfg.get("is_free_tier")
    if measured is not None:
        return 0.0 if measured else float(CREDIT_THRESHOLD)
    return float(cfg.get("credits_purchased_usd", 0.0))


def tier_source() -> str:
    return "OpenRouter" if load().get("is_free_tier") is not None else "you told us"


def set_measured_tier(is_free_tier: bool | None) -> None:
    """Record what the account itself reports. None means it did not say."""
    if is_free_tier is None:
        return
    cfg = load()
    cfg["is_free_tier"] = bool(is_free_tier)
    save(cfg)


CREDIT_THRESHOLD = 10


def set_tier(usd: float) -> str:
    cfg = load()
    cfg["credits_purchased_usd"] = float(usd)
    return save(cfg)
