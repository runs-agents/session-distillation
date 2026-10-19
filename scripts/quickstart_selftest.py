#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quickstart_selftest.py — end-to-end self-test in a pristine environment.

Verifies the whole package works for a brand-new user with nothing but
Python >= 3.10 and `pip install requests`:

  1. fabricates a tiny fake session database + distilled ledger (synthetic data)
  2. runs distill_export.py against it
  3. runs add_semantic.py (single write) and add_semantic_batch.py (batch write)
     against a built-in mock embedding server (no real Ollama needed)
  4. checks: export JSON has sessions, the store has entries, report.json
     exists, exit codes match

Run:  python quickstart_selftest.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class MockEmbedHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"embedding": [0.01] * 8}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


def start_mock_embed_server():
    server = HTTPServer(("127.0.0.1", 0), MockEmbedHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}/"


def make_fake_db(path):
    """A fake platform session DB — synthetic conversations, nothing real."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL)")
    db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, "
               "active INTEGER, timestamp REAL)")
    rows = [
        ("sess-alpha", 1700000000.0, [
            ("user", "guess what, the study ritual actually worked this week", 1, 1700000010.0),
            ("assistant", "told you keeping the tally helps — three days straight now", 1, 1700000011.0),
            ("user", "I'd only keep the promise if you keep the tally, remember", 1, 1700000012.0),
        ]),
        ("sess-beta", 1700100000.0, [
            ("user", "feeling a bit down today, nothing specific", 1, 1700100010.0),
            ("assistant", "that's allowed. no agenda today — just company", 1, 1700100011.0),
            ("user", "thanks, that's exactly what I needed to hear", 1, 1700100012.0),
        ]),
        ("cron_daily_brief", 1700200000.0, [  # must be excluded (cron_ prefix)
            ("user", "[System: scheduled task] morning brief", 1, 1700200010.0),
        ]),
        ("sess-ticket", 1700300000.0, [  # excluded when ticket_prefixes configured (step 1b)
            ("user", "Please run the external review task for the widget.", 1, 1700300010.0),
        ]),
    ]
    for sid, ts, msgs in rows:
        db.execute("INSERT INTO sessions VALUES (?, ?)", (sid, ts))
        for role, content, active, mts in msgs:
            db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                       (sid, role, content, active, mts))
    db.commit()
    db.close()


def main():
    tmp = tempfile.mkdtemp(prefix="distill_quickstart_")
    print(f"self-test workspace: {tmp}\n")

    # ---- step 0: mock embedding server ----
    server, embed_url = start_mock_embed_server()
    print("step 0: mock embedding server up")

    state_db = os.path.join(tmp, "state.db")
    done_list = os.path.join(tmp, "ledger.json")
    store = os.path.join(tmp, "semantic_memories.jsonl")
    export_out = os.path.join(tmp, "_pending_distill.json")
    batch_in = os.path.join(tmp, "batch.json")

    make_fake_db(state_db)
    with open(done_list, "w", encoding="utf-8") as f:
        json.dump({"distilled_sessions": [], "kept_unprocessed": {}, "progress_marks": {}}, f)
    with open(batch_in, "w", encoding="utf-8") as f:
        json.dump([
            {"text": "[selftest_batch] synthetic memory entry one for the quickstart check",
             "category": "selftest"},
            {"text": "[selftest_batch] synthetic memory entry two for the quickstart check",
             "category": "selftest", "event_date": "2025-06-01"},
            {"text": "[selftest_batch] credential probe eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake.fake",
             "category": "selftest"},
            {"text": "[selftest_batch] credential probe ghp_16C7eFgHiJkLmNoPqRsTuVwXyZ1234567890",
             "category": "selftest"},
            {"text": "[selftest_batch] credential-shaped prose allowed on purpose ghp_16C7eFgHiJkLmNoPqRsTuVwXyZ1234567890",
             "category": "selftest", "allow_credential_shaped": True},
        ], f, ensure_ascii=False)

    env = dict(os.environ)
    env.update({"STATE_DB": state_db, "DONE_LIST": done_list,
                "MEMORY_FILE": store, "OLLAMA_URL": embed_url})

    # ---- step 1: export ----
    print("\nstep 1: distill_export")
    r = subprocess.run([PY, os.path.join(BASE_DIR, "distill_export.py"), "--out", export_out],
                       capture_output=True, text=True, env=env, timeout=60)
    check("export exit code 0", r.returncode == 0, r.stderr.strip()[:120])
    if os.path.exists(export_out):
        data = json.load(open(export_out, encoding="utf-8"))
        check("exported sessions present", len(data.get("sessions", {})) >= 2,
              f"{len(data.get('sessions', {}))} sessions")
        check("rank table present", isinstance(data.get("_rank"), list) and data["_rank"])
        check("cron session excluded", "cron_daily_brief" not in data["sessions"])
        check("system injection filtered",
              all("[System:" not in m["content"]
                  for s in data["sessions"].values() for m in s))
    else:
        check("export output exists", False)

    # ---- step 1b: ticket-prefix exclusion via config ----
    print("\nstep 1b: ticket exclusion (config-driven)")
    cfg_dir = os.path.join(tmp, "cfg")
    os.makedirs(cfg_dir)
    shutil.copy2(os.path.join(BASE_DIR, "distill_export.py"), cfg_dir)
    with open(os.path.join(cfg_dir, "distill_config.json"), "w", encoding="utf-8") as f:
        json.dump({"ticket_prefixes": ["Please run the external review task"]}, f)
    export_out_b = os.path.join(tmp, "_pending_ticket_test.json")
    r = subprocess.run([PY, os.path.join(cfg_dir, "distill_export.py"), "--out", export_out_b],
                       capture_output=True, text=True, env=env, timeout=60)
    check("ticket-exclusion export exit 0", r.returncode == 0, r.stderr.strip()[:120])
    if os.path.exists(export_out_b):
        datab = json.load(open(export_out_b, encoding="utf-8"))
        check("ticket session excluded via config", "sess-ticket" not in datab.get("sessions", {}))
    else:
        check("ticket test output exists", False)

    # ---- step 2: single write ----
    print("\nstep 2: add_semantic (single)")
    r = subprocess.run([PY, os.path.join(BASE_DIR, "add_semantic.py"),
                        "[selftest_single] synthetic single-entry write",
                        "--category", "selftest"],
                       capture_output=True, text=True, env=env, timeout=60)
    check("single write exit 0", r.returncode == 0, r.stderr.strip()[:120])
    check("store file created", os.path.exists(store))

    # ---- step 3: batch write ----
    print("\nstep 3: add_semantic_batch")
    r = subprocess.run([PY, os.path.join(BASE_DIR, "add_semantic_batch.py"),
                        "--batch", batch_in],
                       capture_output=True, text=True, env=env, timeout=60)
    report_path = batch_in + ".report.json"
    check("batch exit code 2 (one refused entry = real failure)",
          r.returncode == 2, f"rc={r.returncode}")
    if os.path.exists(report_path):
        rep = json.load(open(report_path, encoding="utf-8"))
        check("report.json contract", rep.get("total") == 5 and rep.get("written") == 3
              and rep.get("failed") == 2,
              f"written={rep.get('written')} failed={rep.get('failed')}")
        cred_fails = [r_ for r_ in rep.get("results", []) if r_.get("status") == "fail"]
        check("credential gate refused JWT and GitHub-token shapes",
              len(cred_fails) == 2 and all("credential" in str(r_.get("reason", "")) for r_ in cred_fails))
        allowed = [r_ for r_ in rep.get("results", [])
                   if r_.get("status") == "ok" and r_.get("index") == 4]
        check("allow_credential_shaped bypass wrote the flagged entry",
              len(allowed) == 1)
    else:
        check("report.json exists", False)

    # ---- step 4: store integrity ----
    print("\nstep 4: store integrity")
    lines = [l for l in open(store, encoding="utf-8").read().splitlines() if l.strip()]
    check("store has 4 entries", len(lines) == 4, f"{len(lines)} lines")
    ok_json = all(isinstance(json.loads(l), dict) for l in lines)
    check("all lines valid JSONL", ok_json)

    # ---- step 5: trailing-newline guard (hand-edited store safety) ----
    print("\nstep 5: trailing-newline guard")
    with open(store, "r+b") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            f.seek(-1, os.SEEK_END)
            f.truncate()  # simulate a hand-edited store with no final newline
    sys.path.insert(0, BASE_DIR)
    from memory_common import append_memories_safe, load_memories_safe
    append_memories_safe([{"id": "mem_selftest_newline", "text": "[selftest] post-trim entry"}], store)
    back = load_memories_safe(store)
    check("append after mid-line ending keeps the store readable",
          len(back) == 5, f"{len(back)} entries")

    server.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    n_pass = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 50}\nself-test: {n_pass}/{len(results)} checks passed "
          f"({PASS if n_pass == len(results) else FAIL})")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()