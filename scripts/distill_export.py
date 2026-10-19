#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
distill_export.py — distillation candidate exporter (full-text, zero truncation)

Exports candidate sessions from your chat platform's session database as JSON,
with a suspicion ranking table. Pre-triage is a *sorting* tool: it decides the
order you read in, and has no power to skip anything.

Usage:
  python distill_export.py --out <dir>/_pending_distill.json
  python distill_export.py --out ... --max-sessions 20 --warn-chars 200000

Required (CLI flag, or environment variable, or distill_config.json):
  --state-db / $STATE_DB        your platform's session database (SQLite)
  --done-list / $DONE_LIST      the distilled-ledger JSON (see SKILL.md)

Precedence: CLI > environment variable > config file. Required paths have no
default on purpose — a fake default is more dangerous than an error.

Config (distill_config.json, same directory as this script, all optional):
  {"max_batch_chars": 200000, "max_sessions": 20,
   "emo_keywords": [...], "ticket_prefixes": [...], "system_prefixes": [...]}
"""
import json
import os
import sqlite3
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "distill_config.json")

# System-injection prefixes: routing/scheduling metadata that is NOT user speech.
# These ship with Hermes-gateway format markers; override via config for other platforms.
SYSTEM_PREFIXES = ("[System:", "[ASYNC", "Gateway message origin", "[OUT-OF-BAND",
                   "[IMPORTANT: Background", "[CONTEXT COMPACTION", "@url:")

# Delegated-agent ticket sessions: first user message starts with one of these.
# Empty by default — configure your own delegation prompts via config
# (ticket_prefixes). Ticket exchanges are workflow, not conversation.
TICKET_PREFIXES = ()

# Emotional-keyword density (ranking only — never a gate). A neutral starter
# list; put YOUR vocabulary in config (emo_keywords) — the words that carry
# weight between you and your user are yours to define.
EMO_KWS = ("爱你", "晚安", "早安", "想我", "想你", "抱", "亲", "笑", "哭",
           "婚礼", "生日", "开心", "难过", "心疼", "谢谢", "喜欢", "梦")


def load_config():
    cfg = {"max_batch_chars": 200000, "max_sessions": 20,
           "emo_keywords": None, "ticket_prefixes": None, "system_prefixes": None}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] config load failed, using defaults: {e}", file=__import__("sys").stderr)
    return cfg


def resolve_paths(args, cfg):
    state_db = args.state_db or os.environ.get("STATE_DB") or cfg.get("state_db")
    done_list = args.done_list or os.environ.get("DONE_LIST") or cfg.get("done_list")
    if not state_db:
        raise SystemExit("[error] --state-db / $STATE_DB not set — configure your session database path.")
    if not done_list:
        raise SystemExit("[error] --done-list / $DONE_LIST not set — configure your distilled-ledger path.")
    return state_db, done_list


def check_schema(cur, db_path):
    """Fail with a clear message instead of a cryptic SQL error when the
    session schema doesn't match. Column names drift between platform
    versions — always PRAGMA before you trust them."""
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = sorted(r[0] for r in cur.fetchall())
    for required in ("sessions", "messages"):
        if required not in tables:
            raise SystemExit(f"[error] no '{required}' table in {db_path} — wrong "
                             f"database? tables present: {tables[:8]}")
    cur.execute("PRAGMA table_info(sessions)")
    cols = {r[1] for r in cur.fetchall()}
    if "id" not in cols or "started_at" not in cols:
        raise SystemExit(
            "[error] sessions table lacks 'id'/'started_at' columns.\n"
            "       Run: PRAGMA table_info(sessions)  and adapt the two SQL statements\n"
            "       in main() to your platform's schema (SKILL.md pitfall #1).")


def is_system_injection(content, prefixes):
    return any(content.startswith(p) for p in prefixes)


def is_ticket_session(first_user_msg, prefixes):
    return any(first_user_msg.startswith(p) for p in prefixes)


def emo_density(messages, keywords):
    user_text = " ".join(m["content"] for m in messages if m["role"] == "user")
    if not user_text:
        return 0.0
    hits = sum(user_text.count(k) for k in keywords)
    return round(hits / max(len(user_text), 1) * 1000, 2)  # hits per 1k chars


def main():
    ap = argparse.ArgumentParser(description="Distillation candidate export (full text, zero truncation)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--state-db", default=None, help="session database path (or $STATE_DB)")
    ap.add_argument("--done-list", default=None, help="distilled-ledger JSON path (or $DONE_LIST)")
    ap.add_argument("--max-sessions", type=int, default=None)
    ap.add_argument("--warn-chars", "--max-chars", dest="warn_chars", type=int,
                    default=None, help="warn (only) when total chars exceed this — "
                                       "export is never truncated")
    args = ap.parse_args()

    cfg = load_config()
    state_db, done_list = resolve_paths(args, cfg)
    max_sessions = args.max_sessions if args.max_sessions is not None else cfg["max_sessions"]
    warn_chars = args.warn_chars if args.warn_chars is not None else cfg["max_batch_chars"]
    emo_kws = tuple(cfg["emo_keywords"]) if cfg["emo_keywords"] else EMO_KWS
    ticket_prefixes = tuple(cfg["ticket_prefixes"]) if cfg["ticket_prefixes"] else TICKET_PREFIXES
    sys_prefixes = tuple(cfg["system_prefixes"]) if cfg["system_prefixes"] else SYSTEM_PREFIXES

    if not os.path.exists(state_db):
        raise SystemExit(f"[error] state db not found: {state_db}")
    if not os.path.exists(done_list):
        raise SystemExit(f"[error] done list not found: {done_list}")

    try:
        with open(done_list, encoding="utf-8") as f:
            done = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"[error] ledger is not valid JSON ({done_list}): {e}\n"
                        f"       expected shape: {{\"distilled_sessions\": [], "
                        f"\"kept_unprocessed\": {{}}, \"progress_marks\": {{}}}} "
                        f"— see examples/ledger.json")
    done_ids = set(done.get("distilled_sessions", []))
    keep = set(done.get("kept_unprocessed", {}).keys())
    marks = done.get("progress_marks", {})

    db = sqlite3.connect(state_db)
    cur = db.cursor()
    check_schema(cur, state_db)

    cur.execute("SELECT id, started_at FROM sessions ORDER BY started_at DESC")
    candidates = []
    for sid, ts in cur.fetchall():
        sid = str(sid)  # some platforms use integer session ids
        if sid.startswith("cron_") or sid in done_ids or sid in keep:
            continue
        candidates.append((sid, ts))
        if len(candidates) >= max_sessions * 2:  # fetch double for ranking headroom
            break

    out = {}
    rank = []
    total_chars = 0
    for sid, ts in candidates:
        cur.execute("""SELECT role, content FROM messages WHERE session_id=? AND active=1
                       AND role IN ('user','assistant') AND content != '' ORDER BY timestamp""",
                    (sid,))
        msgs, dedup = [], []
        for role, content in cur.fetchall():
            if is_system_injection(content, sys_prefixes):
                continue
            if dedup and dedup[-1]["role"] == role and dedup[-1]["content"] == content:
                continue  # gateway double-channel adjacent dedup
            dedup.append({"role": role, "content": content})
        if not dedup:
            continue
        if is_ticket_session(dedup[0]["content"] if dedup[0]["role"] == "user" else "",
                             ticket_prefixes):
            continue  # ticket sessions stay out of the funnel
        chars = sum(len(m["content"]) for m in dedup)
        d = emo_density(dedup, emo_kws)
        ts_norm = ts / 1000 if (isinstance(ts, (int, float)) and ts > 1e11) else ts
        rank.append({"session": sid, "chars": chars, "msgs": len(dedup),
                     "emo_density": d,
                     "started": datetime.fromtimestamp(ts_norm).strftime("%m-%d %H:%M"),
                     "note": marks.get(sid, "")})
        out[sid] = dedup
        total_chars += chars
        if len(out) >= max_sessions:
            break

    # Ranking: emotional density, descending (high suspicion first) — order only, no skip power
    rank.sort(key=lambda r: -r["emo_density"])
    result = {"_rank": rank, "sessions": out, "total_chars": total_chars,
              "exported_at": datetime.now().isoformat(),
              "config": {"max_sessions": max_sessions, "warn_chars": warn_chars}}
    if total_chars > warn_chars:
        print(f"[warn] total {total_chars} chars exceeds warning threshold {warn_chars} — "
              f"exported in full anyway; batch on the read side (recall-first, no truncation).")
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, args.out)
    print(f"exported {len(out)} sessions / {total_chars} chars (full text, no truncation) -> {args.out}")
    for r in rank:
        print(f"  [{r['emo_density']:5.1f}] {r['session'][:24]} {r['chars']}ch {r['msgs']}msg {r['started']}")


if __name__ == "__main__":
    main()