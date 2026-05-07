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

- `--prune` — after the regular sync, run an interactive cleanup pass (Wave 1: stale/empty/scratch files; Wave 2: Other-category audit). Destructive deletions require explicit y/n per file.

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

---

## Step 0: Load Previous State

Read `/Users/cm/Downloads/claude-knowledge/hashes.json` if it exists. This contains the previous scan's file hashes for change detection. If it does not exist, this is a fresh scan — treat all files as new.

## Step 1: Discover Files

Use the Bash tool to find all candidate MD files. Run `find` once per existing scan root and concatenate:

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

Save the full file list to `/tmp/sync_docs_filelist.txt`.

## Step 2: Hash and Collect Metadata

Use a single Python script to hash all files AND collect their mtime. This avoids separate `md5 -r` batches:

```python
import hashlib, os, json
files = open("/tmp/sync_docs_filelist.txt").read().strip().split("\n")
result = {}
for f in files:
    try:
        with open(f, "rb") as fh:
            result[f] = {
                "hash": hashlib.md5(fh.read()).hexdigest(),
                "mtime": os.path.getmtime(f)
            }
    except:
        pass
json.dump(result, open("/tmp/sync_docs_hashes.json", "w"))
print(f"Hashed {len(result)} files")
```

First write the file list to `/tmp/sync_docs_filelist.txt`, then run the script.

## Step 3: Detect Changes (content-based identity)

A file's identity is its **content hash**, not its path. Path is just a location label that updates when a file moves. As long as content is unchanged, the index entry persists across any move.

Compare new hashes against `hashes.json` from Step 0:

- **Unchanged**: same path, same hash → reuse cached title/takeaway
- **Updated**: same path, different hash → re-read in Step 6
- **Moved**: old path no longer exists, but content hash appears at a new path → update path, preserve title/takeaway
- **New**: hash not seen in previous index → fresh entry, read in Step 6
- **Deleted**: hash from previous index has no surviving path → drop entry

### Move detection algorithm

For each `(old_path, old_hash)` in previous hashes where `old_path` is no longer in the new file list:

```
candidates = [new_path for new_path in new_files if hash(new_path) == old_hash and new_path not in previous_hashes]

if len(candidates) == 1:
    # Unambiguous move — content is unique, found exactly one new home
    mark old_path as MOVED → candidates[0]

elif len(candidates) > 1:
    # Multiple new paths share this hash (rare: identical content in multiple places)
    same_name = [c for c in candidates if basename(c) == basename(old_path)]
    if len(same_name) == 1:
        mark old_path as MOVED → same_name[0]
        the rest become NEW (Step 4 will dedupe by hash)
    else:
        # Truly ambiguous — log and treat as deleted; new copies all become NEW
        log_ambiguous(old_path, candidates)
        mark old_path as DELETED

else:  # len(candidates) == 0
    mark old_path as DELETED
```

**Key property**: filename is *not required* to match. A renamed-and-moved file (`mv old/notes.md new/guide.md`) is still detected as a move because its hash is unchanged. Filename only matters as a tiebreaker when hash-based candidates are ambiguous.

**Why this works**: MD5 collisions in real text content are vanishingly rare. If two files share a hash, they share content. Treating shared hash as "same identity" is principled — and on the rare ambiguous case, the algorithm degrades to "log and let dedup sort it out", not silent corruption.

### Logging

Print a one-line summary per category and, if any ambiguous moves were logged, print the full ambiguous list so the user can investigate.

Only `new`, `updated`, and `moved-with-edit` files need content reading in Step 6. Pure moves (hash unchanged) carry their cached title/takeaway forward.

## Step 4: Deduplicate

Group files by their MD5 hash. For any hash appearing more than once:

1. Pick a **canonical** path: prefer files in a project that has a CLAUDE.md (active project), then prefer shorter paths, then prefer `docs/` subdirectories.
2. Other paths with the same hash become **aliases**.

Do NOT hardcode known duplicate sets — detect all duplicates automatically from hashes.

For files with the same filename but different content (e.g., multiple `CLAUDE.md`), these are NOT duplicates. Just note them in the report for user awareness.

## Step 5: Categorize

For each unique (non-alias) file, assign a category. Run **primary filename/path rules** first (first match wins), then if and only if the result is `Other`, run **secondary content-based fallback** using the takeaway text.

### Primary rules (filename + path)

