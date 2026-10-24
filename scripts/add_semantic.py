#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_semantic.py — unified single-entry writer for the semantic memory store.

Writes one atomic-fact entry with a locally computed embedding, so scheduled
distillation/exploration tasks stop hand-appending inconsistent JSON.

Usage:
  python add_semantic.py "memory text" --category conversation-distillation --date 2025-06-01
  python add_semantic.py "insight text" --category exploration-insight
  python add_semantic.py "intel text" --category tech-intel --project owner/repo --dedupe

Configuration (env or CLI, no hidden defaults for the store path):
  $MEMORY_FILE   semantic memory store (JSONL); required — no hidden default
  $OLLAMA_URL    Ollama embedding endpoint; default: http://localhost:11434/api/embeddings

Dedupe gate flags:
  --project <owner/repo>  structured project key, stored in the project_key field
                          (normalized: strip + lowercase). Keys are set once at the
                          entry point — never re-guessed from text by regex.
  --dedupe                refuse to write when project_key+category already exists;
                          prints "SKIP: duplicated (mem_xxx)" and exits 0 (not an error).
  --force                 with --dedupe: write anyway (the only bypass).
  --supersede             archive same-key old entries and chain them to the new one
                          (for time-evolving facts); mutually exclusive with --dedupe/--force.

Behavior:
  - embedding via a local model (default: bge-m3)
  - entries never merge — every distilled fact is its own atomic unit
  - append-only; existing lines are carried over byte-for-byte (no field drift;
    the supersede rewrite re-serialises matched lines — the one reformatting path)
  - atomic writes (.tmp + os.replace); readers see either the old or the new file
  - a version-stamp refresh runs after batched changes when stamp_semantic.py
    is present next to this script (optional — skipped silently if absent)
"""
import json
import os
import sys
import time
import argparse
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

MEMORY_FILE = os.environ.get("MEMORY_FILE")  # required at write time, not import time


def _resolve_memory_file():
    """$MEMORY_FILE is checked at write time so --help works on a fresh clone."""
    if not MEMORY_FILE:
        raise SystemExit("[error] $MEMORY_FILE not set — configure your semantic memory "
                         "store path. No hidden defaults: a fake default is worse than an error.")
    return MEMORY_FILE
OLLAMA_URL = os.environ.get("OLLAMA_URL") or "http://localhost:11434/api/embeddings"
EMBED_MODEL = os.environ.get("EMBED_MODEL") or "bge-m3"
PROJECT_KEY_FIELD = "project_key"
VALID_TYPES = ("general", "event", "emotional", "preference")  # her.memory's memory types


# Credential-shaped strings are refused at the door: a key that lands in a
# long-lived memory store is a leak forever. Structural guarantee beats
# semantic caution — this gate is code, not a rule the model is trusted to keep.
import re as _re
_CRED_PATTERNS = (
    _re.compile(r"eyJ[A-Za-z0-9_-]{10,}"),                          # JWT
    _re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|rk)-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"),  # vendor keys incl. sk-proj-/sk-ant-
    _re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),    # GitHub tokens
    _re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{30,}"),        # Google API keys
    _re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    _re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"),             # AWS access key id
    _re.compile(r"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|secret|password|access[_-]?token)[_a-z0-9]*\s*[=:]\s*[!-~]{8,}"),  # KEY=value (value: 8+ ASCII-printable chars; CJK fix: \S matched whole Chinese sentences — CJK prose has no spaces, so "password: 它当时是..." was falsely rejected)
    _re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+?:\S+?@"),        # credentialed URL
    _re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}"),          # Authorization header
)


def credential_gate(text: str):
    """Return the reason a text is refused (credential-shaped), or None if clean.

    Genuinely-credential-shaped prose (e.g. talking about a password without
    one in it) can bypass via allow_credential_shaped — the bypass is logged
    loudly in the report either way.
    """
    for pat in _CRED_PATTERNS:
        m = pat.search(text)
        if m:
            return f"credential-shaped string: {m.group(0)[:24]}..."
    return None


def normalize_project_key(raw):
    """Normalize project_key: strip + lowercase. Empty/None -> None (= field not stored)."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    return key or None


def _key_tail(pk):
    """Tail segment of a project_key: 'owner/repo' -> 'repo'.
    Two-level matching means a mistyped owner still matches the same project."""
    s = str(pk).strip().lower()
    return s.rsplit("/", 1)[-1] if "/" in s else s


