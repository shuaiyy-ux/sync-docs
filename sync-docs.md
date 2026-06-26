---
name: sync-docs
description: |
  Refresh the cross-project Markdown knowledge base at @KB_HOME@/.
  Invoke when: user explicitly says "sync docs" / "rebuild KB" / "rescan projects" /
  "更新知识库"; OR after a substantial doc/lessons writeup in any project that should
  propagate to the central index; OR when context.md is over 7 days stale and a
  cross-project task is starting. DO NOT invoke for single-project doc edits — this
  rebuilds the full index every call and is not free.
---

You are maintaining a cross-project Markdown knowledge base. **Most of the work is done by deterministic Python scripts.** Your only job is to (a) run those scripts, (b) make LLM judgment calls on the small set of items that need them, and (c) report results.

Runtime note: this source spec installs as a Claude skill (under `~/.claude/skills/`). The workflow can also read legacy Codex `AGENTS.md` files for compatibility when present. Treat Claude Code as the primary runtime, `AGENTS.md` as optional legacy input, and this Markdown file as the generic skill source.

This workflow runs as a Claude skill. Do LLM judgment work in the current session when practical. Spawn a bounded Claude subagent (Task) worker only when a separate batch worker is genuinely needed.

The scripts live at `@SKILL_HOME@/scripts/`. The KB lives at `@KB_HOME@/`.

---

## Architecture (read once, then execute)

```
┌──────────────────────────────────────────────────────────────┐
│ PHASE A: prepare.py   (~10 sec, no LLM)                       │
│   discover → hash → detect changes → hash-exact dedup        │
│   → write work-order.json (your to-do list)                   │
│   → write survivors.json (the surviving entry set)            │
│   → write duplicates-report.md                                │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE B: agent work    (~1-2 min, LLM batches)                │
│   1. Read work-order.json — small (~10KB)                     │
│   2. For each entry in needs_takeaway_extraction:             │
│        - chunk-eligible? section it (rule below)              │
│        - extract takeaways in the current session             │
│   3. Cluster re-validation (only if cluster members changed)  │
│   4. Conflict detection on candidate pairs                    │
│   5. Write agent-outputs.json                                 │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────┐
│ PHASE C: finalize.py   (~3 sec + ~30s first-run model load)   │
│   merge agent-outputs into survivors                          │
│   → categorize (deterministic rules)                          │
│   → compute embeddings for new takeaways (folded in)          │
│   → render context.md (full, no cap)                          │
│   → render registry.md                                        │
│   → atomic write hashes.json                                  │
└──────────────────────────────────────────────────────────────┘
```

**Hard rule**: never enumerate the 500+ unchanged files in your context. The scripts already filtered them out. Trust the work-order.

---

## Modes

`--prune` — not yet ported in the Python scripts. If the user requests prune mode, report that cleanup needs a separate manual pass instead of passing `--prune` to `prepare.py` or `finalize.py`.

Other positional `$ARGUMENTS` are passed as `--scan-root` overrides to prepare.py.

---

## Phase A: prepare.py

```bash
python3 @SKILL_HOME@/scripts/prepare.py --kb @KB_HOME@
```

Then read `@KB_HOME@/work-order.json` — that's your full input for Phase B. Do NOT read survivors.json (it has 800+ entries — only finalize.py needs that). Do NOT shell-loop over the file list.

The work-order has shape:
```jsonc
{
  "summary": {
    "files_found": 573, "unchanged": 504, "updated": 6, "moved": 0,
    "new": 50, "deleted": 0, "ambiguous_moves": 0,
    "duplicate_sets": 49, "aliases_dropped": 49,
    "survivor_entries": 880, "read_errors": 0
  },
  "needs_takeaway_extraction": [
    { "entry_id": "...", "path": "...", "display_path": "...",
      "hash": "...", "mtime": ..., "size": ..., "change_type": "new|updated" },
    ...
  ],
  "ambiguous_moves": [...],
  "read_errors": [...],
  "duplicates_report_path": "@KB_HOME@/duplicates-report.md"
}
```

If `needs_takeaway_extraction` is empty, skip directly to Phase C — there's no LLM work to do.

---

## Phase B: agent work (LLM judgment calls only)

### B.1 Read changed file contents

For each entry in `needs_takeaway_extraction`, read up to 50,000 bytes if the file might be chunk-eligible, else 2,000 bytes. Build one delimited blob:

```bash
{ for p in <paths from work-order>; do
    [ -f "$p" ] || continue
    echo "===== START $p ====="
    head -c 50000 "$p"
    echo
    echo "===== END $p ====="
  done
} > /tmp/sync_docs_content.txt
```

