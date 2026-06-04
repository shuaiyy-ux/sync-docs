# sync-docs

Source specs for three related knowledge-base skills: `/sync-docs`, `/kb-search`, and `/kb-integrate`. They install as Codex skills, keep compatibility with Claude-era project notes where useful, and remain generic enough to adapt to other local assistant skill systems.

`/sync-docs` scans your projects for Markdown documentation, deduplicates across repos (both byte-exact and **semantic**), splits long lesson-collection files into per-section entries, and builds a centralized knowledge base — with **content-based identity** so files survive moves between folders without losing their index entry, and **section-level identity** so individual lessons inside a `LessonsLearned.md` survive being cut and pasted to a new file.

## What it does

One command: `/sync-docs` — produces a structured knowledge base from scattered `.md` files.

```
claude-knowledge/
├── context.md              ← ≤500-line essentials, optionally @-imported by projects
├── registry.md             ← full index (file + section level), cluster column, ToC per parent file
├── hashes.json             ← state for incremental updates (atomic-written)
├── embeddings.npy          ← 384-dim vectors per entry (sentence-transformers)
├── embeddings_index.json   ← entry_id → row in embeddings.npy
└── overrides.json          ← user-taught categorization state
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
1. `overrides.json` (manual recategorization state, currently consumed by the pipeline but not edited by a ported prune command)
2. Primary filename/path rules (9 categories)
3. Secondary content-based fallback for anything that lands in `Other`

The skill can preserve manual category overrides in `overrides.json`; the current Python port does not yet include the old interactive prune workflow that edited this file.

**context.md as a 500-line essentials view** — Project Profiles + Personal Knowledge always rendered in full. Other categories ranked by `recency × cross-project-applicability` and truncated to top-N (5 for Lessons / Architecture / Dev Guides; 3 for Specs / Security / Planning; `Other` excluded). Dead projects (365+ days no commit) get an `[archived]` prefix and sort to the bottom of Project Profiles.

For deep queries, use `kb-search.py` (below) — `context.md` is the optional project-import baseline; `kb-search` is the on-demand index.

**kb-search.py** — Semantic CLI search:

```bash
python3 ~/Downloads/sync-docs/scripts/kb-search.py "race condition in dispatch queue"
python3 ~/Downloads/sync-docs/scripts/kb-search.py -k 5 "swiftdata migration"
python3 ~/Downloads/sync-docs/scripts/kb-search.py --category "Engineering Lessons" "permission script"
python3 ~/Downloads/sync-docs/scripts/kb-search.py --kind evergreen --json "memory leak"
```

Loads the prebuilt embeddings, encodes the query with the same model, returns top-K entries ranked by cosine — automatically filtering out aliases, non-canonical cluster members, and `project-specific` lessons. Section results show `path/file.md#anchor` so you jump straight to the relevant section, not the whole 800-line lessons file.

**Atomic writes** — Every output (`hashes.json`, `context.md`, `registry.md`, embeddings, overrides) is written to a `.tmp` file then renamed. If a sync is killed mid-write, the previous canonical files are intact.

**Error visibility** — File read failures (permission denied, broken symlinks) accumulate into `hashes.json["errors"]` and the run summary. No silent `except: pass`.

**Health metrics** — Each run reports: evergreen / project-specific ratio in lessons (target ≥60% evergreen for cross-project value), median takeaway length, `Other` category share, total semantic clusters formed.

**Forgotten gold** — Surfaces evergreen entries that have been untouched 90+ days and are not referenced from current project instruction files such as `AGENTS.md` or legacy `CLAUDE.md`. The reverse of pruning: lessons you wrote, the system never garbage-collects them, but you've stopped consulting them. Worth re-reading.

**Cross-project move detection** — Filename does *not* need to match. Hash determines identity. Filename only matters as a tiebreaker when multiple new paths share a hash.

**Two-pass categorization** — Per-entry: filename rules first (first match wins), then a content-keyword fallback for anything in `Other` (`lesson`/`gotcha` → Engineering Lessons, `playbook`/`规范` → Personal Knowledge, etc.). Sections inherit their parent's path-based context but classify on their own takeaway.

**Noise excludes** — `.specify/`, `.cursor/`, `.github/`, `LICENSE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`.

**Instruction-file punch list** — At the end of each sync, audits every project's Codex `AGENTS.md` or legacy `CLAUDE.md` for KB integration: validates referenced KB paths, flags moved/deleted references, emits suggested pointer snippets for projects with no integration, and recommends adding the current KB protocol with both `kb-search.py` semantic lookup and exact identifier grep-first lookup. Never auto-edits — produces a manual review list.

**Diff since last sync** — Highlights what's new (evergreen lessons that just landed), what was substantively updated (token-level `difflib`, not naive length diff), what new semantic clusters formed, what just turned stale, what just moved, and the forgotten-gold list.

**Prune status** — The old Claude command documented `/sync-docs --prune`, but the current Python scripts do not expose that flag. If cleanup is needed, run it as a separate manual workflow: Wave 1 flags stale (>180d) / empty (<200 chars) / scratch-named files for per-file confirmation; Wave 2 audits everything in `Other` for recategorize / index-only / delete / keep. Recategorization decisions should land in `overrides.json` so future syncs honor them.

