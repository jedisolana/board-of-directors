"""Writing a file so that a reader never sees half of one, and two writers never lose one.

Every store here used the same pattern: write `x.tmp`, rename over `x`. The rename is atomic,
which is the point - but the TEMP NAME WAS FIXED, so two writers shared it. One renamed it
away while the other was still writing, and the second one's rename found nothing there.

Under the parallel board that stopped being theoretical: a 403 from two members at once has
both threads calling `mark_unusable`, which is a read-modify-write on the file holding the
API key. Hammered with twelve threads, eleven crashed and thirteen of a hundred and
forty-four writes survived. The key came through by luck.

Two things fix it and both are needed:

  a UNIQUE temp name     per process and per thread, so no two writers touch the same
                         scratch file
  a LOCK around the
  whole read-modify-write   because atomically writing a file you read before someone else's
                         write still loses their change
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import threading

try:
    import fcntl
except ImportError:                      # Windows has no fcntl
    fcntl = None

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: str) -> threading.Lock:
    """One lock per file, shared by every thread in this process."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(os.path.abspath(path), threading.Lock())


@contextlib.contextmanager
def locked(path: str):
    """Hold both locks for a read-modify-write of `path`.

    A thread lock for this process and an flock for every other one. Neither is enough alone:
    flock is per open file description and does not serialise threads sharing a descriptor,
    and a thread lock says nothing to a second console.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with _thread_lock(path):
        if fcntl is None:
            # No fcntl means Windows. The thread lock still covers the case that actually
            # bites - a parallel board writing from several threads of ONE process - and two
            # consoles at once is left unserialised rather than the package refusing to
            # import at all. Degrade, do not disappear.
            yield
            return
        lockfile = path + ".lock"
        # Held open across the yield on purpose: an advisory lock lives exactly as long as its
        # descriptor, so a context manager here would release it before the caller writes.
        fh = open(lockfile, "w", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def write(path: str, text: str, mode: int = 0o600) -> str:
    """Replace `path` with `text`, atomically, without sharing a scratch file."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    # pid AND thread id: two processes was the obvious case, two threads is the one that
    # actually bit, and os.getpid() is the same in both threads.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())          # the rename is atomic; the contents must be there first
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def write_json(path: str, obj, mode: int = 0o600, indent: int = 2) -> str:
    return write(path, json.dumps(obj, indent=indent), mode)


def read_json(path: str, default=None):
    """The stored object, or `default`. A truncated file reads as absent rather than raising."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and e.errno not in (errno.ENOENT, errno.EACCES, errno.EISDIR):
            raise
        return default