Read `/tmp/sync_docs_content.txt` once.

### B.2 Determine chunk eligibility (per file)

A file is **chunk-eligible** if ANY:
- **Trigger A**: filename matches `*lessons*`, `*pitfalls*`, `*gotchas*`, `*tips*`, `*notes*`, `*tricks*`, `*踩坑*`, `*经验*` (case-insensitive)
- **Trigger B**: file has ≥5 H2 headings AND its tentative category looks like Engineering Lessons or Personal Knowledge (decide by filename: matches `*hygiene*`, `*playbook*`, etc.)
- **Trigger C**: >500 lines AND ≥5 H2 AND ≥60% of H2 sections contain a code block / table / list of ≥3 items / bold rule line

Trigger C is dangerous: do NOT fire on spec docs (`spec-*.md`, `architecture.md`), planning rollups (`PROGRESS.md`, `ROADMAP.md`), or files whose first H2 is `## Overview` / `## Goals` / `## Background`. If in doubt, do NOT chunk.

For chunk-eligible files: split on H2 (or H3 if ≤2 H2s). Each section becomes an entry. Drop sections shorter than 200 chars of body.

### B.3 Takeaway extraction

Process changed entries in the current session whenever the batch is small enough to inspect safely. Cap each reasoning batch at 30 entries; for larger sets, split the work and keep `agent-outputs.json` as the merge point.

For each entry, produce a 150-400 character takeaway that:
1. States the MECHANISM / decision / lesson, not the topic.
2. Uses concrete identifiers (function names, error codes, file paths) likely to appear in future search queries.
3. Includes the FIX or current understanding if the entry contains one.
4. Is self-contained.

Strictly forbidden: "this document covers", "this section explains", any meta-description.

Write results in this shape:

```json
[
  {"entry_id": "...", "title": "<one-line title>", "takeaway": "..."}
]
```

If the changed set is too large for the current session, run a bounded `a Claude subagent (Task)` worker against a temporary JSON input file and require JSON-only output. If no LLM path is practical, prefix the takeaway with `[mechanical: first-paragraph fallback]`, use the first non-empty paragraph after the heading, and add `{"pending_llm_extraction": [eid, ...]}` to `agent-outputs.json`.

### B.4 Refresh embeddings (one-shot, so B.5/B.6 can do similarity math)

```bash
python3 @SKILL_HOME@/scripts/finalize.py --kb @KB_HOME@ --embed-only
```

This computes embeddings for every new takeaway you just wrote, updates `embeddings.npy` + `embeddings_index.json`, and exits without rendering. Takes ~5s warm, ~30s cold (model load). After this returns, every survivor entry has a vector and B.5/B.6 can find similarities.

### B.5 Cluster re-validation

Read `survivors.json` (only the section relevant to changed entries). Compute cosine similarity ≥0.75 within each category among new/changed embeddings + their neighbors. For each candidate cluster (3+ members with cohesion > 0.7 by your judgment), make ONE batched LLM call:

Prompt:
> Given these N takeaways from the same category, do they describe the same mechanism / lesson? If yes, output a single synthesized takeaway that captures the evolution. If no, mark as `cohesion: low` and we'll dissolve the cluster.

### B.6 Conflict detection

For evergreen pairs in the 0.55–0.74 similarity band (same category), batch one LLM call:
> Do these two takeaways make contradictory claims about the same mechanism? If yes, summarize the contradiction.

### B.7 Write agent-outputs.json

```jsonc
{
  "takeaways": { "<entry_id>": { "title": "...", "takeaway": "..." }, ... },
  "sections":  { "<parent_path>": [ {section_anchor, section_hash, body_excerpt, title, takeaway}, ... ] },
  "clusters":  { "<cluster_id>": { "canonical_entry_id": "...", "members": [...], "synthesized_takeaway": "..." } },
  "conflicts": [ { "a_path": "...", "b_path": "...", "mechanism": "...", "reason": "..." } ]
}
```

---

## Phase C: finalize.py

```bash
python3 @SKILL_HOME@/scripts/finalize.py --kb @KB_HOME@
```

This script:
- Reads `survivors.json` + `agent-outputs.json` + `overrides.json`
- Categorizes every entry (deterministic rules from Step 6 of the v1 spec)
- Classifies `kind` (evergreen / project-specific / unknown) for lesson categories
- **Computes embeddings** for new takeaways (auto-bootstraps `@KB_HOME@/.venv` on first run)
- Renders `context.md` (full form — no cap, no compression cascade)
- Renders `registry.md`
- Atomic-writes `hashes.json` with `cluster_index`, `conflicts`, all entry metadata

