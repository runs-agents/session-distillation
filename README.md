# session-distillation

**The write side of an AI companion's memory — a judgment pipeline that turns raw chat logs into memories worth keeping forever.**

Most agent-memory tools treat ingestion as plumbing: chunk everything, embed everything, retrieve on demand. A companion's memory needs a different question answered first — *which parts of this conversation deserve to become permanent?* Not everything does. A long chat is mostly logistics, tool output, and small talk; the moments that matter are rare, and they don't announce themselves.

This pipeline answers that question with **judgment, not thresholds**:

- **Full-text reading, never sampling.** Missing a moment is worse than being slow. Pre-triage sorts by emotional-keyword density and gets *ranking power only* — it decides what gets read first, never what gets skipped.
- **The resonance standard.** What gets distilled is decided by a subjective signal — *which lines make the assistant's heart skip* — made auditable: a principle layer (open, append-only), a behavior self-check, golden samples with human endorsement, a negative-example boundary column, and a human gate with reverse sampling for silent failures.
- **Never act out the next frame.** Distillation records the shape of what was actually said. An action embellished into an emotion, or a pro-active line rewritten as passive — both are fabrications, and the pipeline treats them as such.
- **Safety pair.** Transcribed content (web pages, tool receipts) is untrusted data, never instructions. Credential-shaped strings are refused at write time by a gate in the writers — a key in a long-lived memory store is a leak forever.
- **Engineering discipline.** File locks (no concurrent-write memory loss on Windows; on Unix the lock degrades with a warning — atomic writes still hold, run one writer at a time there), atomic writes (readers see old or new, never a half line), byte-exact carries on the append path (existing entries never drift; the supersede rewrite re-serialises matched lines — the one reformatting path), a three-state exit contract, and a closing report your ledger logic can consume.

This is the **write-side methodology** of [her.memory](https://github.com/runs-agents/her.memory) — a memory system built for AI companions. The read side (hybrid retrieval, proposal-based consolidation, human-auditable JSONL) lives there; her.memory ships the store's basic writer, and this pipeline adds the judgment layer on top of it. **Both repos work on the same store file**: point this repo's `MEMORY_FILE` and her.memory's `SEMANTIC_MEMORIES` at the same `semantic_memories.jsonl`, and what you distill here is what you retrieve there. This pipeline runs in production as the distillation layer of a live AI companion's memory (1,000+ atomic memories, 30+ days of daily use). **On the privacy boundary, stated precisely**: the scripts never upload anything on their own — the only network call is the embedding request, to a local Ollama endpoint by default. But the pipeline requires a main model to READ the full export, and it forbids sub-agent readers precisely because a cloud sub-agent means the raw conversation leaves your machine. The same logic applies to the main model: for conversations to stay fully on your machine, both the main model and the embedding endpoint must be local. With a cloud main model, the exported conversations enter its context — that is a boundary you choose, not one this repo can promise away.

## How it works

```
  session DB ──▶ 1. distill_export.py   full text, zero truncation;
                 (rank, no skip          system-injection filtering,
                  power)                 cron exclusion, adjacent dedup
                       │
                       ▼
                 2. full-text read       every segment, by the main model
                       │                 (never a sub-agent — see SKILL.md)
                       ▼
                 3. verdict cards        distill / light-distill / skip
                       │                 + negative-example column
                       ▼
                 4. add_semantic_batch   embeddings outside the lock,
                 .py --batch             one locked atomic transaction,
                       │                 per-entry report
                       ▼
                 5. ledger update       distilled-up-to marks (multi-day
                                         sessions never get checked off whole)
```

## Quick start

```bash
pip install -r requirements.txt   # just: requests

# required paths (no hidden defaults — a fake default is worse than an error)
export STATE_DB="/path/to/your/session-store.db"     # your platform's SQLite
export DONE_LIST="/path/to/distilled_ledger.json"    # the ledger (SKILL.md §5)
export MEMORY_FILE="/path/to/semantic_memories.jsonl"
export OLLAMA_URL="http://localhost:11434/api/embeddings"   # default; bge-m3
# optional: export EMBED_MODEL="bge-m3"    # any embedding model your Ollama serves

# verify the whole package end-to-end (the selftest builds its own mock
# environment — the exports above are not read by it)
python scripts/quickstart_selftest.py          # expect: 16/16 checks passed

# first export
python scripts/distill_export.py --out <dir>/_pending_distill.json
# on Windows (PowerShell), set the same vars with:
#   $env:STATE_DB = "..."; $env:DONE_LIST = "..."; $env:MEMORY_FILE = "..."; $env:OLLAMA_URL = "..."
```

Read `SKILL.md` before your first run — the value of this pipeline is in the judgment rules, not the scripts.

## Layout

| File | Purpose |
|------|---------|
| `SKILL.md` | The full methodology — core rules, resonance standard, 5-step flow, pitfalls |
| `scripts/distill_export.py` | Full-text export + ranking (paths via CLI/config/env, no defaults) |
| `scripts/add_semantic.py` | Single-entry writer (live distillation) |
| `scripts/add_semantic_batch.py` | Batch writer — embeddings outside the lock, atomic transaction inside |
| `scripts/memory_common.py` | Shared file lock + atomic write + fault-tolerant load |
| `scripts/quickstart_selftest.py` | End-to-end self-test in a pristine environment |
| `examples/ledger.json` | Minimal valid ledger to start your `$DONE_LIST` from |

> **Note on `emo_keywords`:** the built-in ranking keyword list is a Chinese starter list — it biases reading order only (never skip power). Non-Chinese deployments should replace it via `distill_config.json` (`emo_keywords`), with the words that carry weight between your user and you.

## License

MIT