| Category | Match Rules |
|---|---|
| Project Profiles | Filename is `CLAUDE.md` or `README.md` AND file is at project root (depth ≤ 2 from scan root) |
| Personal Knowledge | Path contains `claude-knowledge/guides/`, `notes/`, or `methodology/`; or filename matches `*hygiene*`, `*playbook*`, `*conventions*`, `*-style*`, `*manifesto*`, `*-principles*` |
| Engineering Lessons | Path contains `experience/`, `lessons/`, or `postmortem/`; or filename matches `*lessons*`, `*pitfalls*`, `*bugs*`, `*postmortem*`, `*gotchas*` |
| Architecture | Path contains `/architecture/` or `/design/` (must be a path component, not just a filename suffix); or filename matches `ARCHITECTURE*`, `*-architecture*` |
| Product Specs | Path contains `specs/` or `requirements/`; or filename matches `*PRD*`, `*-spec*`, `*usecase*`, `*journey*` |
| Security | Filename matches `*security-*`, `*-security*`, `*audit*` (excludes the GitHub-standard `SECURITY.md`, already filtered in Step 1) |
| Dev Guides | Filename matches `DEV_GUIDE*`, `INITIALIZE*`, `development.md`, `SERVER.md`, `SETUP*`; or path contains `docs/requirements/` |
| Planning | Path contains `planning/`; or filename matches `*ROADMAP*`, `*PROGRESS*`, `*-plan.md`, `TODO*` |
| Other | Everything else (will fall through to secondary rules) |

### Secondary rules (content fallback for `Other`)

Only invoked if primary rules returned `Other`. Read the takeaway already produced in Step 6 (or if Step 6 hasn't run for this file yet because it's unchanged, use the cached takeaway from `hashes.json`). Lowercase-match the takeaway against keyword sets:

| Promoted to | Trigger keywords (any match) |
|---|---|
| Engineering Lessons | `lesson`, `pitfall`, `bug`, `踩坑`, `postmortem`, `regression`, `gotcha`, `mistake` |
| Personal Knowledge | `playbook`, `manifesto`, `methodology`, `convention`, `规范`, `心得`, `philosophy` |
| Architecture | `architecture`, `system design`, `data flow`, `component diagram`, `service boundary` |
| Product Specs | `user journey`, `acceptance criteria`, `feature spec`, `PRD`, `product requirement` |
| Planning | `roadmap`, `milestone`, `Q1 plan`, `quarter plan`, `progress report`, `状态更新` |
| Other (kept) | None of the above |

The secondary pass exists because filenames lie. A file called `notes.md` could be lessons learned, a playbook, or a stub — only content tells. Keep this pass cheap: substring match on the cached takeaway, no extra LLM call.

### Kind classification (for lesson-like categories)

After category is fixed, for files in `Engineering Lessons` or `Personal Knowledge`, also assign a `kind`:

| Kind | Heuristic (any match in takeaway) |
|---|---|
| `evergreen` | Names a framework / language / API behavior / mechanism-level pitfall (e.g. "DispatchQueue.main.asyncAfter", "VNClassifyImageRequest", "useEffect cleanup", "FastAPI dependency injection", "SwiftUI", "race condition", "memory leak") |
| `project-specific` | Names project-internal choices: "we use", "我们用", "我们决定", project-name + "Pow"/"Lottie"/"Sentry"/specific library, schema names, internal endpoint names, internal table/model names |
| `unknown` | None of above clear |

Why this matters: `evergreen` lessons travel between projects (mechanism-level pitfalls recur). `project-specific` lessons pollute cross-project view — they only mean something in the original project. The kind drives Step 6 inclusion: `project-specific` entries stay in `registry.md` (still indexed) but are **excluded from `context.md`** to keep cross-project view clean.

Rules-only — no extra LLM call. Misclassifications are cheap (file still indexed, just lives in a different bucket).

## Step 6: Build context.md

For each **new, updated, or moved** file, read the first 2000 characters using `head -c 2000` in batches (more efficient than individual Read calls for many files). Extract:
- The title (first `# heading` line)
- A 2-5 line takeaway: the core lessons, decisions, or facts — more than a sentence, less than a paragraph

For **unchanged** files, reuse their entry from `hashes.json` (the `title` and `takeaway` fields saved in Step 8).

### Staleness markers

Compare each file's `mtime` (from Step 2) and `last_verified_at` (from prior `hashes.json`, may be null) against now:

- If `mtime` > 180 days ago AND (`last_verified_at` is null OR > 180 days ago) → prefix the rendered takeaway in `context.md` with `[unverified — last seen YYYY-MM-DD]` where the date is the more recent of mtime / last_verified_at.
- This is a **rendering-time** decoration; the underlying takeaway stays clean. It's the visual cue that drives the "verify before applying" protocol in CLAUDE-TEMPLATE's KB usage section.

### Project-specific exclusion

