---
name: kb-integrate
description: |
  Wire the current project's CLAUDE.md into the cross-project knowledge base.
  Reads ~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md as the source of truth
  for what good KB integration looks like, audits the current project's CLAUDE.md,
  proposes a diff, and applies it after user approval. Never edits silently.

  Invoke when: starting a new project that should have KB access; or when an
  existing project's CLAUDE.md was written before the KB existed; or when the
  user says any of: "check /sync-docs and update my CLAUDE.md accordingly" /
  "set up KB for this project" / "wire this project into the KB" / "我这个项目
  接一下 KB" / "更新一下 KB 协议".

  This is the COMPLEMENT to /sync-docs. /sync-docs (re)builds the KB itself;
  /kb-integrate makes a single project consume the KB.
---

You are integrating the cross-project knowledge base into the current project's CLAUDE.md. You are running INSIDE that project's directory; the user wants its CLAUDE.md to gain or refresh KB access.

## Step 1: Verify prerequisites

1. Check that `~/Downloads/claude-knowledge/` exists. If not, abort with:
   ```
   No KB found at ~/Downloads/claude-knowledge/.
   Run /sync-docs first to build the cross-project knowledge base, then retry.
   ```

2. Check that `~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md` exists. If not, abort with the same message.

3. Check that `/Users/cm/Downloads/sync-docs/kb-search.py` exists (it's referenced in the protocol). If missing, warn but proceed.

## Step 2: Locate target CLAUDE.md

Find a `CLAUDE.md` at the cwd or one parent up. If none exists:
- Ask the user: "no CLAUDE.md found in this project. Create a minimal one from the template?"
- If yes: scaffold from `~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md`, replacing `{项目名}` with the cwd basename. Save and continue.
- If no: abort.

Record the absolute path of this file as `<TARGET>`.

## Step 3: Read inputs

- `<TARGET>` (the project's CLAUDE.md)
- `~/Downloads/claude-knowledge/CLAUDE-TEMPLATE.md` (canonical "good integration")
- `~/Downloads/claude-knowledge/registry.md` — to compute "K KB entries match this project's stack" relevance hint (see Step 6)
- `~/Downloads/claude-knowledge/hashes.json` — to validate any KB paths the project's CLAUDE.md already references

## Step 4: Audit

Walk the project's CLAUDE.md and identify findings, in priority order:

| Severity | Finding | Detection |
|---|---|---|
| ✗ critical | No `@~/Downloads/claude-knowledge/context.md` import | grep substring |
| ✗ critical | No "## KB 使用协议" section at all | grep heading |
| ⚠ stale | Has KB protocol but doesn't mention `kb-search.py` | grep substring "kb-search" |
| ⚠ stale | Has "every 5 conversations" or similar boilerplate framing | substring match |
| ⚠ stale | References `~/Downloads/claude-knowledge/<path>` that's gone (not in current `hashes.json["files"]` paths AND not in any entry's `previous_paths`) | path validation |
| ⚠ moved | References a KB path now found in some entry's `previous_paths` | path validation |
| ℹ info | Existing protocol section text differs from current template's "KB 使用协议" section by >30% (heuristic: token count diff) | text compare |

For each finding, prepare a concrete patch:
- **No @-import**: prepend `@~/Downloads/claude-knowledge/context.md\n\n` after the front matter (or at very top if no front matter), inside a fitting place (often right after the title line).
- **No protocol section**: append the entire "## KB 使用协议（跨项目知识库）" block from the current template, verbatim.
- **Stale protocol**: replace the existing "## KB 使用协议..." section (start: heading, end: next `^## ` or EOF) with the current template's version.
- **Stale paths / moved**: in-line replace the bad path with the new canonical (from `hashes.json` `previous_paths` chain) or the closest live anchor.

## Step 5: Stack-relevance hint

Compute which KB entries are relevant to this project. Best-effort heuristics:
- Project stack signals: `package.json` keywords, `pyproject.toml` deps, Swift `Package.swift`, `requirements.txt`, top-level config files. Read up to 2-3 of these (don't go deep).
- Sample 1-2 source files for distinctive imports / framework names.
- Build a small list of stack tokens (e.g., `["swiftui", "swiftdata", "fastapi"]`).

Then run `kb-search.py` with each stack token (or a single concatenated query) and collect top-5 entries each. Dedupe. This gives you the "your project may benefit from these N KB entries" preview to show the user.

If `kb-search.py` is unavailable or python deps not installed, fall back to grep on `registry.md` for the stack tokens.

## Step 6: Propose

Print a structured summary:

```
## KB Integration Audit — <project-name>

**Target file**: <absolute path>

### Findings

[✗] No @-import of context.md — adds 500-line cross-project essentials to every session
[⚠] Has KB protocol but doesn't reference kb-search.py — semantic search not wired
[⚠] References "context.md" with "every 5 conversations" boilerplate — recommend scenario-trigger pattern
[ℹ] Stack analysis: this project looks like SwiftUI + SwiftData. KB has 12 entries that match.
    Top relevant: ...

### Proposed changes

1. Insert at line N:
   @~/Downloads/claude-knowledge/context.md

2. Replace lines A-B (current "KB 使用协议" section) with template version. Diff:
   <unified diff, condensed>

3. Append after line C (new section, current template's "## KB 使用协议（跨项目知识库）"):
   <full block>

### Apply?
- [a] apply all
- [s] select per-change (interactive)
- [n] cancel
```

If user chooses `a`: apply via `Edit` tool, one Edit per change.
If user chooses `s`: walk through changes one by one, take y/n for each.
If user chooses `n`: exit without changes.

## Step 7: Apply

Use the `Edit` tool exclusively. Never `Write` the whole file — preserve everything else (Architecture / Hard Rules / Git 规范 etc. are user-curated).

For multi-line replacements, ensure the `old_string` matches the file exactly (re-read if needed). For multiline appends, use a unique anchor (e.g., the last line of the file or a known section header).

## Step 8: Verify

After applying:
- Read `<TARGET>` again and confirm:
  - `@~/Downloads/claude-knowledge/context.md` substring present (if it was a finding)
  - "## KB 使用协议（跨项目知识库）" heading present
  - "kb-search" substring present
  - No more dead KB paths

## Step 9: Report

```
## /kb-integrate Complete

Applied N changes to <path>:
- ✓ added @-import of context.md
- ✓ added KB usage protocol section
- ✓ updated stale path X → Y (file moved)

Stack relevance: K KB entries match this project's stack. After this change,
new sessions in this project will:
1. Auto-load 500-line essentials from context.md
2. Know when to grep registry.md vs invoke kb-search.py
3. Verify before applying (KB is hypothesis, code is truth)

Try it: in a fresh session in this project, ask Claude:
  "search the KB for <some-known-topic-in-this-project's-domain>"
It should produce a kb-search.py invocation and ground the answer in the result.
```

If no changes were needed (project already integrated and current):

```
## /kb-integrate — already current

<path> already integrates with the KB and matches the current template. No changes needed.
```

## Edge cases

- **Multiple CLAUDE.md candidates** (e.g., one at cwd and one at parent): ask user which to target.
- **Project's CLAUDE.md uses a custom KB protocol the user wrote intentionally**: detect via "do not auto-update" / "custom protocol" comment markers. If present, skip protocol replacement and only fix dead paths.
- **Template itself is the target** (someone runs /kb-integrate while inside ~/Downloads/claude-knowledge/): refuse with "this IS the template; nothing to wire".
- **CLAUDE.md is in git but project has uncommitted CLAUDE.md changes**: warn user before editing — they may lose work otherwise.
