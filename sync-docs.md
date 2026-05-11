---
name: sync-docs
description: |
  Refresh the cross-project Markdown knowledge base at ~/Downloads/claude-knowledge/.
  Invoke when: user explicitly says "sync docs" / "rebuild KB" / "rescan projects" /
  "更新知识库"; OR after a substantial doc/lessons writeup in any project that should
  propagate to the central index; OR when context.md is over 7 days stale and a
  cross-project task is starting. DO NOT invoke for single-project doc edits — this
  rebuilds the full index every call and is not free.
---

You are maintaining a centralized knowledge base of all Markdown documentation across the user's projects. Follow these steps precisely.

**Modes**: parse `$ARGUMENTS` for flags before treating tokens as scan roots:

- `--prune` — after the regular sync, run an interactive cleanup pass (Wave 1: stale/empty/scratch files; Wave 2: Other-category audit). Destructive deletions require explicit y/n per file. Wave 2 reclassifications persist to `overrides.json` to seed future categorization.

Strip recognised flags from `$ARGUMENTS`; remaining tokens are scan roots.

**Scan roots**: If positional `$ARGUMENTS` are provided, treat each space-separated path as a scan root. Otherwise default to the full set:

```
/Users/cm/Downloads
/Users/cm/Documents
/Users/cm/projects
/Users/cm/code
```

Skip any root that does not exist. The point of multi-root is **content-based identity**: a file moved between roots is detected as moved (same hash → path updates) rather than deleted+new. Add new roots here whenever you start putting code somewhere new.

**Output directory**: `/Users/cm/Downloads/claude-knowledge/` (create if it does not exist).

**Pipeline overview**:

```
0  Load Previous State
1  Discover Files
2  Hash + mtime (with errors[])
3  Detect Changes (file-level)
4  Hash-exact Deduplicate
5  Read Content + Section Chunk        ← new
6  Categorize (overrides → rules)      ← honors overrides.json
7  Embed (sentence-transformers)       ← new
8  Semantic Cluster + Evolve + Conflict ← new
9  Build context.md (essentials, ≤500 lines)
10 Build registry.md (with cluster column + section ToC)
11 Write hashes.json (atomic, new schema)
12 Staleness Check
13 Report (with health metrics + errors)
14 CLAUDE.md Punch List (with kb-search hint)
15 Diff Since Last Sync (with forgotten gold + real diff)
16 Prune Mode (--prune only; persists to overrides.json)
```

A unit of indexing is an **entry**: either a file (`entry_type: "file"`) or a section within a chunk-eligible file (`entry_type: "section"`). Entries — not files — are what get categorized, embedded, clustered, and rendered.

---

## Step 0: Load Previous State

Read these if present:
- `~/Downloads/claude-knowledge/hashes.json` — previous entries (file + section), errors, cluster_index
- `~/Downloads/claude-knowledge/embeddings.npy` — previous embedding matrix (numpy float32, shape (N, 384))
- `~/Downloads/claude-knowledge/embeddings_index.json` — `{ "<entry_id>": <row>, ... }`
- `~/Downloads/claude-knowledge/overrides.json` — user category overrides from prior `--prune` runs

Missing files = fresh scan; treat all entries as new.

## Step 1: Discover Files

Use Bash to find all candidate MD files. Run `find` once per existing scan root and concatenate:

```bash
for root in <SCAN_ROOTS>; do
  [ -d "$root" ] || continue
  find "$root" -name "*.md" -type f \
    -not -path "*/node_modules/*" \
    -not -path "*/.venv/*" \
    -not -path "*/venv/*" \
    -not -path "*/.git/*" \
    -not -path "*/dist/*" \
    -not -path "*/build/*" \
    -not -path "*/.cache/*" \
    -not -path "*/__pycache__/*" \
    -not -path "*/.expo/*" \
    -not -path "*/.next/*" \
    -not -path "*/.pytest_cache/*" \
    -not -path "*/.specify/*" \
    -not -path "*/.cursor/*" \
    -not -path "*/.github/*" \
    -not -path "*/claude-knowledge/_generated/*" \
    -not -path "*/claude-knowledge/*" \
    -not -path "*/test_fixture/*" \
    -not -path "*/test_fixtures/*" \
    -not -path "*/curseforge/*" \
    -not -path "*/jre.bundle/*" \
    -not -path "*/Jre_*/*" \
    -not -path "*/legal/java.*" \
    -not -path "*/legal/jdk.*" \
    -not -path "*/legal/javafx.*" \
    -not -path "*/java-runtime-*/*" \
    -not -name "LICENSE.md" \
    -not -name "LICENCE.md" \
    -not -name "CHANGELOG.md" \
    -not -name "CONTRIBUTING.md" \
    -not -name "CODE_OF_CONDUCT.md" \
    -not -name "SECURITY.md" \
    2>/dev/null
done | sort -u
```

Excluded categories and why:
- `.specify/`, `.cursor/` — AI tool boilerplate, not real knowledge
- `.github/` — issue/PR templates, not docs
- GitHub-standard files (`LICENSE`, `CHANGELOG`, `CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY`) — boilerplate that lives in every repo
- `claude-knowledge/` itself — avoid self-indexing the knowledge base output
- `curseforge/`, `jre.bundle/`, `Jre_*/`, `legal/java.*`, `legal/jdk.*`, `legal/javafx.*`, `java-runtime-*/` — JDK / JRE third-party license docs bundled by app installers (e.g. CurseForge Minecraft launcher). Pure noise. Add similar patterns here if you discover another installer dumping legal-doc MD into a scan root.

Save the full file list to `/tmp/sync_docs_filelist.txt`.

## Step 2: Hash and Collect Metadata

Single Python script. **No silent failures** — every read exception goes into `errors[]` for the final summary:

```python
import hashlib, os, json
files = open("/tmp/sync_docs_filelist.txt").read().strip().split("\n")
result, errors = {}, []
for f in files:
    if not f:
        continue
    try:
        with open(f, "rb") as fh:
            data = fh.read()
        result[f] = {
            "hash": hashlib.md5(data).hexdigest(),
            "mtime": os.path.getmtime(f),
            "size": len(data),
        }
    except Exception as e:
        errors.append({"path": f, "error": str(e)})
json.dump({"files": result, "errors": errors},
          open("/tmp/sync_docs_hashes.json", "w"))
print(f"Hashed {len(result)} files, {len(errors)} errors")
```

Errors propagate to Step 11's `hashes.json["errors"]` and Step 13's report.

## Step 3: Detect Changes (content-based identity)

A file's identity is its **content hash**, not its path. Path is just a location label that updates when a file moves. As long as content is unchanged, the index entry persists across any move.

Compare new hashes against `hashes.json` from Step 0 (using only `entry_type: "file"` entries — sections are derived in Step 5):