Files with `kind: project-specific` (from Step 5) are **excluded from context.md** Engineering Lessons / Personal Knowledge sections. They remain in `registry.md` and `hashes.json` (still indexed by name + project for searchability via grep), they just don't pollute the cross-project narrative view.

Write `/Users/cm/Downloads/claude-knowledge/context.md` with this structure. **Section order matters** — Project Profiles and Personal Knowledge come first because they're the most-referenced overview material:

```markdown
# Cross-Project Knowledge Base
> Auto-generated by /sync-docs on YYYY-MM-DD HH:MM
> Scan roots: <ROOT_1>, <ROOT_2>, ...
> Files indexed: N unique (M total, D duplicates)

---

## Project Profiles

### CLAUDE.md — project-name
> Source: relative/path/to/CLAUDE.md

Takeaway should prioritize the project's `## Architecture` section (stack, ports, entry points) and `## Hard Rules` highlights. Keep it 3-5 lines so each project's profile is scannable.

---

### README.md — project-name
> Source: relative/path/to/README.md

(when CLAUDE.md is absent, README.md serves as profile)

---

## Personal Knowledge

### [Title]
> Source: relative/path/to/file.md

User-curated methodology / playbook / convention docs. No project name suffix — these are cross-project.

---

## Engineering Lessons

### [Title] — project-name
> Source: relative/path/to/file.md

2-5 line takeaway.

---

## Architecture
## Product Specs
## Security
## Dev Guides
## Planning
## Other

(continue same format for each category in this exact order)
```

**Section ordering rationale**: Project Profiles answers "what is this project?", Personal Knowledge answers "what's the house style?". These two together prime any new session. The remaining categories are reference material to dip into as needed. `Other` always comes last and is the first to be dropped under size pressure.

**Size constraint**: context.md must stay under 2000 lines. If it exceeds:
1. Drop "Other" category entries first
2. Then truncate `Engineering Lessons` and `Architecture` entries from longest takeaway downward (keep title + first 2 lines)
3. Never truncate Project Profiles or Personal Knowledge — they're the most-referenced

## Step 7: Build registry.md

Write `/Users/cm/Downloads/claude-knowledge/registry.md`:

```markdown
# Documentation Registry
> Last synced: YYYY-MM-DD HH:MM
> Scan roots: <ROOT_1>, <ROOT_2>, ...

## Summary
- Total files scanned: N
- Unique files: N
- Duplicate sets: N
- Moved files (since last sync): N
- Ambiguous moves needing review: N

## Index

(Categories appear in this order: Project Profiles → Personal Knowledge → Engineering Lessons → Architecture → Product Specs → Security → Dev Guides → Planning → Other)

### Project Profiles

| File | Project | Description | Hash |
|------|---------|-------------|------|
| CLAUDE.md | project-name | one-line desc | abc123 |

(repeat per category)

## By Task Trigger

(Indexed by takeaway-text keyword match. Files can appear in multiple buckets. This is the section users copy-paste into project CLAUDE.md "scenario trigger" blocks.)

### UI / 设计系统
| File | Project | Hash |
|------|---------|------|
| ... (files matching `ui-rules` / `design system` / `design tokens` / `颜色` / `排版` in takeaway) | ... | ... |

### API contract / 前后端对接
(files matching `api contract` / `endpoint` / `breaking change` / `request schema` / `契约`)

### Bug 排查 / 经验教训
(files matching `lesson` / `pitfall` / `bug` / `regression` / `postmortem` / `gotcha` / `踩坑`)

### 数据迁移 / DB
(files matching `migration` / `schema` / `database` / `sqlite` / `postgres` / `swiftdata`)

### 启动 / 部署 / CI
(files matching `deploy` / `dockerfile` / `start.sh` / `systemctl` / `github actions` / `ci`)

### 平台特定
(grouped by detected stack from path/takeaway: iOS / SwiftUI / React / FastAPI / Next.js / Bun / etc.)

## Duplicate Sets

| Canonical | Aliases | Hash |
|-----------|---------|------|
| /path/canonical.md | /path/alias1.md, ... | abc123 |

## Same-Name Files (different content)

