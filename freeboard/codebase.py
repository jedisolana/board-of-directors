"""Reading a folder of code and packing it into one message.

This is a LOADER, not a harness. You choose the folder; the program reads it once and sends
one message. The model never decides what to open next. That is deliberate: on the free tier
the agentic scores -- how well a model drives itself through a task -- are the weakest numbers
these models have, while "read a lot, give one careful answer" is what they are good at. The
loader plays to the strength and costs one request instead of ten.

Three things it refuses to do:

  * send a file it has not scanned for secrets. Source trees are exactly where a stray key or
    a private address lives, and this is the feature most likely to leak one by accident.
  * guess at binaries. Anything that is not text, or is implausibly large, is skipped and
    listed as skipped, so a silent omission never looks like a clean bill of health.
  * pretend the token count is exact. It is characters over four. Good enough to decide
    whether something fits, and labelled as an estimate everywhere it is shown.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import redact

# Folders that are never your code, and would swamp the message if included.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target",
    ".next", ".nuxt", ".cache", "vendor", "Pods", ".idea", ".vscode", "site-packages",
    ".gradle", ".terraform", "coverage", ".DS_Store", "bin", "obj",
}
SOURCE = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".kt", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh", ".sql",
    ".html", ".css", ".scss", ".vue", ".svelte", ".lua", ".pl", ".r", ".m", ".scala",
    ".ex", ".exs", ".erl", ".clj", ".hs", ".ml", ".dart", ".yaml", ".yml", ".toml",
    ".json", ".md", ".txt", ".cfg", ".ini", ".env.example", ".tf", ".proto",
}
# Real code, but noise in an audit: generated, locked, or minified.
SKIP_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock", "go.sum",
              "pnpm-lock.yaml", "composer.lock", "Gemfile.lock"}
MAX_FILE_BYTES = 400_000
CHARS_PER_TOKEN = 4


@dataclass
class File:
    path: str
    rel: str
    chars: int
    text: str = field(repr=False, default="")

    @property
    def tokens(self) -> int:
        return self.chars // CHARS_PER_TOKEN


@dataclass
class Scan:
    root: str
    files: list[File]
    skipped: list[tuple[str, str]]          # (relative path, why)
    findings: list[tuple[str, str]]         # (relative path, what the seam saw)

    @property
    def chars(self) -> int:
        return sum(f.chars for f in self.files)

    @property
    def tokens(self) -> int:
        return self.chars // CHARS_PER_TOKEN

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> dict:
        return {"root": self.root, "count": len(self.files), "tokens": self.tokens,
                "chars": self.chars, "clean": self.clean,
                "files": [{"rel": f.rel, "tokens": f.tokens} for f in
                          sorted(self.files, key=lambda x: -x.chars)],
                "skipped": [{"rel": r, "why": w} for r, w in self.skipped[:60]],
                "skipped_total": len(self.skipped),
                "findings": [{"rel": r, "what": w} for r, w in self.findings]}


def _is_source(name: str) -> bool:
    if name in SKIP_NAMES or name.startswith("."):
        return name in (".env.example",)
    ext = os.path.splitext(name)[1].lower()
    return ext in SOURCE


def scan(root: str, max_files: int = 400) -> Scan:
    """Walk a folder, keep the source, and run the seam over every byte of it."""
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise NotADirectoryError(f"{root} is not a folder")
    files: list[File] = []
    skipped: list[tuple[str, str]] = []
    findings: list[tuple[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if not _is_source(name):
                skipped.append((rel, "not a source file"))
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                skipped.append((rel, "unreadable"))
                continue
            if size > MAX_FILE_BYTES:
                skipped.append((rel, f"too big ({size // 1000}kB)"))
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                skipped.append((rel, "not text"))
                continue
            for f in redact.scan(text):
                findings.append((rel, str(f)))
            files.append(File(full, rel, len(text), text))
            if len(files) >= max_files:
                skipped.append(("...", f"stopped at {max_files} files"))
                return Scan(root, files, skipped, findings)
    return Scan(root, files, skipped, findings)


def pack(scan_result: Scan, budget_tokens: int | None = None) -> str:
    """One message holding the whole tree, biggest files first if something has to go.

    If it will not fit, the files that are dropped are NAMED in the message. A model told it
    is seeing part of a codebase reasons differently from one that believes it saw all of it.
    """
    kept, dropped, used = [], [], 0
    for f in sorted(scan_result.files, key=lambda x: -x.chars):
        if budget_tokens is not None and used + f.tokens > budget_tokens:
            dropped.append(f)
            continue
        kept.append(f)
        used += f.tokens

    kept.sort(key=lambda f: f.rel)
    parts = [f"PROJECT: {os.path.basename(scan_result.root)}",
             f"{len(kept)} file(s), about {used} tokens.", ""]
    if dropped:
        parts.insert(2, "NOT INCLUDED (did not fit): "
                        + ", ".join(f.rel for f in sorted(dropped, key=lambda x: x.rel)))
    for f in kept:
        parts.append(f"----- {f.rel} -----")
        parts.append(f.text.rstrip())
        parts.append("")
    return "\n".join(parts)


AUDIT = """You are auditing a codebase. Below is the source.

Report only DEFECTS you can point at: the file, what is wrong, and the input or situation that
makes it go wrong. Rank the most serious first.

Do not summarise what the code does. Do not suggest style changes. Do not praise it. If you
find nothing you can point at, say so plainly rather than filling the space.

{code}"""


def audit_message(scan_result: Scan, budget_tokens: int | None = None, ask: str = "") -> str:
    body = pack(scan_result, budget_tokens)
    if ask.strip():
        return f"You are auditing a codebase. The question is:\n\n{ask.strip()}\n\n{body}"
    return AUDIT.format(code=body)


def suggest(limit: int = 30) -> list[dict]:
    """Folders that look like projects, to offer as buttons instead of a path to type."""
    marks = {".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
             "requirements.txt", "setup.py", "Gemfile", "pom.xml", "build.gradle"}
    out = []
    for base in (os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"),
                 os.path.expanduser("~")):
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.scandir(base), key=lambda e: -e.stat().st_mtime)
        except OSError:
            continue
        for e in entries:
            if not e.is_dir() or e.name.startswith("."):
                continue
            try:
                inside = set(os.listdir(e.path))
            except OSError:
                continue
            if inside & marks or any(n.endswith((".py", ".js", ".ts")) for n in inside):
                out.append({"name": e.name, "path": e.path,
                            "where": os.path.basename(base) or "home"})
            if len(out) >= limit:
                return out
    return out
