# sync-docs

A Claude Code skill that scans all your projects for Markdown documentation, deduplicates across repos, and builds a centralized knowledge base.

## What it does

One command: `/sync-docs` — produces a structured knowledge base from all your scattered `.md` files.

```
claude-knowledge/
├── context.md    ← Structured knowledge base, organized by category
├── registry.md   ← Full index + dedup report
└── hashes.json   ← State persistence for incremental updates
```

## How it works

**Incremental hashing** — Computes MD5 for all `.md` files, batched in groups of 100 to avoid ARG_MAX overflow. Compares against previous `hashes.json` to classify files as New / Updated / Deleted / Unchanged. Unchanged files reuse cached entries — second run only processes the diff, not a full rebuild.

**Cross-project dedup** — Groups files by hash. Canonical selection priority: projects with `CLAUDE.md` (active projects) > shortest path > `docs/` subdirectories. Duplicates are marked as aliases and excluded from the knowledge base.

**Same-name different-content detection** — e.g., 10 projects each have `CLAUDE.md` with different content. These are NOT deduplicated — listed separately for review.

**Rule-engine categorization** — 8 categories, first-match priority:

| Category | Match Rules |
|---|---|
| Engineering Lessons | Path contains `experience/`, or filename matches `*bugs*`, `*lessons*`, `*pitfalls*` |
| Architecture | Path contains `architecture/`, or filename matches `*ARCHITECTURE*`, `*-design*` |
| Product Specs | Path contains `specs/`, or filename matches `*PRD*`, `*spec*`, `*usecase*`, `*journey*` |
| Security | Filename matches `*security*`, `*audit*` |
| Dev Guides | `CLAUDE.md`, `DEV_GUIDE*`, `INITIALIZE*`, `development.md`, `SERVER.md` |
| Planning | Path contains `planning/`, or filename matches `*ROADMAP*`, `*PROGRESS*`, `*plan*` |
| Project Config | `SKILL.md`, `COMMAND.md` |
| Other | Fallback |

**context.md design** — Reads only the first 800 characters of each file, extracts title + one-line takeaway. Hard limit of 2000 lines with priority-based truncation: drops Other → Project Config → compresses metadata. Minimal tokens, maximum decision context.

## Installation

Copy `sync-docs.md` to your Claude Code commands directory:

```bash
cp sync-docs.md ~/.claude/commands/sync-docs.md
```

Then run `/sync-docs` in any Claude Code session.

## Why

Your engineering lessons, architecture decisions, product specs, and deployment guides are scattered across dozens of repos. Every new Claude Code conversation only sees the current project — everything else is a blind spot.

sync-docs turns all of that into a structured, searchable knowledge base that fits in the AI context window. Start a new project, and Claude already knows every pitfall you've documented, every architecture decision you've validated, every approach you've rejected. No repetition needed.

**An O(changed) incremental document indexer that uses MD5 for both change detection and dedup, outputting directly into AI context. Turns your scattered engineering experience into structured memory.**
