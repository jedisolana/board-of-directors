"""The outbound seam: what must never leave the machine.

A board sends your question to models run by other people. That is the whole point, and it
is also the whole risk -- a question pasted out of a terminal carries whatever was on the
terminal. So the seam refuses by default and says exactly what it caught.

REFUSE, do not scrub. A scrubber that quietly rewrites your prompt is worse than a wall: you
never learn that you nearly sent a key, and the redacted stub goes out anyway looking fine.
The rule here is that a hit stops the send and names the finding; removing it is your call,
made with your eyes open.

This is a seam, not a guarantee. It catches shapes -- key prefixes, private address ranges,
paths that only exist on your box. It cannot catch a secret that looks like a sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Each rule is (name, pattern, what it means). Ordered most-specific first so the message
# a user sees names the real thing rather than a generic "looks like a token".
RULES: list[tuple[str, re.Pattern, str]] = [
    ("openrouter key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}"), "an OpenRouter API key"),
    ("openai key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "an OpenAI-style API key"),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "an Anthropic API key"),
    ("aws key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "an AWS access key id"),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "a GitHub token"),
    ("slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    ("google key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "a Google API key"),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "a signed token"),
    ("bearer header", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"), "an Authorization header"),
    ("assignment", re.compile(
        r"(?i)\b[A-Z0-9_]*(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL)[A-Z0-9_]*\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
     "a secret assigned to a variable"),
    ("tailscale address", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[0-1]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
     "a private Tailscale address"),
    ("private address", re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"), "a private network address"),
    ("ssh path", re.compile(r"(?:^|[\s\"'])(?:~|/(?:home|Users)/[^/\s]+)/\.ssh/\S*"), "a path inside .ssh"),
    ("dotenv path", re.compile(r"(?:^|[\s\"'/])\.env(?:\.[A-Za-z0-9_-]+)?\b"), "a .env file"),
    ("home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"), "an absolute path inside a home directory"),
]


@dataclass(frozen=True)
class Finding:
    rule: str
    means: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.means} ({self.rule}): {self.excerpt}"


class Refused(Exception):
    """The seam stopped a send. `.findings` says why."""

    def __init__(self, findings: list[Finding]):
        self.findings = findings
        super().__init__("refused to send: " + "; ".join(str(f) for f in findings))


def _mask(s: str, keep: int = 4) -> str:
    s = s.strip()
    if len(s) <= keep * 2:
        return s[:keep] + "..."
    return f"{s[:keep]}...{s[-keep:]}"


def scan(text: str) -> list[Finding]:
    """Every rule that fires, with a masked excerpt -- the report never repeats the secret."""
    out, seen = [], set()
    for name, pat, means in RULES:
        for m in pat.finditer(text or ""):
            key = (name, m.group(0))
            if key in seen:
                continue
            seen.add(key)
            out.append(Finding(name, means, _mask(m.group(0))))
    return out


def check(text: str) -> str:
    """Return the text, or raise `Refused`. Everything outbound goes through here."""
    found = scan(text)
    if found:
        raise Refused(found)
    return text