def find_duplicate(project_key, category, filepath=None):
    """Find a duplicate by project_key+category. Returns the existing entry id, or None.

    Structural fields only — never guess project names out of entry text.
    Two-level matching (a real-world lesson: one scheduled task wrote three
    spellings of the same key across three days and all slipped through):
      1) full-key exact match (owner/repo == owner/repo)
      2) tail-segment fallback (repo == repo) — a mistyped owner does not
         defeat "same project" detection.
    """
    if not project_key:
        return None
    filepath = filepath or _resolve_memory_file()
    cat = (category or "").strip().lower()
    try:
        from memory_common import load_memories_safe
        mems = load_memories_safe(filepath)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] dedupe read failed (treating as no-hit, write proceeds): {e}", file=sys.stderr)
        return None
    # level 1: full-key exact
    for m in mems:
        if not isinstance(m, dict):
            continue
        pk = m.get(PROJECT_KEY_FIELD)
        if not pk:
            continue
        if str(pk).strip().lower() != project_key:
            continue
        if str(m.get("category", "")).strip().lower() != cat:
            continue
        return m.get("id") or "unknown-id"
    # level 2: tail-segment fallback (same category + tail collision = same project)
    tail = _key_tail(project_key)
    for m in mems:
        if not isinstance(m, dict):
            continue
        pk = m.get(PROJECT_KEY_FIELD)
        if not pk:
            continue
        if _key_tail(pk) != tail:
            continue
        if str(m.get("category", "")).strip().lower() != cat:
            continue
        print(f"[warn] tail-segment match: new key '{project_key}' vs stored '{pk}' (tail {tail})",
              file=sys.stderr)
        return m.get("id") or "unknown-id"
    return None