| Filename | Paths |
|----------|-------|
| CLAUDE.md | clawapp, amigo_app, ... |
```

The By Task Trigger pass uses cheap substring matching against the cached takeaway — no extra LLM call. A file qualifies for a bucket if its takeaway lowercased contains any of the bucket's trigger keywords. Skip empty buckets (don't render).

## Step 8: Write hashes.json

Write `/Users/cm/Downloads/claude-knowledge/hashes.json`:

```json
{
  "scan_roots": ["<ROOT_1>", "<ROOT_2>"],
  "scanned_at": "<ISO timestamp>",
  "files": {
    "/absolute/path/file.md": {
      "hash": "<md5>",
      "category": "<category>",
      "kind": "evergreen | project-specific | unknown",
      "last_verified_at": null,
      "canonical": true,
      "alias_of": null,
      "title": "The Document Title",
      "takeaway": "2-5 line summary for reuse on next sync",
      "previous_paths": [],
      "mtime": 1714000000.0
    }
  }
}
```

For alias files, set `"canonical": false` and `"alias_of": "/path/to/canonical.md"`. The `title` and `takeaway` fields allow unchanged files to skip re-reading on the next sync.

`"kind"` is set in Step 5 by the lesson-kind classifier. Drives `context.md` inclusion (project-specific files indexed but not rendered cross-project) and the `[unverified]` decoration logic.

`"last_verified_at"` is initialized to `null`. It is intended to be updated when a user/Claude explicitly confirms the lesson still holds against current code (mechanism out of scope for this skill — field reserved). Until then, staleness is judged on `mtime` alone.

`"mtime"` is the file's last modification time (collected in Step 2). Used for staleness checks in Step 6 and Step 9.

`"previous_paths"` records the file's location history when a move is detected — append the old path each time the entry's path changes. This makes move chains (`~/Downloads/foo` → `~/dev/active/foo` → `~/dev/archive/foo`) traceable. Cap at the last 5 entries to keep size bounded.

**Backwards compatibility**: if Step 0 reads a `hashes.json` with the old `scan_root` (singular) field, treat it as `scan_roots: [scan_root]`. Files lacking `previous_paths` are treated as having `[]`. Files lacking `kind` / `last_verified_at` / `mtime` get re-classified on next read (kind from takeaway, last_verified_at = null, mtime from filesystem). No migration script needed — the next write produces the new schema.

## Step 9: Staleness Check

Using the mtime collected in Step 2, identify files that have not been modified in over 90 days. Exclude aliases. Build a staleness list grouped by project.

## Step 10: Report to User

Print a summary:

```
## /sync-docs Complete

Scan roots: ~/Downloads, ~/dev, ...
Files found: N | Unique: N | Duplicates: N

### Changes since last sync
- New: N files
- Updated: N files
- Moved: N files (path updated, content preserved)
- Ambiguous moves: N (listed below if any)
- Deleted: N files
- Unchanged: N files

### Ambiguous Moves
(Only shown if non-zero. Each entry: old path → multiple hash-matching candidates. User should review.)

### Duplicate Sets Found
(auto-detected from hashes, list all sets with 2+ files)

### Category Breakdown
| Category | Count |
|----------|-------|
| Project Profiles | N |
| Personal Knowledge | N |
| Engineering Lessons | N |
| Architecture | N |
| Product Specs | N |
| Security | N |
| Dev Guides | N |
| Planning | N |
| Other | N |

### Stale Files (not modified in 90+ days)
These files haven't been touched in over 3 months. Consider reviewing or removing them:

| File | Project | Last Modified | Days Stale |
|------|---------|---------------|------------|
| ... | ... | ... | ... |

### Output
- /Users/cm/Downloads/claude-knowledge/registry.md
- /Users/cm/Downloads/claude-knowledge/context.md
- /Users/cm/Downloads/claude-knowledge/hashes.json
```

If first run, skip "Changes since last sync" and say "First scan — all files are new."

## Step 11: CLAUDE.md Punch List

For every project directory that contains a top-level `CLAUDE.md`, audit how it integrates with the KB and emit suggestions. **Do NOT auto-edit project CLAUDE.md files** — only print the punch list.

For each `<project>/CLAUDE.md`:

1. Read the file (≤8000 chars is enough for any reasonable CLAUDE.md).
2. Check if it contains the substring `claude-knowledge` OR references `context.md` / `registry.md`.
3. If yes:
   - Extract every path-like token starting with `~/Downloads/claude-knowledge/` or absolute equivalent.
   - For each referenced KB path, check current `hashes.json`:
     - Path exists → ✓
     - Path is in some entry's `previous_paths` → ⚠ moved (report new canonical path)
     - Path is gone entirely → ✗ deleted
   - Check the framing: does it say "every 5 conversations" or similar boilerplate? → ⚠ recommend upgrading to scenario-trigger pattern from `CLAUDE-TEMPLATE.md`.
4. If no:
   - Look at the project's stack (best-effort heuristics: filenames in repo, `package.json` / `pyproject.toml` / `project.yml` if present).
   - Cross-reference KB By Task Trigger buckets — does the KB have entries that would be relevant?
   - Emit ✗ "no KB integration" + a minimal pointer snippet to paste in.

Output format:

```
## CLAUDE.md Suggestions (manual review)

