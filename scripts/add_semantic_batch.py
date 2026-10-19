#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_semantic_batch.py — batch writer for the semantic memory store.

Single-entry mode lives in add_semantic.py — this script only batches.

Usage:
  python add_semantic_batch.py --batch <file.json>

Input: a JSON array, each item:
  {"text": str required, "category": str required, "event_date": str optional,
   "project": str optional, "dedupe": bool optional, "force": bool optional,
   "type": str optional (her.memory memory type: general/event/emotional/preference),
   "allow_credential_shaped": bool optional (bypass the credential gate for
   genuinely-shaped prose; the bypass is reported either way)}

Behavior:
  - all embeddings computed OUTSIDE the lock (network calls never inside)
  - one locked transaction: load once -> per-entry keyword dedupe (a hit on an
    existing text -> skip + report) -> append bytes assembled -> single atomic
    write -> single stamp refresh
  - skip/fail semantics are separated:
      skip_dedupe_hit (benign: the entry already exists — counts as success)
      embed_fail / empty_text (real failure: marker held for that entry, retry)
  - closing report: per-entry results written to <batch>.report.json — ledger
    marking logic consumes this report; sessions with benign skips are marked
    normally, sessions with real failures are held for retry
  - exit codes: 0 all written / 1 benign skips, no real failures / 2 real failures
  - per-entry failure isolation: one bad entry never kills the batch