def _append_line_locked(entry, filepath):
    """Atomic append while the caller holds memory_file_lock.

    .tmp + os.replace: readers see old or new, never a half line.
    Existing content is carried over as raw bytes (not re-serialized) — old
    entries never drift.
    """
    new_bytes = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    # first-write defense: missing target file -> start from empty
    old = b""
    if os.path.exists(filepath):
        with open(filepath, "rb") as f:
            old = f.read()
    tmp = str(filepath) + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(old)
            if old and not old.endswith(b"\n"):
                f.write(b"\n")
            f.write(new_bytes)
        os.replace(tmp, str(filepath))
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def embed_text(text: str):
    """Generate embedding via the local endpoint, one retry (cold starts are slow)."""
    last_err = None
    for _ in (1, 2):
        try:
            r = requests.post(
                OLLAMA_URL,
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            emb = data.get("embedding")
            if isinstance(emb, list) and emb:
                return emb
        except Exception as e:  # noqa: BLE001
            last_err = e
    print(f"[error] embedding failed: {last_err}", file=sys.stderr)
    return None


def _stamp_version():
    """Refresh the store's version stamp — single source of truth.

    Delegates to stamp_semantic.py when present next to this script; a batched
    change should always refresh the stamp afterwards. Optional component:
    when absent, skipped silently. Failure never blocks the write.
    """
    stamp = os.path.join(BASE_DIR, "stamp_semantic.py")
    if not os.path.exists(stamp):
        return
    try:
        import subprocess as _sp
        _r = _sp.run([sys.executable, stamp], capture_output=True, text=True, timeout=60)
        if _r.returncode != 0:
            print(f"[warn] version stamp refresh failed (write unaffected): "
                  f"{_r.stderr.strip()[:120]}", file=sys.stderr)
    except Exception as _e:  # noqa: BLE001
        print(f"[warn] version stamp refresh failed: {_e}", file=sys.stderr)


def add_semantic(text: str, category: str, event_date: str = None,
                 project: str = None, dedupe: bool = False,
                 force: bool = False, supersede: bool = False,
                 allow_credential_shaped: bool = False,
                 mem_type: str = "general") -> bool:
    store = _resolve_memory_file()
    text = (text or "").strip()
    if not text:
        print("[error] empty text, skipped", file=sys.stderr)
        return False
    cred = credential_gate(text)
    if cred and not allow_credential_shaped:
        print(f"[refused] {cred} — nothing written (scrub it and retry, or "
              f"pass --allow-credential-shaped for genuine prose)", file=sys.stderr)
        return False
    if cred and allow_credential_shaped:
        print(f"[warn] credential-shaped but bypassed by allow flag: {cred}",
              file=sys.stderr)

    project_key = normalize_project_key(project)
    if project is not None and project_key is None:
        print("[warn] --project empty, treated as not provided", file=sys.stderr)
    if dedupe and not project_key:
        print("[warn] --dedupe ignored: no --project (dedupe needs a project_key)",
              file=sys.stderr)
    if supersede and not project_key:
        print("[warn] --supersede ignored: no --project (writing normally)",
              file=sys.stderr)
    if supersede and (dedupe or force):
        print("[error] --supersede is mutually exclusive with --dedupe/--force "
              "(keep-old / replace-new / coexist: pick one)", file=sys.stderr)
        return False

    # Dedupe fast path: a hit refuses the write before embedding is even computed
    # (works correctly even when the embedding endpoint is down).
    if dedupe and project_key and not force:
        dup_id = find_duplicate(project_key, category)
        if dup_id:
            print(f"SKIP: duplicated ({dup_id})")
            return True

    emb = embed_text(text)
    if emb is None:
        return False

    entry_type = (mem_type or "general").strip().lower()
    if entry_type not in VALID_TYPES:
        print(f"[error] unknown type '{mem_type}' — valid: {VALID_TYPES}", file=sys.stderr)
        return False
    now = datetime.now().isoformat()
    entry = {
        "id": f"mem_{int(time.time() * 1000)}_{os.getpid()}",
        "text": text,
        "type": entry_type,
        "category": category,
        "embedding": emb,
        "timestamp": now,
        "energy": 2.0,
        "last_hit": now,
        "archived": False,
    }
    if event_date:
        entry["event_date"] = event_date
    if project_key:
        entry[PROJECT_KEY_FIELD] = project_key
    # stable_key extraction: a "[topic_subtopic]" prefix (2-40 chars) becomes a key
    # field — the retrieval side's key-priority pinning relies on it.
    if text.startswith("[") and "]" in text:
        _key = text[:text.find("]") + 1]
        if 2 < len(_key) <= 40:            # sane length only (don't swallow long spans)
            entry["stable_key"] = _key

    # Dedupe/supersede + atomic append share ONE lock transaction: no
    # check-then-write race window.
    from memory_common import memory_file_lock, append_memories_safe
    try:
        with memory_file_lock(store, fail_fast=True):  # writers fail fast
            if dedupe and project_key and not force:
                dup_id = find_duplicate(project_key, category)
                if dup_id:
                    print(f"SKIP: duplicated ({dup_id})")
                    return True
            if supersede and project_key:
                # supersede chain, locked in three steps: find old -> mutate old
                # (archived + chain fields) -> write new, one full-store rewrite.
                # Legacy entries without an id are skipped with a warning — never
                # silently replaced.
                from memory_common import load_memories_safe, save_memories_atomic
                mems = load_memories_safe(store)
                now_iso = datetime.now().isoformat()
                old_ids = []
                for m in mems:
                    if not isinstance(m, dict) or m.get("archived", False):
                        continue
                    pk = m.get(PROJECT_KEY_FIELD)
                    if not pk or str(pk).strip().lower() != project_key:
                        continue
                    if str(m.get("category", "")).strip().lower() != (category or "").strip().lower():
                        continue
                    mid = m.get("id")
                    if not mid:
                        print(f"[warn] supersede skipped a legacy entry without id "
                              f"(text[:30]={str(m.get('text', ''))[:30]})", file=sys.stderr)
                        continue
                    m["archived"] = True
                    m["superseded_by"] = entry["id"]
                    m["supersede_date"] = now_iso
                    m["_archive_note"] = f"superseded by {entry['id']}"
                    old_ids.append(mid)
                if old_ids:
                    entry["supersedes"] = old_ids
                    save_memories_atomic(mems + [entry], store)
                    print(f"SUPERSEDE: {len(old_ids)} old entries archived, chained to {entry['id']}")
                    _stamp_version()
                    return True
                # no same-key old entry -> plain write (supersede degrades gracefully)
                print("SUPERSEDE: no active same-key entry, writing normally")
            _append_line_locked(entry, store)
    except OSError as e:
        # Atomic write failed (e.g. target file held on Windows) -> fall back to a
        # locked append; never silently drop a memory. Exception: supersede mode
        # refuses the fallback — a fallback append writes the new entry without
        # archiving the old ones = a half transaction. Retry instead.
        if supersede:
            print(f"[error] SUPERSEDE atomic write failed ({e}); refusing half-transaction "
                  f"fallback, retry", file=sys.stderr)
            return False
        print(f"[warn] atomic write failed ({e}), falling back to locked append", file=sys.stderr)
        append_memories_safe([entry], store)

    # refresh the store's version stamp after the batched change
    _stamp_version()

    _tag = f" [project={project_key}]" if project_key else ""
    print(f"OK: stored [{category}]{_tag} {text[:30]}...")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified semantic memory writer")
    parser.add_argument("text", type=str, help="memory/insight text")
    parser.add_argument("--category", type=str, required=True, help="category (e.g. conversation-distillation)")
    parser.add_argument("--date", type=str, default=None, help="event date (e.g. 2025-06-01)")
    parser.add_argument("--project", type=str, default=None,
                        help="structured project key (owner/repo; normalized lowercase)")
    parser.add_argument("--dedupe", action="store_true",
                        help="dedupe by project_key+category; hit refuses write, prints SKIP, exit 0")
    parser.add_argument("--force", action="store_true",
                        help="with --dedupe: write anyway")
    parser.add_argument("--supersede", action="store_true",
                        help="archive same-key old entries + write new (time-evolving facts)")
    parser.add_argument("--allow-credential-shaped", action="store_true",
                        help="bypass the credential gate for genuinely-shaped prose (logged)")
    parser.add_argument("--type", dest="mem_type", default="general",
                        choices=list(VALID_TYPES),
                        help="memory type (her.memory consumes: general/event/emotional/preference)")
    args = parser.parse_args()

    ok = add_semantic(args.text, args.category, args.date,
                      project=args.project, dedupe=args.dedupe, force=args.force,
                      supersede=args.supersede,
                      allow_credential_shaped=args.allow_credential_shaped,
                      mem_type=args.mem_type)
    sys.exit(0 if ok else 1)