EmailDigest/CLAUDE.md
  ✓ References KB with scenario triggers — looks current

vigil/CLAUDE.md
  ⚠ Only references context.md once with "every 5 conversations" boilerplate
  → suggested replacement: see CLAUDE-TEMPLATE.md "KB 使用协议" section

capstone/CLAUDE.md
  ✗ No KB reference at all — KB has N entries that match this project's stack
  → minimal pointer snippet:
    ## KB 使用协议
    `~/Downloads/claude-knowledge/registry.md` 索引按任务场景查 — 触发场景见 CLAUDE-TEMPLATE.md
```

This closes the "build KB → use KB" loop. User decides whether to apply each suggestion.

## Step 12: Diff Since Last Sync

Print a concise summary of what changed in the KB this run, prioritizing high-signal additions:

```
## What's new since last sync

### New evergreen lessons (cross-project value)
- <project> — <title> | takeaway snippet (≤120 chars)
- ...

### Updated entries with substantive change
(updated files where the takeaway delta is non-trivial — heuristic: takeaway length differs by >30 chars or first sentence changed)
- <project> — <title>
- ...

### Newly stale (>180 days untouched, may need review)
- <project>/<file> — last touched YYYY-MM-DD
- ...

### Just-moved files (path changed, content same)
- <old> → <new>
- ...
```

Heuristic for "substantive": takeaway-string Levenshtein-style change > 30 chars OR first sentence (up to first `.`/`。`) differs. Don't run actual Levenshtein — just `len(diff)` is fine.

This is what gives the user "what to read this week" without re-reading the whole context.md.

## Step 13: Prune Mode (only if `--prune` was passed)

After all the above, if `--prune` is in `$ARGUMENTS`, run two interactive cleanup waves. **Always confirm per file before deletion.** Never delete silently.

### Wave 1: Auto-flagged garbage

Build the candidate list from `hashes.json` (post-sync state):

| Trigger | Criterion |
|---|---|
| Stale | `mtime` > 180 days ago AND category ≠ `Project Profiles` |
| Empty | Source file size < 200 chars |
| Scratch | Filename matches `*tmp*` / `*scratch*` / `*draft*` / `*WIP*` / `*old*` / `*backup*` |
| Dead-project alias | Non-canonical alias whose canonical project has no commits in 365 days (best-effort: check git log) |

For each candidate, print:
```
[stale + scratch] /Users/cm/Downloads/foo/bar/old-notes.md
  Title: ...
  Takeaway: ...
  Last touched: 2025-08-12 (267 days ago)
  Action [d]elete / [k]eep / [s]kip-rest:
```

Read user input. `d` deletes the actual file (not just the index). `k` adds it to a "keep" list (suppresses on future prunes). `s` exits Wave 1 immediately.

### Wave 2: Other-category audit

For all `category: Other` entries (non-alias), prompt similarly:
```
[Other] /Users/cm/Downloads/foo/notes.md
  Title: ...
  Takeaway: ...
  Action [r]ecategorize / [i]ndex-only / [d]elete / [k]eep / [s]kip-rest:
```

- `r` — prompt for new category (one-letter shortcut), update `hashes.json` and re-render context.md/registry.md at end
- `i` — set a `index_only: true` flag in `hashes.json` (file stays in registry.md but skipped by context.md)
- `d` — delete the file
- `k` — leave as is

### Audit log

Write all decisions to `~/Downloads/claude-knowledge/logs/prune-decisions-YYYYMMDD.md`:

```markdown
# Prune Run YYYY-MM-DD HH:MM

## Wave 1 — auto-flagged
| File | Triggers | Action |
| ... | stale, scratch | deleted |
...

## Wave 2 — Other audit
| File | Action |
| ... | recategorized → Engineering Lessons |
...
```

### Re-render after prune

If any deletes happened, re-run Steps 6 and 7 (rebuild `context.md` + `registry.md`) and rewrite `hashes.json` so the on-disk artifacts match the post-prune state.

## Verification

After writing all files, verify:
1. `wc -l` on context.md is under 2000
2. `python3 -c "import json; json.load(open('/Users/cm/Downloads/claude-knowledge/hashes.json'))"` passes
3. File count in hashes.json matches discovered files
4. Every entry in `hashes.json` has the new fields (`kind`, `last_verified_at`, `mtime`) — older runs upgrade in place
5. `context.md` does not contain any entry whose `kind == "project-specific"` (kept in registry.md only)
6. If `--prune` ran with deletions: re-rendered `context.md` no longer references deleted paths
