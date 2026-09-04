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


def set_api_key(key: str) -> str:
    cfg = load()
    cfg["api_key"] = key.strip()
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
