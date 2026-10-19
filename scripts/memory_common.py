#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_common.py — shared utilities for the memory store
(file lock + atomic writes + fault-tolerant loading)

All memory scripts (add_semantic / add_semantic_batch / maintenance tools)
share ONE lock, which eliminates "concurrent JSONL writes overwrite each
other and lose memories".

Lock file: <store>.jsonl.lock (derived from the store path, .lock suffix)
Every write (append / full rewrite / maintenance) must happen inside a
memory_file_lock() transaction.
"""
import json
import os
import sys
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

# one shared lock per store file (derived from filename, .lock suffix appended)
_LOCK_FILES = {}


def _lock_path(filepath) -> Path:
    p = Path(filepath)
    key = str(p)
    if key not in _LOCK_FILES:
        _LOCK_FILES[key] = p.with_suffix(p.suffix + ".lock")
    return _LOCK_FILES[key]


@contextmanager
def memory_file_lock(filepath, retries: int = 60, delay: float = 0.5,
                     fail_fast: bool = False):
    """Memory-store file lock (Windows msvcrt.locking).

    - transactional: with memory_file_lock(path): read -> modify -> write
    - lock failures retry up to ~30s (retries x delay), then warn and continue
      (atomic writes keep the file uncorrupted; you only lose mutual exclusion,
      which beats silently losing data)
    - full-rewrite WRITERS must pass fail_fast=True: when the lock can't be
      ACQUIRED (lock contention), exit(1) instead of proceeding — an unlocked
      rewrite window is an amplifier for concurrent-overwrite data loss.
      Readers don't pass it (they keep warn-and-continue); appenders pass True
      as well. Default False preserves existing behavior.
    - platform WITHOUT a lock mechanism (non-Windows, no msvcrt): fail_fast
      cannot demand a lock the platform doesn't have — degrade with a warning
      and continue unlocked (atomic writes still hold; run one writer at a
      time). This matches the documented Unix contract.
    """
    lp = _lock_path(filepath)
    f = None
    locked = False
    try:
        import msvcrt
        f = open(lp, "a+")
        f.seek(0)
        for attempt in range(retries):
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if attempt < retries - 1:
                    time.sleep(delay)
        if not locked:
            if fail_fast:
                print(f"[error] writer failed to acquire lock ({retries} retries "
                      f"exhausted), fail-fast exit: {lp}", file=sys.stderr)
                sys.exit(1)
            print(f"[warn] lock acquisition failed ({retries} retries exhausted), "
                  f"continuing unlocked: {lp}", file=sys.stderr)
    except (ImportError, OSError) as e:
        # No lock mechanism on this platform, or the lock file can't be opened:
        # degrade with a warning — fail_fast governs lock CONTENTION, not
        # platform capability. Atomic writes still hold.
        print(f"[warn] file lock unavailable ({e}); continuing unlocked "
              f"(atomic writes hold — run one writer at a time): {lp}",
              file=sys.stderr)

    try:
        yield
    finally:
        if locked:
            try:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except (ImportError, OSError):
                pass
        try:
            f.close()
        except OSError:
            pass


def load_memories_safe(filepath):
    """Fault-tolerant JSONL load: skip corrupted lines with a warning, never crash.

    Corrupted lines are backed up to <file>.corrupt.bak so a later maintenance
    pass can't silently drop them.
    """
    mems = []
    corrupted = []
    p = Path(filepath)
    if not p.is_file():
        return mems
    with open(p, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError as e:
                corrupted.append((lineno, str(e), line[:100]))
                continue
            if isinstance(m, dict):
                mems.append(m)
    if corrupted:
        bak = p.with_suffix(p.suffix + ".corrupt.bak")
        with open(bak, "a", encoding="utf-8") as bf:
            for lineno, err, frag in corrupted:
                bf.write(json.dumps({"lineno": lineno, "error": err,
                                     "fragment": frag}, ensure_ascii=False) + "\n")
        print(f"[warn] skipped {len(corrupted)} corrupted lines (backed up to {bak}): "
              f"{[c[0] for c in corrupted[:5]]}", file=sys.stderr)
    return mems


def load_archive_file(filepath):
    """Loader for archive files: deliberately NO archived filtering.

    Entries in an archive file are archived=True by definition (maintenance
    moves them as-is; tidy-archival sets the flag). Filtering by archived on
    an archive file would return an empty list, forever. Same-family
    accidents happened on both the stamp and maintenance sides before this
    rule existed.

    [discipline] Always use this function for archived_memories.jsonl.
    Never call a filtered loader (include_archived=False default) on an
    archive file.
    """
    return load_memories_safe(filepath)


def save_memories_atomic(mems, filepath):
    """Atomic JSONL write: temp file + os.replace. Must be called inside memory_file_lock."""
    tmp = str(filepath) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for m in mems:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    os.replace(tmp, str(filepath))


def append_memories_safe(entries: list, filepath):
    """Locked JSONL append (no full rewrite, no overwriting existing lines).

    This function is a WRITER, not a reader: it passes fail_fast=True because
    an append that degrades to unlocked mode gets swallowed whole by the next
    full-rewrite's os.replace. Callers are writers with retry safety nets.
    """
    with memory_file_lock(filepath, fail_fast=True):
        payload = b"".join((json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8")
                           for e in entries)
        # Guard: a hand-edited store can end mid-line (no trailing newline);
        # appending blindly would splice the old tail and the new entry into
        # one corrupt line. Fix the newline first.
        needs_nl = False
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            with open(filepath, "rb") as f:
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"
        with open(filepath, "ab") as f:
            if needs_nl:
                f.write(b"\n")
            f.write(payload)


# ---------------------------------------------------------------------------
# Retrieval-hit logging (cross-process energy settlement)
#
# The retrieval process and the maintenance process don't share memory. Storing
# hit ids on a function attribute meant the maintenance process always read an
# empty set -> memories only ever decayed, never recharged. Hit ids now go to
# an append-only JSONL that maintenance drains (read + clear) — reliable across
# processes. The hit log is a side signal: losing it only affects recharging,
# never correctness, so it needs no file lock.
# ---------------------------------------------------------------------------

def _hit_log_path(filepath=None) -> Path:
    """Hit-log path: derived from the store file when given, else next to this script."""
    if filepath:
        p = Path(filepath)
        return p.with_suffix(p.suffix + ".hits.jsonl")
    return BASE_DIR / "retrieval_hits.jsonl"


def log_hit(memory_id: str, filepath=None) -> None:
    """Record a retrieval hit by memory_id (append-only, crash-safe)."""
    try:
        with open(_hit_log_path(filepath), "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": memory_id,
                                "ts": datetime.now().isoformat()},
                               ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[warn] hit log write failed (retrieval still works, only energy "
              f"recharge affected): {e}", file=sys.stderr)


def drain_hits(keep: bool = False, filepath=None) -> set:
    """Drain all recorded hits (for batch energy settlement in maintenance).

    keep=False (old default): read and clear.
    keep=True: read WITHOUT clearing — the settlement side calls clear_hits()
    explicitly after its write succeeds. A crash mid-settlement leaves the
    hit data intact for the next round.

    Clearing truncates instead of unlinking: on Windows, unlink fails while
    another process holds a handle -> the file never empties -> the same hits
    get settled twice (double recharge). Truncate has no such problem.
    """
    hits = set()
    try:
        hit_log = _hit_log_path(filepath)
        if hit_log.is_file():
            with open(hit_log, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        hits.add(json.loads(line)["id"])
                    except (json.JSONDecodeError, KeyError):
                        pass
            if not keep:
                with open(hit_log, "w", encoding="utf-8") as f:
                    f.truncate(0)
    except OSError as e:
        print(f"[warn] hit log read failed (this settlement round may miss hits): "
              f"{e}", file=sys.stderr)
    return hits


def clear_hits(filepath=None) -> None:
    """Explicitly clear the hit log after a successful settlement (for drain_hits(keep=True))."""
    try:
        hit_log = _hit_log_path(filepath)
        if hit_log.is_file():
            with open(hit_log, "w", encoding="utf-8") as f:
                f.truncate(0)
    except OSError as e:
        print(f"[warn] hit log clear failed (next round may settle twice): {e}",
              file=sys.stderr)