- **Unchanged**: same path, same hash → reuse cached title/takeaway/category/embedding_row
- **Updated**: same path, different hash → re-process from Step 5
- **Moved**: old path no longer exists, but content hash appears at a new path → update path, preserve everything else
- **New**: hash not seen in previous index → fresh entry, process from Step 5
- **Deleted**: hash from previous index has no surviving path → drop the entry. **Cascade**: if the dropped entry was a chunk-eligible parent (`entry_type: "file"` with section children), also drop every child section whose `parent_file` equals this path. Dropped entries do not appear in the rebuilt `hashes.json`, `embeddings.npy`, `cluster_index`, `context.md`, or `registry.md` — the whole pipeline rebuilds from the surviving entry set, so deletion is fully cleaned up by construction. The `errors[]` list does NOT cause deletion (an unreadable file is recorded as an error but its previous entry is preserved, so a transient permission glitch doesn't nuke the index).

### Move detection algorithm

For each `(old_path, old_hash)` in previous file-level hashes where `old_path` is no longer in the new file list:

```
candidates = [new_path for new_path in new_files if hash(new_path) == old_hash and new_path not in previous_hashes]

if len(candidates) == 1:
    mark old_path as MOVED → candidates[0]
elif len(candidates) > 1:
    same_name = [c for c in candidates if basename(c) == basename(old_path)]
    if len(same_name) == 1:
        mark old_path as MOVED → same_name[0]
    else:
        log_ambiguous(old_path, candidates)
        mark old_path as DELETED
else:
    mark old_path as DELETED
```

**Key property**: filename is *not required* to match. A renamed-and-moved file (`mv old/notes.md new/guide.md`) is still detected as a move.

**Section move detection** is symmetric, using `section_hash` instead — Step 5.5 builds it. A user can cut-and-paste a section from one file to another and the section entry survives.

Print a one-line summary per category and, if any ambiguous moves were logged, print the full ambiguous list.

## Step 4: Deduplicate (hash-exact)

Group files by their MD5 hash. For any hash appearing more than once:

1. Pick a **canonical** path: prefer files in a project that has a CLAUDE.md (active project), then prefer shorter paths, then prefer `docs/` subdirectories.
2. Other paths with the same hash become **aliases**.

Do NOT hardcode known duplicate sets — detect all duplicates automatically from hashes.

Same-name files with different content (e.g., multiple `CLAUDE.md`) are NOT duplicates — note them in the report.

This step covers exact byte-equal duplicates. Semantic duplicates (different wording, same lesson) are handled in Step 8.

## Step 5: Read Content + Section Chunk

For each new / updated / moved file, read its content and decide whether to chunk it into sections.

### 5.1 Chunk eligibility

A file is chunk-eligible if any of these holds. Each trigger has explicit guard rails — they exist because the wrong call here either fragments a long structured doc into meaningless slivers, or buries a 30-lesson list under one diluted file-level takeaway.

**Trigger A — filename pattern (high confidence)**
- Filename matches `*lessons*`, `*pitfalls*`, `*gotchas*`, `*tips*`, `*notes*`, `*tricks*`, `*踩坑*`, `*经验*` (case-insensitive). Fires regardless of size.

**Trigger B — lessons/personal-knowledge category w/ ≥5 H2**
- Tentative category (from Step 6 filename rules) is `Engineering Lessons` or `Personal Knowledge` AND the file has ≥5 H2 headings.

**Trigger C — large, multi-section, AND independent (strict, all three required)**
- File length > 500 lines, AND
- ≥5 H2 headings, AND
- **strict independence**: ≥60% of H2 sections each contain at least one of: a fenced code block, a markdown table, an ordered/unordered list of ≥3 items, or a bold inline-rule line like `**坑**:` / `**Lesson**:`. The point: sections of a "lessons file" each stand alone; sections of an architecture/design doc cross-reference each other and lose meaning when split.

Trigger C is the dangerous one. Concretely, it should **not** fire on: spec documents (`spec-*.md`, `architecture.md`, `database.md`, `email-pipeline.md`), planning rollups (`PROGRESS.md`, `ROADMAP.md`, `midterm_presentation.md`), or any file whose first H2 is a continuation of the title (e.g. `## Overview`, `## Goals`, `## Background`) rather than an independent unit. If in doubt, default to NOT chunking — a slightly diluted file-level takeaway is recoverable; over-chunking floods registry.md with 23 sections of `## email_chunks 表` style entries that are unintelligible without their siblings.

Files that don't match get a single file-level entry. Files that match get **both** a file-level entry (skeleton, used for navigation) AND one section-level entry per H2 section.

After identifying the eligible set, drop any section shorter than 200 chars of body — those are stub headings, not lessons.

### 5.2 Batch read

**Defensive re-stat first.** Between Step 1 (find) and now, files can have disappeared if the user reorganized a folder during the sync. Before reading, stat each path and drop any that no longer exist; move those paths into the deletion set so they cascade through Step 3's logic correctly:

```python
import os
to_read = [p for p in to_read if os.path.exists(p)]
deleted.update(p for p in originally_to_read if not os.path.exists(p))
```

Without this, you end up with `hashes.json` entries pointing at deleted files (orphans), which violates verification invariant 11.

Then build a single bash invocation that emits a delimited blob for all changed paths to read:

```bash
{ for p in <new_or_updated_or_moved_paths>; do
    [ -f "$p" ] || continue
    echo "===== START $p ====="
    if <p is chunk-eligible>; then
      head -c 50000 "$p"
    else
      head -c 2000 "$p"
    fi
    echo
    echo "===== END $p ====="
  done
} > /tmp/sync_docs_content.txt
```

The inline `[ -f "$p" ] || continue` is a belt-and-braces second check in case a file vanishes between the Python stat and the shell read.

Then read `/tmp/sync_docs_content.txt` once with the Read tool — this avoids N separate Read calls for N changed files.

### 5.3 Section parsing (chunk-eligible files only)

Default split level: `##` (H2). If a file has ≤ 2 H2 headings, fall back to `###` (H3).

For each section:

- `section_anchor` = lowercased heading text, non-alphanumerics → `-`, collapsed runs of `-`, trimmed (e.g., `## Script permission lost on macOS arm64` → `script-permission-lost-on-macos-arm64`)
- `section_body` = lines from the heading until the next heading at the same or higher level (or EOF)
- `section_hash` = md5(section_body)
- `entry_id` = `section_hash`
- `entry_type` = `"section"`
- `parent_file` = the file's absolute path
- `path` (display) = `<parent_file>#<section_anchor>`

The parent file's own entry has:
- `entry_type` = `"file"`
- A skeleton `takeaway`: `"列表型聚合文档，含 N 个 section: <anchor1>, <anchor2>, ..."`
- It is **NOT rendered in context.md** (sections render instead)
- It IS shown in registry.md with a ToC linking to its sections

### 5.4 Extract title + takeaway per entry

For each entry (file or section):

- `title`: for files = first `# heading` line; for sections = the section heading text
- `takeaway`: 2-5 lines, capturing the **mechanism-level core**. What is the lesson / decision / fact a future agent needs to recognise this entry from a description that uses *different words*? More than a sentence, less than a paragraph (target 150-400 chars). Sections only summarize that section's body.

**Quality bar — mechanical first-paragraph extraction is FORBIDDEN.** The most common failure mode is taking `content.split('\n\n')[0]` and calling it done. That gives you the intro/setup paragraph (often "this section explains X" or quoted preamble) instead of the actual takeaway. The takeaway must read like a one-paragraph answer to "what would I tell a colleague who hit this in another project?"

**Implementation — single batched LLM call.** Group all entries needing extraction (new + updated files + new + updated sections), batch them into one `claude -p` subprocess invocation, and parse the JSON response. Same pattern as `measure-kb-usage.py`. Concretely:

```python
import subprocess, json
inputs = [
    {"entry_id": eid, "title": e["title"], "path": e["path"], "body": body[:4000]}
    for eid, (e, body) in entries_to_extract.items()
]
prompt = f"""You are extracting mechanism-level takeaways for a cross-project documentation index.

For each entry below, produce a 150-400 character takeaway that:
1. States the MECHANISM / decision / lesson, not the topic ("DispatchQueue.asyncAfter cannot be cancelled on view disappear and bypasses SwiftUI animation transactions" — not "this section discusses DispatchQueue").
2. Uses concrete identifiers (function names, error codes, file paths) the future search query is likely to share.
3. Includes the FIX or current understanding if the entry contains one.
4. Is self-contained — the takeaway alone tells the reader if this entry is what they need.

Strictly forbidden: "this document covers", "this section explains", any meta-description.

Output a single JSON array, one object per input entry:
[{{"entry_id": "...", "takeaway": "..."}}]

Entries:
{json.dumps(inputs, ensure_ascii=False, indent=2)}
"""
proc = subprocess.run(
    ["claude", "-p", "--model", "claude-sonnet-4-6", prompt],
    capture_output=True, text=True, timeout=600,
)
out = proc.stdout
results = json.loads(out[out.find("["):out.rfind("]")+1])
```

Batching: cap at ~30 entries per call (Sonnet handles this reliably). For larger sets, split into batches of 30 and run them with `concurrent.futures.ThreadPoolExecutor(max_workers=4)`. Total cost on a fresh full-corpus run (~360 sections + 50 changed files) is one-time and ~$1-2 in tokens — acceptable for the once-per-week sync cadence.

**Fallback when `claude -p` is unavailable** (e.g. running inside a session where subprocess CLI invocation is blocked):
1. Try to detect: `which claude` returns 0 AND `claude -p "test" --model claude-haiku-4-5` succeeds within 30s.
2. If unavailable, MARK the takeaway as `[mechanical: first-paragraph fallback]` (a literal prefix in the takeaway string), use first non-empty paragraph after the heading, and add this run to `hashes.json["pending_llm_extraction"]: [<entry_id>, ...]`. The next sync that has LLM access processes only that pending list — no full re-extract.
3. Step 13's report must surface "K entries on mechanical fallback — next sync should refill" so the user can re-run when an LLM-capable session is available.

This explicit fallback exists so a sync can still produce useful artifacts under restricted conditions, but the system tracks the debt and pays it off on the next opportunity.

Skip entries that are unchanged (file's hash matched previous; or for chunk-eligible files where individual section_hash matched previous). Reuse cached values from `hashes.json`. Also reuse if the previous takeaway is non-empty and NOT prefixed with `[mechanical:` — i.e. only re-extract previously-fallback entries when LLM is available.

### 5.5 Section change detection

If a previously-chunk-eligible file is updated, compute fresh section_hashes and compare against old:

- Same section_anchor + same section_hash → unchanged, reuse cached
- Same section_anchor + different section_hash → updated, re-extract takeaway
- New section_anchor → new entry
- Old section_anchor missing → check if section_hash appears in another file (cross-file section move) before marking deleted

## Step 6: Categorize

For each entry (file + section), assign a category and a `kind`. **Resolution order**: `overrides.json` → primary filename/path rules → secondary content fallback.

### 6.1 Overrides

If `overrides.json` contains the entry's full path (or `parent_file#anchor` for sections), use that category and skip rules. This is how the user teaches the skill its blind spots over time via `--prune` Wave 2.

**GC orphan overrides**: before reading, prune any key in `overrides.json` whose path no longer matches a current entry (file or section). Atomic-rewrite if anything was removed. This prevents `overrides.json` from accumulating dead entries when files are deleted.

### 6.2 Primary rules (filename + path)

Sections inherit the rules of their parent_file path, but the filename match runs against `parent_file` only. First match wins.

| Category | Match Rules |
|---|---|
| Project Profiles | Filename is `CLAUDE.md` or `README.md` AND file is at project root (depth ≤ 2 from scan root). Sections of these are not categorized as profiles — they fall through. |
| Personal Knowledge | Path contains `claude-knowledge/guides/`, `/notes/`, `/methodology/`, `/playbooks/`, `/guides/`; or filename matches `*hygiene*`, `*playbook*`, `*conventions*`, `*-style*`, `*manifesto*`, `*-principles*`, `*-rules*`, `*-protocol*`, `*-methodology*`, `*-handbook*`, `*-style-guide*`, `*-checklist*` |
| Engineering Lessons | Path contains `/experience/`, `/lessons/`, `/postmortem/`, `/learnings/`, `/retros/`, `/debug/`, `/troubleshooting/`; or filename matches `*lessons*`, `*pitfalls*`, `*bugs*`, `*postmortem*`, `*gotchas*`, `*tips*`, `*tricks*`, `*踩坑*`, `*经验*`, `*lessons-learned*`, `*-debugging*`, `*-troubleshoot*`, `*observations*` |
| Architecture | Path contains `/architecture/`, `/design/`, `/system-design/`, `/data-model/`; or filename matches `ARCHITECTURE*`, `*-architecture*`, `*-design.md`, `*-data-model*`, `architecture.md`, `design.md`, `system-design.md`, `data-flow*` |
| Product Specs | Path contains `/specs/`, `/requirements/`, `/prd/`, `/product/`, `/features/`, `/user-stories/`; or filename matches `*PRD*`, `*-spec*`, `spec-*.md`, `*usecase*`, `*journey*`, `*user-story*`, `*-requirements*`, `*-features*`, `feature-*.md` |
| Security | Path contains `/security/`, `/audit/`, `/threat-model/`; or filename matches `*security-*`, `*-security*`, `*audit*`, `*threat-model*`, `*permission*`, `*authz*`, `*authn*` |
| Dev Guides | Path contains `/docs/requirements/`, `/docs/dev/`, `/dev-guide/`, `/setup/`, `/getting-started/`, `/onboarding/`, `/cookbook/`, `/howto/`; or filename matches `DEV_GUIDE*`, `INITIALIZE*`, `development.md`, `SERVER.md`, `SETUP*`, `INSTALL*`, `getting-started*`, `dev-*.md`, `*-cookbook*`, `*-howto*`, `*-recipe*`, `*-walkthrough*` |
| Planning | Path contains `/planning/`, `/roadmap/`, `/milestones/`; or filename matches `*ROADMAP*`, `*PROGRESS*`, `*-plan.md`, `plan-*.md`, `*-milestone*`, `TODO*`, `*backlog*`, `*next-steps*`, `*phase-*.md`, `*-progress*` |
| Other | Everything else (falls to secondary) |

Notes:
- All match patterns are **case-insensitive** unless they use ALL-CAPS literally (which still matches case-insensitive — the all-caps is just convention).
- `/docs/` alone is **not** a category trigger — too generic. The subdirectory after `docs/` (e.g. `docs/specs/`, `docs/design/`) determines category.
- A section inherits its parent file's path-based triggers but classifies on its own title + body for the keyword-based filename triggers (so a section called `## Authentication audit` inside `docs/design/main.md` still classifies as Security).

### 6.3 Secondary fallback (content-based)

Only invoked if 6.2 returned `Other`. Lowercase-match against `title + " " + takeaway`. First match wins (table order = priority — Engineering Lessons matched before Architecture because the former is more specific).

| Promoted to | Trigger keywords (any substring match) |
|---|---|
| Engineering Lessons | `lesson`, `lesson learned`, `pitfall`, `bug`, `踩坑`, `教训`, `坑：`, `坑:`, `postmortem`, `post-mortem`, `regression`, `gotcha`, `mistake`, `root cause`, `RCA`, `incident`, `outage`, `we got burned`, `we found`, `breaks when`, `surprising`, `quirk`, `caveat`, `the fix is`, `the fix was` |
| Personal Knowledge | `playbook`, `manifesto`, `methodology`, `convention`, `规范`, `心得`, `principle`, `philosophy`, `style guide`, `checklist`, `rules of thumb`, `recipe`, `pattern library`, `how we`, `our approach`, `way of working` |
| Architecture | `architecture`, `system design`, `data flow`, `data model`, `component diagram`, `service boundary`, `tech stack`, `module structure`, `sequence diagram`, `layered`, `microservice`, `monolith`, `event-driven`, `data pipeline`, `database schema` |
| Product Specs | `user journey`, `acceptance criteria`, `feature spec`, `prd`, `product requirement`, `user story`, `as a user`, `given/when/then`, `gherkin`, `success metric`, `out of scope`, `in scope`, `flow:`, `user flow` |
| Dev Guides | `setup`, `getting started`, `how to`, `step-by-step`, `walkthrough`, `tutorial`, `cookbook`, `recipe`, `to run this`, `to develop`, `to install`, `prerequisites`, `environment variable`, `npm run`, `pnpm`, `pip install`, `yarn` |
| Security | `cve`, `vulnerability`, `threat model`, `attacker`, `injection`, `xss`, `csrf`, `auth bypass`, `permission check`, `principle of least`, `secret`, `credential`, `oauth`, `jwt`, `token` |
| Planning | `roadmap`, `milestone`, `q1 plan`, `q2 plan`, `quarter plan`, `progress report`, `状态更新`, `下一步`, `next phase`, `phase 1`, `phase 2`, `mvp scope`, `release plan`, `cutover`, `migration plan`, `timeline`, `eta` |
| Other (kept) | none of the above |

**Target health metric**: after these rules, `Other` should be ≤15% of canonical entries. If a run reports >25%, surface the top-20 paths still in `Other` in the report so the user can decide whether to add more rules or use `--prune` to recategorize them (which persists to `overrides.json`).

### 6.4 Kind classification

For entries in `Engineering Lessons` or `Personal Knowledge`, also assign a `kind`:

| Kind | Heuristic (any match in takeaway) |
|---|---|
| `evergreen` | Names a framework / language / API behavior / mechanism-level pitfall (e.g. "DispatchQueue", "useEffect cleanup", "FastAPI dependency injection", "race condition", "memory leak") |
| `project-specific` | Names project-internal choices: "we use", "我们用", "我们决定", project name + library, internal endpoint names, internal table/model names |
| `unknown` | None clear |

`evergreen` lessons travel between projects; `project-specific` lessons stay in `registry.md` (still indexed) but are **excluded from `context.md`** to keep the cross-project view clean.

Sections classify on their own takeaway, not their parent's.

## Step 7: Embed

Compute embeddings of takeaways for new + updated entries (both files and sections).

**Venv bootstrap**: modern Homebrew/system Pythons reject global `pip install`. Create a dedicated venv at `~/Downloads/claude-knowledge/.venv` on first run and use its python for all embedding work. The same venv is used by `kb-search.py`.

```bash
KB=~/Downloads/claude-knowledge
mkdir -p "$KB"
if [ ! -x "$KB/.venv/bin/python" ]; then
  python3 -m venv "$KB/.venv"
  "$KB/.venv/bin/pip" install --quiet --upgrade pip
  "$KB/.venv/bin/pip" install --quiet sentence-transformers numpy
fi
```

Then run the encoding step using the venv's python (write the script to a temp file and invoke):

```python
# /tmp/sync_docs_embed.py — invoke with: $KB/.venv/bin/python /tmp/sync_docs_embed.py
import json, os
import numpy as np
from sentence_transformers import SentenceTransformer

KB = os.path.expanduser("~/Downloads/claude-knowledge")
model = SentenceTransformer("all-MiniLM-L6-v2")  # 384-dim, CPU-friendly

# Load previous embeddings + index.
old_idx_path = f"{KB}/embeddings_index.json"
old_npy_path = f"{KB}/embeddings.npy"
if os.path.exists(old_idx_path) and os.path.exists(old_npy_path):
    old_idx = json.load(open(old_idx_path))
    old_arr = np.load(old_npy_path)
else:
    old_idx, old_arr = {}, np.zeros((0, 384), dtype=np.float32)

# entries: list of (entry_id, takeaway) — supplied by Step 5/6
# changed: subset that needs re-encoding (new/updated)
to_encode = [(eid, t) for eid, t in entries if eid not in old_idx or eid in changed]
new_vecs = model.encode([t for _, t in to_encode], show_progress_bar=False)

# Build new array: reuse old rows for unchanged, append for new.
new_idx, rows = {}, []
for eid, _ in entries:
    if eid in old_idx and eid not in changed:
        new_idx[eid] = len(rows)
        rows.append(old_arr[old_idx[eid]])
    else:
        # find this eid in to_encode
        i = next(i for i, (e, _) in enumerate(to_encode) if e == eid)
        new_idx[eid] = len(rows)
        rows.append(new_vecs[i])

new_arr = np.vstack(rows).astype(np.float32) if rows else np.zeros((0, 384), dtype=np.float32)

# Atomic write. np.save appends .npy if missing — pass a path that already has it.
tmp_npy = f"{KB}/embeddings.tmp.npy"
tmp_idx = f"{KB}/embeddings_index.json.tmp"
np.save(tmp_npy, new_arr)
json.dump(new_idx, open(tmp_idx, "w"))
os.rename(tmp_npy, f"{KB}/embeddings.npy")
os.rename(tmp_idx, f"{KB}/embeddings_index.json")
```

Each entry in `hashes.json` gets `embedding_row: <int>` matching its position in the new array.

Token cost: zero. CPU cost: ~0.5 ms per takeaway. Even 1000 fresh entries take seconds.

## Step 8: Semantic Cluster + Evolve + Conflict

Cluster semantically similar entries, synthesize evolution-trail takeaways for clusters, and detect contradictions.

**Important**: every Python snippet in this step that imports `numpy` must be invoked via the KB venv (`~/Downloads/claude-knowledge/.venv/bin/python /tmp/<script>.py`), not the system `python3`. Same venv as Step 7.

### 8.1 Cluster within categories

For each category (excluding `Project Profiles`, where every entry is unique by construction):

1. Collect category entries; fetch their embedding rows.
2. Compute pairwise cosine similarity.
3. Build a graph: edge between `i, j` if `cos(i, j) ≥ 0.75`. (Empirically: with `all-MiniLM-L6-v2`, true semantic duplicates expressed in different wording land at cosine ~0.68–0.81; clearly different topics stay below 0.20. There's a wide gap, so 0.75 is comfortable. Lower if you find clusters being missed; raise if false positives appear.)
4. Connected components of size ≥ 2 are clusters; singletons are unaffected.

```python
import numpy as np
def cluster(emb, threshold=0.75):
    # emb: (N, 384). Normalize so dot product == cosine.
    n = len(emb)
    if n < 2:
        return []
    norms = np.linalg.norm(emb, axis=1, keepdims=True) + 1e-12
    emb_n = emb / norms
    sims = emb_n @ emb_n.T
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for i in range(n):
        for j in range(i+1, n):
            if sims[i, j] >= threshold:
                a, b = find(i), find(j)
                if a != b: parent[a] = b
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) >= 2]
```

For each cluster:
- `cluster_id` = `md5(",".join(sorted(member_entry_ids)))` (stable across runs while membership is stable)
- `canonical` = member with newest mtime (tiebreak: longest takeaway)
- Other members: `cluster_member_of: <canonical_entry_id>`

### 8.2 Cache check

If `cluster_id` exists in old `hashes.json["cluster_index"]` AND its canonical's `synthesized_takeaway` is non-empty → reuse, skip 8.3 for this cluster.

### 8.3 LLM batch synthesis (mandatory; structured fallback if blocked)

For all uncached clusters, batch into a **single** `claude -p` call (no per-cluster overhead). This is not optional — the threshold-based cluster from 8.1 captures candidates, but only the LLM cohesion check separates real semantic dedup from "same domain, different mechanism" noise. Empirically, ~25% of clusters at threshold 0.75 split on LLM review. Skipping this step pollutes context.md and registry.md with bogus evolution trails.

```python
import subprocess, json
prompt = f"""You are auditing semantic clusters of cross-project documentation entries.

For each cluster below, decide:
1. cohesion: do all members truly describe the same MECHANISM-level lesson? (Same surface topic with different mechanisms = NOT cohesive.) Output a confidence 0.0-1.0.
2. members_to_split: list any member paths that should be removed from the cluster (low cohesion).
3. synthesized_takeaway: for the remaining cohesive members, synthesize an evolution-trail takeaway:

<3-5 lines stating the latest understanding>

Evolution:
- YYYY-MM (project-name): <what this version contributed>
- YYYY-MM (project-name): <what this version refined or corrected>
...

Output a single JSON array, one object per cluster:
[{{"cluster_id": "...", "confidence": 0.0-1.0, "members_to_split": ["path", ...], "synthesized_takeaway": "..."}}]

Clusters:
{json.dumps(clusters_input, ensure_ascii=False, indent=2)}
"""
proc = subprocess.run(["claude", "-p", "--model", "claude-sonnet-4-6", prompt],
                      capture_output=True, text=True, timeout=300)
results = json.loads(proc.stdout[proc.stdout.find("["):proc.stdout.rfind("]")+1])
```

Where `clusters_input[i]` is:
```json
{
  "cluster_id": "abc123",
  "members": [
    {"path": "...", "mtime_iso": "2024-08-12", "project": "amigo", "takeaway": "..."},
    ...
  ]
}
```

Confidence < 0.7 → split: pull those members out, they become standalone (no `cluster_id`). Members in `members_to_split` likewise.

If a cluster is split such that fewer than 2 members remain, dissolve it entirely.

**Heuristic pre-filter** (run before the LLM call to spare obvious noise from costing tokens):
- Two members from the **same parent_file** (e.g. two sections of `vigil/docs/architecture.md`) — drop the edge. Adjacent sections of one doc rarely describe the same mechanism.
- One member is the file-level entry whose ToC names the other section's anchor — drop the edge (artificial similarity from anchor names appearing in the ToC).

These two filters alone removed ~30% of cluster noise on the test corpus without an LLM call.

**Fallback when `claude -p` is unavailable**: track the cluster_ids in `hashes.json["pending_llm_validation"]: [<cluster_id>, ...]`. Mark canonical's `synthesized_takeaway` with prefix `[mechanical: members enumerated, awaiting LLM cohesion check]` followed by the timeline-only template (no synthesis). Step 13's report must surface "K clusters pending LLM validation".

### 8.4 Conflict detection (mandatory; same fallback pattern)

After clustering, identify contradictions among `kind: evergreen` entries (across all categories). One LLM batch call:

```python
prompt = f"""Among these N evergreen lessons, identify any pair that makes CONTRADICTORY claims about the same mechanism or API. Surface-level disagreement (e.g., different style preferences) does NOT count — only operational contradictions (e.g., "always use X" vs "never use X" for the same scenario).

Output JSON: [{{"a_path": "...", "b_path": "...", "mechanism": "...", "reason": "..."}}, ...]
Empty array if no genuine conflicts.

Lessons:
{json.dumps(evergreen_input, ensure_ascii=False, indent=2)}
"""
```

To keep input manageable: pre-filter to pairs whose cosine similarity is in `[0.55, 0.74]` — the band where two lessons are about the same thing but didn't quite cluster. Lower than 0.55 → unrelated; ≥ 0.75 → already clustered (not a conflict, a duplicate). This collapses an O(N²) check into O(K) where K is typically <100.

Conflicts go into Step 13's `### Conflicts (need adjudication)` section.

**Fallback when blocked**: record `hashes.json["conflicts"] = []` AND `hashes.json["conflict_check_skipped": true]` so the next sync's Step 13 reports "conflict check skipped this run — `conflicts: []` is unverified".

### 8.5 Persist to hashes.json

- Each cluster member: set `cluster_id`. Non-canonical members get `cluster_member_of: <canonical_entry_id>`.
- Canonical only: `synthesized_takeaway` field set to LLM output.
- Root: `cluster_index: { "<cluster_id>": ["<entry_id>", ...] }`.
- Conflicts list: `conflicts: [...]` at root.
- Root: `pending_llm_validation: [<cluster_id>, ...]` if 8.3 fell back.
- Root: `conflict_check_skipped: true` if 8.4 fell back.

## Step 9: Build context.md (Essentials View)

Goal: ≤ 500 lines, cross-session-loaded essentials. Deep queries go through `kb-search.py`.

### 9.1 Inclusion rules

**Always include**:
- **Project Profiles**: every CLAUDE.md / README.md project root entry. Detect dead projects — `git -C <project> log -1 --format=%ct` returning > 365 days ago — and prefix the rendered title with `[archived]`. Place archived entries last within the section.
- **Personal Knowledge**: full set.

**Per other category**, top-N entries by score:
```
score = recency_weight × cross_project_weight
recency_weight = exp(-days_since_mtime / 365)
cross_project_weight = 1.0 if kind == "evergreen" else 0.3
```

Defaults:
- `Engineering Lessons`, `Architecture`, `Dev Guides` → top 5
- `Product Specs`, `Security`, `Planning` → top 3
- `Other` → excluded entirely from context.md

### 9.2 Render rules

- **Skip non-canonical cluster members** (`cluster_member_of != null`) — only canonical renders.
- **For canonical with `synthesized_takeaway`**: render that synthesized text (with the Evolution: trail), not the original takeaway.
- **For sections**: source line shows `path/file.md#anchor`.
- **Skip parent files of sections** (file-level entries that are ToC-only).
- **Skip `kind: project-specific`** entries.

### 9.3 Format

```markdown
# Cross-Project Knowledge Base
> Auto-generated by /sync-docs on YYYY-MM-DD HH:MM
> Scan roots: <ROOT_1>, <ROOT_2>, ...
> Files indexed: N unique (M entries — F files + S sections)
> For deep queries: `python3 ~/Downloads/sync-docs/kb-search.py "your query"`

---

## Project Profiles

### CLAUDE.md — project-name
> Source: relative/path/to/CLAUDE.md

3-5 line takeaway focused on Architecture + Hard Rules.

---

### [archived] CLAUDE.md — old-project
> Source: ...

(dead projects appear last in this section, with [archived] prefix)

---

## Personal Knowledge
(full set, no truncation)

---

## Engineering Lessons (top 5 by score)

### [Title] — project-name
> Source: relative/path/to/file.md#anchor    ← when section
> Cluster: ab12cd (evolved across 3 projects) ← when canonical

<synthesized takeaway with Evolution: trail, OR original takeaway>

---

## Architecture (top 5)
## Product Specs (top 3)
## Security (top 3)
## Dev Guides (top 5)
## Planning (top 3)
```

### 9.4 Cap enforcement (resolved soft/hard tiers)

The previous version of this section had a contradiction: "≤500 lines" AND "never trim Profiles + Personal Knowledge" — when these two categories alone exceed 500 lines (45 profiles × ~6 lines + 37 personal × ~6 lines = ~500), the cap was unsatisfiable. Resolved with explicit two-tier caps:

- **Soft cap: 500 lines.** Below this, render every category at its default top-N. Goal of the soft cap: any agent loading this file as `@`-import sees the essentials in <1k tokens of context budget.
- **Hard cap: 750 lines.** Maximum allowed total. If renders exceed this, apply the reductions below in order.
- **Profiles + Personal Knowledge are protected** — never trimmed, but their *per-entry body* gets compact-rendered if the soft cap is busted.

Render in this order: Project Profiles → Personal Knowledge → Engineering Lessons → Architecture → Dev Guides → Product Specs → Security → Planning.

**Adaptive compression order** (applied in this order until under the cap):
1. Drop `Planning` section.
2. Drop `Security` section.
3. Drop `Product Specs` section.
4. Drop `Dev Guides` section.
5. Drop `Architecture` section.
6. Reduce `Engineering Lessons` top-N from 5 → 3, then 3 → 1.
7. Drop `Engineering Lessons` section.
8. **If still over hard cap (750)**: switch Profiles + Personal Knowledge to **compact render** — one heading line + one ≤80-char one-liner per entry, no separator, no source URL. Compact render reduces ~6 lines/entry to ~2 lines/entry. Full-form versions of those entries remain in `registry.md`.
9. **If still over** (would only happen with >300 protected entries): truncate body to 40 chars + `…`. This is the absolute floor; any further reduction loses entry identity.

Footer:
```
---
> Essentials view. Soft cap 500 / hard cap 750. Full index: registry.md. Semantic search: kb-search.py.
```

After render, the report (Step 13) must surface:
- `actual_lines / soft_cap / hard_cap`
- compressions applied (e.g. "dropped Planning, Security, Product Specs to fit hard cap")
- whether compact render kicked in for Profiles/Personal

So you can see when the doc is straining against the cap and prune projects from the index.

## Step 10: Build registry.md

```markdown
# Documentation Registry
> Last synced: YYYY-MM-DD HH:MM
> Scan roots: ...
> For semantic queries, prefer `kb-search.py` over grep on this file.

## Summary
- Total files scanned: N
- Unique entries: M (F files + S sections)
- Hash-exact duplicate sets: D
- Semantic clusters: C
- Moved entries (since last sync): K
- Ambiguous moves needing review: A
- Read errors: E

## Index

(Categories in order: Project Profiles → Personal Knowledge → Engineering Lessons → Architecture → Product Specs → Security → Dev Guides → Planning → Other)

### Project Profiles

| Entry | Project | Description | Hash | Cluster |
|-------|---------|-------------|------|---------|
| CLAUDE.md | proj-name | one-line | abc123 | — |

### Engineering Lessons

| Entry | Project | Description | Hash | Cluster |
|-------|---------|-------------|------|---------|
| LessonsLearned.md#script-perm-arm64 | amigo | macOS arm64 chmod loss | def456 | ab12cd |
| LessonsLearned.md#dispatch-reentry | amigo | DispatchQueue reentry | 789abc | — |

(sections appear with their #anchor; cluster shows short cluster_id when in a cluster)

### Section Tables of Contents

For each chunk-eligible parent file, list its sections:

#### amigo/LessonsLearned.md
- #script-permission-lost-on-macos-arm64 → Engineering Lessons
- #dispatchqueue-reentry-bug → Engineering Lessons
- #swiftdata-migration-pitfall → Engineering Lessons (cluster ab12cd)
- ...

## By Task Trigger

(Indexed by takeaway-text keyword match. Files can appear in multiple buckets.)

### UI / 设计系统
| Entry | Project | Hash |
|-------|---------|------|
| ... |

### API contract / 前后端对接
### Bug 排查 / 经验教训
### 数据迁移 / DB
### 启动 / 部署 / CI
### 平台特定

## Semantic Clusters (演化簇)

| Cluster ID | Canonical | Members | Synthesized Topic |
|------------|-----------|---------|-------------------|
| ab12cd | amigo/LessonsLearned.md#swiftdata-migration | 3 | SwiftData schema migration in production |

## Duplicate Sets (hash-exact)

| Canonical | Aliases | Hash |

## Same-Name Files (different content)

| Filename | Paths |
```

## Step 11: Write hashes.json (atomic)

### Schema

```json
{
  "scan_roots": ["..."],
  "scanned_at": "<ISO timestamp>",
  "errors": [
    {"path": "/abs/file.md", "error": "Permission denied"}
  ],
  "cluster_index": {
    "<cluster_id>": ["<entry_id>", "<entry_id>", ...]
  },
  "conflicts": [
    {"a_path": "...", "b_path": "...", "mechanism": "...", "reason": "..."}
  ],
  "files": {
    "<entry_id>": {
      "path": "/abs/path/file.md",
      "hash": "<md5 of full file>",
      "mtime": 1714000000.0,
      "size": 12345,

      "entry_type": "file",
      "parent_file": null,
      "section_anchor": null,
      "section_hash": null,

      "category": "Engineering Lessons",
      "kind": "evergreen",
      "title": "...",
      "takeaway": "...",
      "synthesized_takeaway": null,

      "cluster_id": null,
      "cluster_member_of": null,
      "embedding_row": 42,

      "canonical": true,
      "alias_of": null,
      "previous_paths": [],
      "last_verified_at": null
    },
    "<section_entry_id>": {
      "path": "/abs/path/file.md#script-perm-arm64",
      "hash": "<section_hash>",
      "mtime": 1714000000.0,
      "size": 1023,

      "entry_type": "section",
      "parent_file": "/abs/path/file.md",
      "section_anchor": "script-perm-arm64",
      "section_hash": "<md5>",

      "category": "Engineering Lessons",
      "kind": "evergreen",
      "title": "Script permission lost on macOS arm64",
      "takeaway": "...",
      "synthesized_takeaway": "...",

      "cluster_id": "ab12cd34",
      "cluster_member_of": null,
      "embedding_row": 97,

      "canonical": true,
      "alias_of": null,
      "previous_paths": [],
      "last_verified_at": null
    }
  }
}
```

### Atomic write

For `hashes.json`, `context.md`, `registry.md`, `embeddings.npy`, `embeddings_index.json`, and `overrides.json`:

```python
import os, json, tempfile
def atomic_write_json(path, obj):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path)+".",
                               dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.rename(tmp, path)

def atomic_write_text(path, text):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path)+".",
                               dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.rename(tmp, path)
```

If sync is killed mid-write, the previous canonical files stay intact.

### Backwards compat

If old `hashes.json` lacks the new fields, treat them as default:
- `entry_type`: `"file"` (everything was file-level pre-upgrade)
- `parent_file`, `section_anchor`, `section_hash`: `null`
- `cluster_id`, `cluster_member_of`, `synthesized_takeaway`: `null`
- `embedding_row`: missing → entry needs encoding next run

Old `scan_root` (singular) → `scan_roots: [scan_root]`. No migration script needed; the next write produces the new schema.

## Step 12: Staleness Check

Using mtime collected in Step 2, identify files unmodified for 90+ days. Exclude aliases. Group by project. Reuse for Step 13 and Step 15's "forgotten gold".

## Step 13: Report to User

```
## /sync-docs Complete

Scan roots: ~/Downloads, ~/Documents, ...
Files found: N | Unique entries: M (F files + S sections) | Duplicates: D

### Changes since last sync
- New: N entries
- Updated: N entries
- Moved: N entries
- Sections added/removed: +N / -N
- Ambiguous moves: N
- Deleted: N entries
- Unchanged: N entries

### Errors (read failures)
N files could not be read (see hashes.json["errors"]):
- /path/perm-denied.md — Permission denied
- ...

### KB Health
- Engineering Lessons: K evergreen / J project-specific (target ≥60% evergreen — actual XX%)
- Median takeaway length: X chars (target 150-400)
- Other category: Y entries (Z%) — high % suggests categorization rules need tuning
- Semantic clusters: C clusters covering M entries

### Conflicts (need adjudication)
⚠ Same mechanism, opposing claims:
- proj-A — `path/a.md` says "always X"
  vs
  proj-B — `path/b.md` says "never X"
  reason: <LLM explanation>

(omit section if no conflicts)

### Ambiguous Moves
(only shown if non-zero)

### Duplicate Sets Found (hash-exact)

### Category Breakdown
| Category | Files | Sections |
|----------|-------|----------|
| Project Profiles | N | 0 |
| ... |

### Stale Files (not modified in 90+ days)

### Output
- ~/Downloads/claude-knowledge/registry.md
- ~/Downloads/claude-knowledge/context.md
- ~/Downloads/claude-knowledge/hashes.json
- ~/Downloads/claude-knowledge/embeddings.npy
- ~/Downloads/claude-knowledge/embeddings_index.json
```

If first run, skip "Changes since last sync" and say "First scan — all entries are new."

## Step 14: CLAUDE.md Punch List

For every project that contains a top-level `CLAUDE.md`, audit KB integration. **Do NOT auto-edit project CLAUDE.md files** — only print suggestions.

For each `<project>/CLAUDE.md`:

1. Read the file (≤8000 chars).
2. Check substring presence of `claude-knowledge` and `kb-search`.
3. Path validity:
   - For each `~/Downloads/claude-knowledge/...` path referenced:
     - Path exists in current `hashes.json` → ✓
     - In some entry's `previous_paths` → ⚠ moved (report new canonical)
     - Gone entirely → ✗ deleted
4. Framing:
   - "every 5 conversations" or similar → ⚠ recommend scenario-trigger pattern
   - References `context.md` but not `kb-search.py` → ⚠ recommend adding semantic search

Output:

```
## CLAUDE.md Suggestions (manual review)

EmailDigest/CLAUDE.md
  ✓ References KB with scenario triggers — looks current
  ⚠ Doesn't use kb-search.py for deep queries
  → suggested addition:
    For task-specific lesson lookup:
    `python3 ~/Downloads/sync-docs/kb-search.py "<task keywords>"`

vigil/CLAUDE.md
  ⚠ Only references context.md once with "every 5 conversations" boilerplate
  → suggested replacement: see CLAUDE-TEMPLATE.md "KB 使用协议" section
  → also add kb-search.py snippet (above)

capstone/CLAUDE.md
  ✗ No KB reference at all — KB has N entries that match this project's stack
  → easiest fix: in that project run /kb-integrate (it auto-applies the latest template)
  → or paste manually:
    @~/Downloads/claude-knowledge/context.md          (at top, auto-loads 500-line essentials)
    
    ## KB 使用协议（跨项目知识库）
    (full section from ~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md)
```

The recommended phrase to surface is **"run /kb-integrate in that project"** — it reads the current template, audits the project CLAUDE.md, and applies a diff after user approval. Manual paste is the fallback for users who don't have /kb-integrate installed.

## Step 15: Diff Since Last Sync

```
## What's new since last sync

### New evergreen lessons (cross-project value)
- <project> — <title> | takeaway snippet (≤120 chars)

### Updated entries with substantive change
(see "substantive" definition below)

### New semantic clusters formed
- cluster ab12cd: 3 entries about "SwiftData schema migration"
  canonical: amigo/LessonsLearned.md#swiftdata-migration

### Forgotten gold (high-value, untouched, unreferenced)
Lessons that are evergreen, mtime > 90 days, AND not referenced from any current CLAUDE.md:

- <project>/<file>#<anchor> — last touched YYYY-MM-DD
  takeaway: ...
  why surfaced: this lesson hasn't been read in N days but it's still relevant to <inferred stack>

### Newly stale (>180 days untouched)

### Just-moved entries (path changed, content same)
- <old> → <new>
```

### Substantive-change heuristic (real diff)

Replace the old `len(diff) > 30` with token-level `difflib`:

```python
import difflib

def substantive(old_takeaway: str, new_takeaway: str) -> bool:
    old_tok = old_takeaway.split()
    new_tok = new_takeaway.split()
    sm = difflib.SequenceMatcher(None, old_tok, new_tok)
    changed_tokens = sum(
        max(i2-i1, j2-j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != 'equal'
    )
    return changed_tokens >= 5 or sm.ratio() < 0.7
```

This catches "rewrote one sentence" but ignores "fixed two typos".

### Forgotten-gold algorithm

```python
import datetime, os, re
def forgotten_gold(hashes, claude_md_paths, days=90):
    referenced_paths = set()
    referenced_anchors = set()
    for p in claude_md_paths:
        try:
            txt = open(p).read()
        except: continue
        # Match KB paths (file or file#anchor)
        for m in re.finditer(r'claude-knowledge/[^\s)\'"`]+', txt):
            referenced_paths.add(m.group(0))
        for m in re.finditer(r'(\S+\.md)#([\w-]+)', txt):
            referenced_anchors.add((m.group(1), m.group(2)))

    now = datetime.datetime.now().timestamp()
    out = []
    for eid, e in hashes["files"].items():
        if e.get("kind") != "evergreen": continue
        days_old = (now - e["mtime"]) / 86400
        if days_old < days: continue
        # not referenced from any CLAUDE.md
        path_match = any(e["path"].endswith(p.split("/")[-1]) for p in referenced_paths)
        anchor_match = (e.get("entry_type") == "section" and
                        (e["parent_file"].split("/")[-1], e["section_anchor"]) in referenced_anchors)
        if path_match or anchor_match: continue
        out.append((days_old, e))
    return sorted(out, reverse=True)[:10]
```

## Step 16: Prune Mode (only if `--prune`)

After all the above, if `--prune` is in `$ARGUMENTS`, run two interactive cleanup waves. **Always confirm per file/entry before deletion.**

### Wave 1: Auto-flagged garbage

(unchanged from previous spec — stale + scratch + empty + dead-project alias)

### Wave 2: Other-category audit

For each `category: Other` entry (file or section):

```
[Other] /Users/cm/Downloads/foo/notes.md
  Title: ...
  Takeaway: ...
  Action [r]ecategorize / [i]ndex-only / [d]elete / [k]eep / [s]kip-rest:
```

- `r` — prompt for new category (one-letter shortcut). **Persist to `overrides.json`** so future syncs honor it:
  ```json
  {
    "/Users/cm/Downloads/foo/notes.md": "Engineering Lessons",
    "/Users/cm/Downloads/bar/big.md#some-anchor": "Architecture"
  }
  ```
  (Atomic write.)
- `i` — set `index_only: true` in `hashes.json` (file stays in registry.md, skipped by context.md)
- `d` — delete the file
- `k` — leave as is

### Audit log

Write all decisions to `~/Downloads/claude-knowledge/logs/prune-decisions-YYYYMMDD.md`. Format unchanged.

### Re-render after prune

If any deletes or recategorizations happened, re-run Steps 6 → 11 (no need to re-embed unchanged entries; `overrides.json` is honored automatically by Step 6).

---

## Verification

After writing all files, verify:

1. `wc -l` on `context.md` is ≤ 500
2. `python3 -c "import json; json.load(open('/Users/cm/Downloads/claude-knowledge/hashes.json'))"` passes
3. `python3 -c "import numpy; arr = numpy.load('/Users/cm/Downloads/claude-knowledge/embeddings.npy'); print(arr.shape)"` shows row count == count of entries with `embedding_row` set
4. Every entry has the new fields (`entry_type`, `embedding_row` for non-error files)
5. `context.md` has no entry whose `cluster_member_of != null` or `kind == "project-specific"`
6. `kb-search.py "test"` returns at least one result and doesn't crash
7. If `--prune` ran with deletions/recategorizations: re-rendered `context.md` / `registry.md` reflect post-prune state
8. `hashes.json["errors"]` correctly records any unreadable files
9. `hashes.json["cluster_index"]` matches: every cluster_id has its members listed, and every member's `cluster_id` field matches its key in `cluster_index`
10. No `.tmp` files left behind in `~/Downloads/claude-knowledge/`
11. **Deletion correctness**: every `entry_id` in `hashes.json["files"]` corresponds to a real file (or section of a real file) on disk. No orphan rows in `embeddings.npy`. No orphan keys in `overrides.json` (the GC pass in Step 6.1 must have run). Test by deleting a known file and re-running `/sync-docs` — it should disappear from all three artifacts plus `context.md` / `registry.md` in a single run.