Keyed discipline: conversation-distillation entries usually carry no project_key
-> they do not participate in key-based dedupe/supersede. Keyword dedupe runs
ONLY when the item sets "dedupe": true (and can be bypassed with "force": true) —
the first 60 chars are checked for containment against the store; a hit is a
benign skip. Items without the flag are always written.
"""
import json
import os
import sys
import time
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from add_semantic import (  # noqa: E402
    embed_text, _stamp_version, normalize_project_key, find_duplicate,
    PROJECT_KEY_FIELD, credential_gate, VALID_TYPES,
)
from memory_common import (  # noqa: E402
    memory_file_lock, load_memories_safe, append_memories_safe,
)

from add_semantic import MEMORY_FILE, _resolve_memory_file  # noqa: E402

DEDUPE_PREFIX_LEN = 60  # keyword dedupe caliber: first 60 chars of text


def _kw_dedupe_hit(text, existing_texts):
    """Keyword-caliber dedupe (for project-key-less conversation entries).

    Bidirectional prefix containment: take n = min(len(prefix), len(existing));
    exact prefix match of n chars counts as a duplicate. (One-directional
    startswith never matches when the existing entry is the shorter one — a
    first-run lesson.) Conservative prefix comparison only, no fuzzy
    similarity — prefer a missed duplicate over a false one.
    """
    prefix = text[:DEDUPE_PREFIX_LEN].strip()
    if not prefix:
        return False
    for t in existing_texts:
        n = min(len(prefix), len(t))
        if n >= 8 and prefix[:n] == t[:n]:
            return True
    return False


def run_batch(batch_path):
    t_start = time.time()
    with open(batch_path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        print("[error] --batch input must be a JSON array", file=sys.stderr)
        return 2

    results = []          # closing report: per-entry {index, status, reason, id}
    entries_ok = []      # fully-built entries (already embedded)
    n_dupe = n_fail = 0

    # ---- phase 1: outside the lock (validation + embedding; no network in locks) ----
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            results.append({"index": i, "status": "fail", "reason": "bad_item_not_dict"})
            n_fail += 1
            continue
        text = (item.get("text") or "").strip()
        category = (item.get("category") or "").strip()
        if not text or not category:
            results.append({"index": i, "status": "fail", "reason": "empty_text_or_category"})
            n_fail += 1
            continue
        cred = credential_gate(text)
        if cred and not item.get("allow_credential_shaped"):
            results.append({"index": i, "status": "fail", "reason": cred})
            n_fail += 1
            continue
        project_key = normalize_project_key(item.get("project"))
        if item.get("dedupe") and project_key and not item.get("force"):
            dup_id = find_duplicate(project_key, category)
            if dup_id:
                results.append({"index": i, "status": "skip_dedupe_hit", "reason": f"key:{dup_id}"})
                n_dupe += 1
                continue
        emb = embed_text(text)
        if emb is None:
            results.append({"index": i, "status": "fail", "reason": "embed_fail"})
            n_fail += 1
            continue
        entry_type = (item.get("type") or "general")
        if not isinstance(entry_type, str) or entry_type.strip().lower() not in VALID_TYPES:
            results.append({"index": i, "status": "fail",
                            "reason": f"bad_type:{entry_type!r}"})
            n_fail += 1
            continue
        now = datetime_iso = __import__("datetime").datetime.now().isoformat()
        entry = {
            "id": f"mem_{int(time.time() * 1000)}_{os.getpid()}_{i}",
            "text": text, "type": entry_type.strip().lower(), "category": category,
            "embedding": emb, "timestamp": now, "energy": 2.0,
            "last_hit": now, "archived": False,
        }
        if item.get("event_date"):
            entry["event_date"] = item["event_date"]
        if project_key:
            entry[PROJECT_KEY_FIELD] = project_key
        if text.startswith("[") and "]" in text:
            _key = text[:text.find("]") + 1]
            if 2 < len(_key) <= 40:
                entry["stable_key"] = _key
        kw_check = bool(item.get("dedupe")) and not item.get("force")
        entries_ok.append((i, entry, kw_check))

    # ---- phase 2: one locked transaction (final dedupe + byte assembly + atomic write + stamp) ----
    written = 0
    if entries_ok:
        store = _resolve_memory_file()
        try:
            with memory_file_lock(store, fail_fast=True):  # writer discipline: fail fast
                existing = load_memories_safe(store)
                existing_texts = [str(m.get("text", "")) for m in existing
                                  if isinstance(m, dict) and not m.get("archived")]
                new_bytes = b""
                for i, entry, kw_check in entries_ok:
                    # final dedupe inside the lock, GATED on the item's dedupe
                    # flag (the pre-grep ran earlier; the in-lock check wins —
                    # the store may have moved)
                    if kw_check and _kw_dedupe_hit(entry["text"], existing_texts):
                        results.append({"index": i, "status": "skip_dedupe_hit",
                                        "reason": "kw_prefix"})
                        n_dupe += 1
                        continue
                    new_bytes += (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
                    existing_texts.append(entry["text"])  # intra-batch cross-dedupe pool
                    results.append({"index": i, "status": "ok", "id": entry["id"]})
                    written += 1
                if new_bytes:
                    # byte-concatenated single atomic write (existing content
                    # carried over as raw bytes — no field drift)
                    old = b""
                    if os.path.exists(store):
                        with open(store, "rb") as f:
                            old = f.read()
                    tmp = store + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(old)
                        if old and not old.endswith(b"\n"):
                            f.write(b"\n")
                        f.write(new_bytes)
                    os.replace(tmp, store)
        except OSError as e:
            # atomic write failed -> locked-append fallback (append_memories_safe
            # carries its own fail-fast lock). Batch mode has no supersede
            # half-transaction risk, so the fallback is safe (same verdict as
            # single-entry mode).
            print(f"[warn] atomic write failed ({e}), falling back to locked append",
                  file=sys.stderr)
            ok_entries = [e_ for i_, e_, _ in entries_ok
                          if any(r["index"] == i_ and r["status"] == "ok" for r in results)]
            if ok_entries:
                append_memories_safe(ok_entries, store)
                written = len(ok_entries)

    # ---- phase 3: single stamp + closing report ----
    if written:
        _stamp_version()
    report = {
        "batch": os.path.basename(batch_path),
        "total": len(items),
        "written": written,
        "skipped_dedupe": n_dupe,
        "failed": n_fail,
        "results": results,
        "duration_sec": round(time.time() - t_start, 1),
    }
    report_path = batch_path + ".report.json"
    tmp = report_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, report_path)

    print(f"batch done: {written} written / {n_dupe} benign skips / {n_fail} failures "
          f"| {report['duration_sec']}s | report {os.path.basename(report_path)}")
    for r in results:
        if r["status"] != "ok":
            print(f"  - [{r['status']}] #{r['index']} {r.get('reason', '')}")
    return 2 if n_fail else (1 if n_dupe else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Semantic memory batch writer (--batch)")
    parser.add_argument("--batch", type=str, required=True,
                        help="batch input JSON path (array: text/category/event_date/project/dedupe/force)")
    args = parser.parse_args()
    sys.exit(run_batch(args.batch))