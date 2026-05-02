# sync-docs

A Claude Code skill that scans all your projects for Markdown documentation, deduplicates across repos, and builds a centralized knowledge base — with **content-based identity** so files survive moves between folders without losing their index entry.

## What it does

One command: `/sync-docs` — produces a structured knowledge base from all your scattered `.md` files.

```
claude-knowledge/
├── context.md    ← Structured knowledge base, organized by category
├── registry.md   ← Full index + dedup report
└── hashes.json   ← State persistence for incremental updates
```

## How it works

**Multi-root scanning** — By default scans `~/Downloads`, `~/Documents`, `~/projects`, `~/code` (skipping any that don't exist). Pass paths as arguments to override. Scanning multiple roots at once is what makes content-based move detection work — when you `mv` a project from `~/Downloads/foo` to `~/projects/foo`, the skill sees both ends and updates the path instead of treating it as delete+new.

**Content-based identity** — A file's identity is its MD5 hash, not its path. The move detection algorithm:

```
For each (old_path, old_hash) where old_path no longer exists:
  candidates = all new paths with the same hash that weren't in the previous index
  if exactly 1 candidate → move (filename can change, content is what matters)
  elif >1 candidate AND one matches the old filename → move to that one (tiebreaker)
  else → ambiguous (logged for user review) or genuinely deleted
```

Filename is *not required* to match. `mv old/notes.md new/guide.md` is detected as a move because content is unchanged. The cached title and takeaway carry forward across moves, so unchanged files cost zero LLM reads on subsequent syncs.

**Incremental hashing** — Computes MD5 for all `.md` files. Compares against previous `hashes.json` to classify files as Unchanged / Updated / Moved / New / Deleted. Only New, Updated, and edited-during-move files are read for takeaway generation — pure moves preserve their cached content.

**Cross-project dedup** — Groups files by hash. Canonical selection priority: projects with `CLAUDE.md` (active projects) > shortest path > `docs/` subdirectories. Duplicates are marked as aliases and excluded from the knowledge base.

**Same-name different-content detection** — e.g., 10 projects each have `CLAUDE.md` with different content. These are NOT deduplicated — listed separately for review.

**Two-pass categorization** — 9 categories, primary filename/path rules first, then a content-based fallback for anything that lands in `Other`:

| Category | Match Rules |
|---|---|
| Project Profiles | `CLAUDE.md` or `README.md` at project root (depth ≤ 2) |
| Personal Knowledge | `claude-knowledge/guides/`, `notes/`, `methodology/`; or filename matches `*hygiene*`, `*playbook*`, `*conventions*`, `*-style*`, `*manifesto*`, `*-principles*` |
| Engineering Lessons | `experience/`, `lessons/`, `postmortem/`; or filename matches `*lessons*`, `*pitfalls*`, `*bugs*`, `*postmortem*`, `*gotchas*` |
| Architecture | `/architecture/` or `/design/` as path component; or filename `ARCHITECTURE*`, `*-architecture*` |
| Product Specs | `specs/`, `requirements/`; or filename `*PRD*`, `*-spec*`, `*usecase*`, `*journey*` |
| Security | filename `*security-*`, `*-security*`, `*audit*` |
| Dev Guides | `DEV_GUIDE*`, `INITIALIZE*`, `development.md`, `SERVER.md`, `SETUP*` |
| Planning | `planning/`; or filename `*ROADMAP*`, `*PROGRESS*`, `*-plan.md`, `TODO*` |
| Other | Falls through to secondary content-based pass |

The secondary pass substring-matches the cached takeaway against keyword sets (`lesson`/`pitfall`/`gotcha` → Engineering Lessons, `playbook`/`manifesto`/`规范` → Personal Knowledge, etc.) — promoting filename-ambiguous docs into their real category at no extra LLM cost.

**Noise excludes** — Skips AI-tool boilerplate (`.specify/`, `.cursor/`), GitHub metadata (`.github/`), and standard repo files (`LICENSE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`) so they don't dilute the index.

**context.md design** — Section ordering puts Project Profiles and Personal Knowledge first (the most-referenced material when priming a new session), followed by reference categories. Hard limit of 2000 lines with priority-based truncation: drops Other → truncates lengthy Engineering Lessons / Architecture entries → never touches Project Profiles or Personal Knowledge.

## Installation

Copy `sync-docs.md` to your Claude Code commands directory:

```bash
cp sync-docs.md ~/.claude/commands/sync-docs.md
```

Then run `/sync-docs` in any Claude Code session.

## Why

Your engineering lessons, architecture decisions, product specs, and deployment guides are scattered across dozens of repos and folders. Every new Claude Code conversation only sees the current project — everything else is a blind spot.

sync-docs turns all of that into a structured, searchable knowledge base that fits in the AI context window. Move a project from `Downloads` to `Downloads/archive` six months later — the index updates the path automatically and your accumulated knowledge stays put. No re-indexing, no lost takeaways.

**An O(changed) incremental document indexer that treats content hash as identity, outputting directly into AI context. Reorganize your filesystem freely; your knowledge base follows.**