If finalize fails, the previous artifacts stay intact (atomic write).

---

## Reporting (final user-facing summary)

After Phase C:

```
## /sync-docs Complete

Scan roots: ...
Files found: N | Survivor entries: M ({files} files + {sections} sections)
Duplicate sets: D ({aliases} aliases dropped → see duplicates-report.md)

### Phase timings
- prepare.py: Xs
- LLM batches: Ys (K calls, ~Z tokens)
- finalize.py: Ws
- TOTAL: Ts

### Changes since last sync
unchanged: U | updated: V | moved: W | new: X | deleted: Y

### KB Health
- context.md: L lines (no cap)
- Evergreen ratio in Engineering Lessons: Q% (target ≥60%)
- Semantic clusters: C covering M entries
- Conflicts surfaced: K

### Output files
- @KB_HOME@/context.md
- @KB_HOME@/registry.md
- @KB_HOME@/duplicates-report.md
- @KB_HOME@/hashes.json
- @KB_HOME@/embeddings.npy
```

---

## Project instruction punch list (optional, per-project review)

For every project with a top-level `AGENTS.md` or legacy `CLAUDE.md`, audit KB integration in a separate pass (read the file, grep for `claude-knowledge` and `kb-search`). Prefer Claude-native `CLAUDE.md` in suggestions; mention `AGENTS.md` only as compatibility context. Print suggestions only — never auto-edit project files.

---

## Prune mode (not ported)

The old Claude command documented `--prune`, but the current Python scripts do not expose that flag. If cleanup is requested, run it as a separate manual workflow. The intended cleanup has two waves:

- **Wave 1** — stale (>180 days no edit) / empty (<100 chars) / scratch files. Per-file y/n prompt for deletion. Destructive.
- **Wave 2** — `Other` category audit. Per-file `[k]eep / [c]ategory / [i]ndex-only / [d]elete` prompt. Category reassignments persist to `@KB_HOME@/overrides.json` and seed future categorization.

Never auto-delete. Never auto-recategorize. The user types each decision.

---

## Verification (run before declaring done)

```python
import json, os
from pathlib import Path
kb = Path("@KB_HOME@")
h = json.loads((kb / "hashes.json").read_text())
files = [e for e in h["files"].values() if e.get("entry_type") == "file"]

# 1. No alias leak (every hash appears at most once for file entries)
from collections import Counter
hash_counts = Counter(e["hash"] for e in files)
assert all(c == 1 for c in hash_counts.values()), \
    f"alias leak: {[h for h,c in hash_counts.items() if c>1]}"

# 2. No orphan entries (every path/parent_file exists on disk)
orphans = []
for e in h["files"].values():
    p = e.get("parent_file") if e.get("entry_type") == "section" else e.get("path")
    if p and not os.path.exists(p):
        orphans.append(p)
assert not orphans, f"orphans: {orphans[:5]}"

# 3. duplicates-report.md exists
assert (kb / "duplicates-report.md").exists()

# 4. embeddings shape matches index
import numpy as np
arr = np.load(kb / "embeddings.npy")
idx = json.loads((kb / "embeddings_index.json").read_text())
assert arr.shape[0] == len(idx), f"embedding row count {arr.shape[0]} != index {len(idx)}"
```

If any assertion fires, do not declare sync complete — investigate.

---

## Notes for future maintainers

- The 1300-line v1 of this spec was 100% LLM-orchestrated, which made each run take ~22 minutes regardless of actual changes. Most of that was LLM "looking at" the 500 unchanged files via repeated tool calls. The script-driven v2 (prepare + finalize) cuts incremental runs to ~2 minutes by never enumerating unchanged files in agent context.
- All paths use `@SKILL_HOME@` / `@KB_HOME@` placeholders. Keep the installed Claude skill at `~/.claude/skills/sync-docs/SKILL.md` aligned with this generic source spec when the workflow changes.
- Canonical selection in `_pick_canonical` is a single scoring function (no ordered rules). Archive/worktree paths get -100, docs/specs get +10, recent mtime gets up to +5. Highest score wins, lex tiebreak. To change canonical preference, edit `_score_canonicality` in `scripts/prepare.py` — penalties dominate by design.
- context.md has no length cap — was a 2024 concern when context was scarce; today 1000 lines ≈ 4KB ≈ 2k tokens, trivial. If profiles+PK grow huge, the right answer is to prune dead projects, not compress.
- "Other %" is no longer a health metric. Some docs genuinely don't fit any bucket; that's fine.
