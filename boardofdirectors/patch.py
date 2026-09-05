"""Proposed changes to files, and applying them only when a person says so.

Auditing tells you what is wrong. This is the other half - the board writing the fix - and it
is the first thing here that can change your files, so it is built the way the paid tier is
built: the safe direction is doing nothing.

THE MODEL NEVER WRITES. It returns whole files in a fenced block; this parses them, diffs
them against what is on disk, and shows you. Nothing touches the filesystem until a person
clicks apply, one file at a time.

Whole files rather than unified diffs, deliberately. A model that miscounts a hunk header
produces a patch that either fails to apply or applies to the wrong lines, and the second is
much worse than the first. A whole file is either parseable or it is not.

Every guard below exists because the failure it prevents is silent:

  * a path that escapes the folder      resolved and checked, so `../../.ssh/config` cannot be
                                        written no matter what the model returns
  * a file that was not in the scan     the board never saw it, so it cannot be proposing an
                                        informed change to it
  * a file that changed since the scan  refused: the model reasoned about text that is no
                                        longer there
  * no backup                           the previous contents are kept before every write
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from dataclasses import dataclass, field

from . import atomic, codebase

FENCE = re.compile(r"^-{3,}\s*(?P<path>[^\n]+?)\s*-{3,}\s*$", re.M)

WRITE_PROMPT = """You are changing a real codebase. Below is the source.

TASK:
{task}

Return ONLY the files you are changing, each in full, in exactly this form:

----- relative/path/to/file.py -----
<the complete new contents of that file>

Rules:
- Give the WHOLE file, not a fragment and not a diff. It replaces what is there.
- Only files that appear in the source below. Do not invent new paths.
- Change as little as possible. Every line you alter is a line someone has to review.
- If the task cannot be done in the files shown, say so in one line and return no files.

{code}"""


@dataclass
class Change:
    rel: str
    path: str
    old: str = field(repr=False, default="")
    new: str = field(repr=False, default="")
    reason: str = ""

    @property
    def added(self) -> int:
        return sum(1 for line in self.diff_lines() if line.startswith("+") and not line.startswith("+++"))

    @property
    def removed(self) -> int:
        return sum(1 for line in self.diff_lines() if line.startswith("-") and not line.startswith("---"))

    def diff_lines(self) -> list[str]:
        return list(difflib.unified_diff(self.old.splitlines(), self.new.splitlines(),
                                         fromfile=self.rel, tofile=self.rel, lineterm="", n=3))

    def diff(self) -> str:
        return "\n".join(self.diff_lines())


class Rejected(Exception):
    """A proposed change that must not be applied, and why."""


def parse(text: str, root: str, allowed: set[str]) -> tuple[list[Change], list[str]]:
    """Pull whole files out of a model's answer. Returns (changes, complaints)."""
    root = os.path.abspath(os.path.expanduser(root))
    out: list[Change] = []
    notes: list[str] = []
    marks = list(FENCE.finditer(text or ""))
    for i, m in enumerate(marks):
        rel = m.group("path").strip().strip("`")
        # `.lstrip("./")` strips a SET of characters, not a prefix: it turned
        # "../../.ssh/config" into "ssh/config" - quietly rewriting a traversal attempt into
        # a plausible relative path. Here the allowlist caught it anyway, but only because no
        # file called ssh/config had been scanned. Strip one prefix, and refuse traversal
        # outright rather than normalising it into something that might pass.
        if rel.startswith("./"):
            rel = rel[2:]
        body = text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))]
        body = _unfence(body).rstrip() + "\n"
        if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
            notes.append(f"{rel}: path escapes the folder — refused")
            continue
        full = os.path.abspath(os.path.join(root, rel))
        if os.path.commonpath([full, root]) != root:
            notes.append(f"{rel}: outside the folder — refused")
            continue
        # Belt and braces on the symlink case. The scanner already refuses to show the board
        # a link that leaves the folder, so one should never reach the allowlist - but the
        # allowlist is data, and the check that matters is the one standing next to the write.
        if os.path.islink(full) or not codebase.inside(root, full):
            notes.append(f"{rel}: resolves outside the folder — refused")
            continue
        if rel not in allowed:
            notes.append(f"{rel}: was not in the code the board was shown — refused")
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                old = fh.read()
        except OSError:
            notes.append(f"{rel}: cannot be read — refused")
            continue
        if old == body:
            notes.append(f"{rel}: unchanged")
            continue
        out.append(Change(rel=rel, path=full, old=old, new=body))
    return out, notes


def _unfence(body: str) -> str:
    """Models wrap the contents in ``` about half the time. Take it off if it is there."""
    lines = body.strip("\n").split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def apply(change: Change, expect_digest: str | None = None, backup_dir: str | None = None) -> str:
    """Write one file. Raises `Rejected` rather than guessing.

    `expect_digest` is the hash of the contents the board actually reasoned about. If the file
    has moved on since - another edit, a git checkout, a formatter - the proposal was written
    against text that is no longer there, and applying it would silently discard whatever
    happened in between.
    """
    try:
        with open(change.path, encoding="utf-8") as fh:
            now = fh.read()
    except OSError as e:
        raise Rejected(f"{change.rel} cannot be read: {e}") from e
    if expect_digest and digest(now) != expect_digest:
        raise Rejected(f"{change.rel} has changed on disk since the board read it — "
                       "re-run the task rather than applying a proposal written against "
                       "text that is no longer there")
    if backup_dir:
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = change.rel.replace(os.sep, "__")
        atomic.write(os.path.join(backup_dir, f"{stamp}.{safe}"), now, mode=0o600)
    # The user's own source file, so the same rule: a unique scratch name and an fsync
    # before the rename. A half-written file here is somebody's code.
    atomic.write(change.path, change.new, mode=0o644)
    return change.path
