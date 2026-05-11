# sync-docs

A Claude Code skill that scans all your projects for Markdown documentation, deduplicates across repos (both byte-exact and **semantic**), splits long lesson-collection files into per-section entries, and builds a centralized knowledge base — with **content-based identity** so files survive moves between folders without losing their index entry, and **section-level identity** so individual lessons inside a `LessonsLearned.md` survive being cut and pasted to a new file.

## What it does

One command: `/sync-docs` — produces a structured knowledge base from all your scattered `.md` files.

```
claude-knowledge/
├── context.md              ← ≤500-line essentials, loaded into every session
├── registry.md             ← full index (file + section level), cluster column, ToC per parent file
├── hashes.json             ← state for incremental updates (atomic-written)
├── embeddings.npy          ← 384-dim vectors per entry (sentence-transformers)
├── embeddings_index.json   ← entry_id → row in embeddings.npy
└── overrides.json          ← user-taught categorization (populated by --prune Wave 2)
```

## How it works

**Multi-root scanning** — By default scans `~/Downloads`, `~/Documents`, `~/projects`, `~/code` (skipping any that don't exist). Pass paths as arguments to override.

**Content-based identity at two levels**:
- **File level**: a file's identity is its MD5. `mv old/notes.md new/guide.md` → detected as a move, cached title/takeaway carry forward.
- **Section level** (new): for chunk-eligible files (filenames matching `*lessons*` / `*pitfalls*` / `*gotchas*` / `*踩坑*` / `*经验*`, or any `Engineering Lessons` / `Personal Knowledge` file with ≥5 H2 sections, or any >500-line file with independent sections), each H2 section becomes a first-class entry with its own `section_hash`. Cut a section out of `proj-A/LessonsLearned.md` and paste it into `proj-B/notes.md` — the section entry survives the move; only its `parent_file` updates.

**Why section-level matters**: file-level indexing means a single `LessonsLearned.md` with 30 lessons gets one diluted takeaway. The specific "macOS arm64 chmod loss" lesson would never surface from a search. Section-level indexing makes each lesson independently findable, dedupable, and clusterable across projects.

**Semantic clustering with evolution preservation** — Within each category, entries with cosine similarity ≥ 0.85 (against their takeaway embeddings) form clusters. For each cluster, an LLM batch call:
- Verifies the cluster is mechanism-level cohesive (not just same surface topic)
- Synthesizes a single takeaway with an **evolution trail**: `2024-08 (proj-A): initial observation → 2025-03 (proj-B): root cause located → 2026-01 (proj-C): current fix`
- Picks the newest member as canonical; only canonical renders in `context.md`
- All members stay in `registry.md` linked by cluster_id

**Cluster-id stability**: cluster_id = md5(sorted member entry_ids). If membership doesn't change, the synthesized takeaway is cached — no LLM call on subsequent syncs.

**Conflict detection** — A separate LLM batch pass identifies pairs of evergreen lessons that make contradictory claims about the same mechanism (e.g., "always X" in proj-A vs "never X" in proj-B). These are surfaced as "需要裁决" in the run summary.

**Local embeddings** — Uses `sentence-transformers/all-MiniLM-L6-v2` (384-dim, CPU-friendly). Auto-installed on first run; ~80MB model cached locally; zero API tokens. Encodes only new/updated entries each sync.

**Three-tier categorization** — Resolution order:
1. `overrides.json` (your manual recategorizations, persisted from `--prune` Wave 2)
2. Primary filename/path rules (9 categories)
3. Secondary content-based fallback for anything that lands in `Other`

The skill learns your blind spots: every time you fix a misclassification via `--prune`, it remembers.

**context.md as a 500-line essentials view** — Project Profiles + Personal Knowledge always rendered in full. Other categories ranked by `recency × cross-project-applicability` and truncated to top-N (5 for Lessons / Architecture / Dev Guides; 3 for Specs / Security / Planning; `Other` excluded). Dead projects (365+ days no commit) get an `[archived]` prefix and sort to the bottom of Project Profiles.

For deep queries, use `kb-search.py` (below) — `context.md` is the always-loaded baseline; `kb-search` is the on-demand index.

**kb-search.py** — Semantic CLI search:

```bash
python3 ~/Downloads/sync-docs/kb-search.py "race condition in dispatch queue"
python3 ~/Downloads/sync-docs/kb-search.py -k 5 "swiftdata migration"
python3 ~/Downloads/sync-docs/kb-search.py --category "Engineering Lessons" "permission script"
python3 ~/Downloads/sync-docs/kb-search.py --kind evergreen --json "memory leak"
```

Loads the prebuilt embeddings, encodes the query with the same model, returns top-K entries ranked by cosine — automatically filtering out aliases, non-canonical cluster members, and `project-specific` lessons. Section results show `path/file.md#anchor` so you jump straight to the relevant section, not the whole 800-line lessons file.

**Atomic writes** — Every output (`hashes.json`, `context.md`, `registry.md`, embeddings, overrides) is written to a `.tmp` file then renamed. If a sync is killed mid-write, the previous canonical files are intact.

**Error visibility** — File read failures (permission denied, broken symlinks) accumulate into `hashes.json["errors"]` and the run summary. No silent `except: pass`.

**Health metrics** — Each run reports: evergreen / project-specific ratio in lessons (target ≥60% evergreen for cross-project value), median takeaway length, `Other` category share, total semantic clusters formed.

**Forgotten gold** — Surfaces evergreen entries that have been untouched 90+ days AND aren't referenced from any current project's CLAUDE.md. The reverse of pruning: lessons you wrote, the system never garbage-collects them, but you've stopped consulting them. Worth re-reading.

**Cross-project move detection** — Filename does *not* need to match. Hash determines identity. Filename only matters as a tiebreaker when multiple new paths share a hash.

**Two-pass categorization** — Per-entry: filename rules first (first match wins), then a content-keyword fallback for anything in `Other` (`lesson`/`gotcha` → Engineering Lessons, `playbook`/`规范` → Personal Knowledge, etc.). Sections inherit their parent's path-based context but classify on their own takeaway.

**Noise excludes** — `.specify/`, `.cursor/`, `.github/`, `LICENSE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`.

**CLAUDE.md punch list** — At the end of each sync, audits every project's `CLAUDE.md` for KB integration: validates referenced KB paths, flags moved/deleted references, emits suggested pointer snippets for projects with no integration, and recommends adding a `kb-search.py` invocation snippet to projects that only reference `context.md`. Never auto-edits — produces a manual review list.

**Diff since last sync** — Highlights what's new (evergreen lessons that just landed), what was substantively updated (token-level `difflib`, not naive length diff), what new semantic clusters formed, what just turned stale, what just moved, and the forgotten-gold list.

**Prune mode** — `/sync-docs --prune` runs interactive cleanup. Wave 1 flags stale (>180d) / empty (<200 chars) / scratch-named files for per-file confirmation. Wave 2 audits everything in `Other`: recategorize / index-only / delete / keep. Recategorize decisions land in `overrides.json` so the next sync honors them automatically. Decisions logged to `claude-knowledge/logs/prune-decisions-YYYYMMDD.md`.

## Measuring KB usefulness

`measure-kb-usage.py` mines existing JSONL session logs (`~/.claude/projects/-Users-cm-Downloads-*/`) for every event where Claude touched the KB, captures trigger context + query + result + downstream actions, and runs an LLM judge to classify each event as `applied` / `consulted_no_action` / `contradicted` / `unrelated_match` / `unknown`.

Output: `~/Downloads/claude-knowledge/logs/kb-usage-report.md` with per-project engagement, application rate, top KB paths referenced, and concrete examples of "applied" vs "stale" events.

```bash
python3 measure-kb-usage.py                    # full run with judge (~$1-2 in tokens)
python3 measure-kb-usage.py --no-judge         # heuristics only, no LLM cost
python3 measure-kb-usage.py --since 2026-04-01
python3 measure-kb-usage.py --project EmailDigest
```

This is how you answer "is the KB actually being used?" with data instead of intuition.

## Companion skill: `/kb-integrate`

`/sync-docs` builds the KB. `/kb-integrate` makes a project consume it. Invoke `/kb-integrate` inside any project — it audits that project's `CLAUDE.md` against `~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md`, proposes a diff (no silent edits), and applies after approval. Wires three things: `@`-import of `context.md`, a "KB 使用协议" section with current trigger scenarios, and a `kb-search.py` invocation snippet.

Natural-language entry points (skill auto-detects):
- "check /sync-docs and update my CLAUDE.md accordingly"
- "set up KB for this project"
- "wire this project into the KB"
- "我这个项目接一下 KB"

Workflow:

```
$ cd ~/Downloads/some-app
$ claude
> /kb-integrate
[audits some-app/CLAUDE.md against CLAUDE-TEMPLATE.md]
[shows diff: missing @-import, missing protocol, X relevant KB entries for this stack]
> a   # apply all
[uses Edit tool; never overwrites the whole file]
[done — new sessions in this project now have KB access]
```

Use `/kb-integrate` whenever you start a new project, or when you've updated `CLAUDE-TEMPLATE.md` and want existing projects to refresh.

## Installation

Copy both skill specs to your Claude Code commands directory:

```bash
cp sync-docs.md ~/.claude/commands/sync-docs.md
cp kb-integrate.md ~/.claude/commands/kb-integrate.md
```

`kb-search.py` runs out of `~/Downloads/sync-docs/` directly — Claude invokes it via Bash. First `/sync-docs` run (or first `kb-search.py` invocation) will auto-bootstrap a venv at `~/Downloads/claude-knowledge/.venv` with `sentence-transformers` + `numpy`, and download the ~80MB `all-MiniLM-L6-v2` model into `~/.cache/sentence-transformers/`. This is required because modern Homebrew/system Pythons reject global pip installs.

`measure-kb-usage.py` is a standalone Python script (stdlib only); run it directly with `python3`.

## Why

Your engineering lessons, architecture decisions, product specs, and deployment guides are scattered across dozens of repos and folders. Every new Claude Code conversation only sees the current project — everything else is a blind spot.

sync-docs turns all of that into a structured, searchable knowledge base that fits in the AI context window:

- **File moves preserve identity** — `mv` between roots without re-indexing
- **Section moves preserve identity** — cut-paste a lesson between files, the entry survives
- **Same lesson written 3 times in 3 projects becomes 1 evolved entry** — with a timeline of how understanding sharpened across writings, instead of 3 isolated copies
- **Semantic search across the corpus** — find related lessons even when the wording is completely different
- **No API tokens for embeddings** — fully local model

**An O(changed) incremental document indexer that treats content hash (file + section) as identity, clusters semantically, preserves knowledge evolution, and exposes both an always-loaded summary and an on-demand semantic search. Reorganize your filesystem freely; your knowledge base follows.**