## Measuring KB usefulness

`scripts/measure-kb-usage.py` mines existing Claude-era JSONL session logs (`~/.claude/projects/-Users-cm-Downloads-*/`) for every event where Claude touched the KB, captures trigger context + query + result + downstream actions, and runs an LLM judge to classify each event as `applied` / `consulted_no_action` / `contradicted` / `unrelated_match` / `unknown`.

Output: `~/Downloads/claude-knowledge/logs/kb-usage-report.md` with per-project engagement, application rate, top KB paths referenced, and concrete examples of "applied" vs "stale" events.

```bash
python3 ~/Downloads/sync-docs/scripts/measure-kb-usage.py                    # full run with judge (~$1-2 in tokens)
python3 ~/Downloads/sync-docs/scripts/measure-kb-usage.py --no-judge         # heuristics only, no LLM cost
python3 ~/Downloads/sync-docs/scripts/measure-kb-usage.py --since 2026-04-01
python3 ~/Downloads/sync-docs/scripts/measure-kb-usage.py --project EmailDigest
```

This is how you answer "was the KB actually used in Claude-era sessions?" with data instead of intuition. Codex session usage measurement is separate work because Codex stores conversation history differently.

## Companion skill: `/kb-integrate`

`/sync-docs` builds the KB. `/kb-integrate` makes a project consume it. Invoke `/kb-integrate` inside any project — it audits that project's Codex `AGENTS.md`, uses `~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md` only as a legacy compatibility reference when present, proposes a diff (no silent edits), and applies after approval. It wires a compact pointer to the Codex KB skills and the lookup split: exact identifiers / file names use `registry.md` grep first, descriptive lessons / patterns use `kb-search.py`. `@`-import of `context.md` is optional and should only be added when a project explicitly wants always-loaded KB essentials. Legacy `CLAUDE.md` files are read as migration context, and edited only when the user explicitly asks for Claude compatibility.

Natural-language entry points (skill auto-detects):
- "check /sync-docs and update my CLAUDE.md accordingly"
- "set up KB for this project"
- "wire this project into the KB"
- "我这个项目接一下 KB"

Workflow:

```
$ cd ~/Downloads/some-app
$ codex
> /kb-integrate
[audits some-app/AGENTS.md and optional Claude-era template context]
[shows diff: missing compact KB pointer, optional @-import if requested, X relevant KB entries for this stack]
> a   # apply all
[uses apply_patch; never overwrites the whole file]
[done — new sessions in this project now have KB access]
```

Use `/kb-integrate` whenever you start a new project, or when you've updated the KB protocol source and want existing Codex or Claude-compatible project instructions to refresh.

## Installation

Install or refresh the Codex skills from this repo:

```bash
~/Downloads/sync-docs/install.sh
```

`install.sh` copies the generic source specs (`sync-docs.md`, `kb-search.md`, `kb-integrate.md`) into `~/.codex/skills/*/SKILL.md` and substitutes `@SKILL_HOME@` / `@KB_HOME@`. Re-run it after changing these source specs.

`kb-search.py` runs out of `~/Downloads/sync-docs/scripts/` directly — Codex invokes it via Bash. First `/sync-docs` run (or first `kb-search.py` invocation) will auto-bootstrap a venv at `~/Downloads/claude-knowledge/.venv` with `sentence-transformers` + `numpy`, and download the ~80MB `all-MiniLM-L6-v2` model into `~/.cache/sentence-transformers/`. This is required because modern Homebrew/system Pythons reject global pip installs.

`scripts/measure-kb-usage.py` is a standalone Python script (stdlib only); run it directly with `python3`.

## Codex, Claude, and Generic Skill Roles

- **Codex**: primary runtime. Install source specs into `~/.codex/skills`, target project instructions at `AGENTS.md`, and use `kb-search.py` through the Codex skill workflow.
- **Claude**: compatibility layer. Existing `CLAUDE.md`, `~/.claude/projects/*` logs, and `CLAUDE-TEMPLATE.md` remain useful migration sources, but they are not the active Codex authority unless the user explicitly requests Claude compatibility.
- **Generic skill specs**: this repo's `*.md` files are the source specs. They use placeholders such as `@SKILL_HOME@` and `@KB_HOME@` so another installer or assistant runtime can adapt them without changing the workflow text by hand.

## Why

Your engineering lessons, architecture decisions, product specs, and deployment guides are scattered across dozens of repos and folders. Every new assistant session only sees the current project by default — everything else is a blind spot.

sync-docs turns all of that into a structured, searchable knowledge base that fits in the AI context window:

- **File moves preserve identity** — `mv` between roots without re-indexing
- **Section moves preserve identity** — cut-paste a lesson between files, the entry survives
- **Same lesson written 3 times in 3 projects becomes 1 evolved entry** — with a timeline of how understanding sharpened across writings, instead of 3 isolated copies
- **Semantic search across the corpus** — find related lessons even when the wording is completely different
- **No API tokens for embeddings** — fully local model

**An O(changed) incremental document indexer that treats content hash (file + section) as identity, clusters semantically, preserves knowledge evolution, and exposes both an optionally imported summary and an on-demand semantic search. Reorganize your filesystem freely; your knowledge base follows.**
