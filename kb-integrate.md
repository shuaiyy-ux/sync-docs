---
name: kb-integrate
description: |
  Wire the current project's AGENTS.md into the cross-project knowledge base.
  Reads @KB_HOME@/CLAUDE-TEMPLATE.md as the source of truth
  for what good KB integration looks like, audits the current project's Codex instructions,
  proposes a diff, and applies it after user approval. Never edits silently.

  Invoke when: starting a new project that should have KB access; or when an
  existing project's AGENTS.md or CLAUDE.md was written before the KB existed; or when the
  user says any of: "check /sync-docs and update my CLAUDE.md accordingly" /
  "set up KB for this project" / "wire this project into the KB" / "我这个项目
  接一下 KB" / "更新一下 KB 协议".

  This is the COMPLEMENT to /sync-docs. /sync-docs (re)builds the KB itself;
  /kb-integrate makes a single project consume the KB.
---

You are integrating the cross-project knowledge base into the current project's Codex instructions. You are running INSIDE that project's directory.

Prefer `AGENTS.md` as the Codex-native target. Only edit `CLAUDE.md` when the user explicitly wants Claude compatibility. If both files exist, keep `AGENTS.md` as the active Codex source and treat `CLAUDE.md` as legacy context.

## Step 1: Verify prerequisites

1. Check that `@KB_HOME@/` exists. If not, abort with:
   ```
   No KB found at @KB_HOME@/.
   Run /sync-docs first to build the cross-project knowledge base, then retry.
   ```

2. Check that `@KB_HOME@/CLAUDE-TEMPLATE.md` exists. If not, abort with the same message.

3. Check that `/Users/cm/Downloads/sync-docs/scripts/kb-search.py` exists (it's referenced in the protocol). If missing, warn but proceed.

## Step 2: Locate target instruction file

Find an `AGENTS.md` at the cwd or one parent up. If none exists, use a cwd or parent `CLAUDE.md` only when the user explicitly requested Claude compatibility.

If no target exists:
- Ask the user: "no AGENTS.md found in this project. Create a minimal one from the KB template?"
- If yes: scaffold from `@KB_HOME@/CLAUDE-TEMPLATE.md`, replacing `{项目名}` with the cwd basename and adapting wording from Claude to Codex/AGENTS.md. Save and continue.
- If no: abort.

Record the absolute path of this file as `<TARGET>`.

## Step 3: Read inputs

- `<TARGET>` (the project's `AGENTS.md`, or `CLAUDE.md` only for explicit compatibility)
- `@KB_HOME@/CLAUDE-TEMPLATE.md` (canonical "good integration")
- `@KB_HOME@/registry.md` — to compute "K KB entries match this project's stack" relevance hint (see Step 6)
- `@KB_HOME@/hashes.json` — to validate any KB paths the target already references

## Step 4: Audit

Walk the target instruction file and identify findings, in priority order:

| Severity | Finding | Detection |
|---|---|---|
| ⚠ optional | No `@@KB_HOME@/context.md` import and no compact Codex KB pointer section | grep substring |
| ✗ critical | No "## KB 使用协议" or compact "本地 kb" section at all | grep heading |
| ⚠ stale | Has KB protocol but doesn't mention `kb-search.py` | grep substring "kb-search" |
| ⚠ stale | Has KB protocol that says all "local KB" use must be semantic search and forbids grep without an exact-identifier exception | substring match: "必走" + "semantic" + "不 grep", or equivalent |
| ⚠ stale | Has "every 5 conversations" or similar boilerplate framing | substring match |
| ⚠ stale | References `@KB_HOME@/<path>` that's gone (not in current `hashes.json["files"]` paths AND not in any entry's `previous_paths`) | path validation |
| ⚠ moved | References a KB path now found in some entry's `previous_paths` | path validation |
| ℹ info | Existing protocol section text differs from current template's "KB 使用协议" section by >30% (heuristic: token count diff) | text compare |

For each finding, prepare a concrete patch:
- **No @-import**: only propose `@@KB_HOME@/context.md` when the target has no compact Codex KB pointer section yet, or when the user explicitly wants always-loaded KB context. If the target already says KB rules are maintained in `~/.codex/skills/kb-search` / `~/.codex/skills/kb-integrate` and includes the exact identifier grep-first split, do not force an @-import.
- **No protocol section**: append the compact Codex pointer block below, not the full template protocol.
- **Stale protocol**: replace the existing "## KB 使用协议..." / "## 本地 kb..." section (start: heading, end: next `^## ` or EOF) with the compact Codex pointer block below, including the exact identifier / file name grep-first exception.
- **Stale paths / moved**: in-line replace the bad path with the new canonical (from `hashes.json` `previous_paths` chain) or the closest live anchor.

Compact Codex pointer block:

```markdown
## KB 使用协议（跨项目知识库）

KB 规则不在本项目复制维护。权威来源:

- `~/.codex/skills/kb-search/SKILL.md`
- `~/.codex/skills/kb-integrate/SKILL.md`
- 源文件: `~/Downloads/sync-docs/kb-search.md` / `~/Downloads/sync-docs/kb-integrate.md`

分流原则: 描述性经验 / 模糊症状走 `kb-search.py`; 精确 identifier / 文件名 / 路径片段 / 错误原文先查 `~/Downloads/claude-knowledge/registry.md` 和 `context.md`.
```

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

[⚠] No @-import of context.md — optional if this project wants always-loaded KB essentials
[⚠] Has KB protocol but doesn't reference kb-search.py — semantic search not wired
[⚠] KB protocol forces semantic search for exact identifiers like `start.sh` — add grep-first exception
[⚠] References "context.md" with "every 5 conversations" boilerplate — recommend scenario-trigger pattern
[ℹ] Stack analysis: this project looks like SwiftUI + SwiftData. KB has 12 entries that match.
    Top relevant: ...

### Proposed changes

1. Optional insert at line N:
   @@KB_HOME@/context.md

2. Replace lines A-B (current "KB 使用协议" section) with template version. Diff:
   <unified diff, condensed>

3. Append after line C:
   <compact Codex pointer block>

### Apply?
- [a] apply all
- [s] select per-change (interactive)
- [n] cancel
```

If user chooses `a`: apply via `apply_patch`, one scoped patch per change.
If user chooses `s`: walk through changes one by one, take y/n for each.
If user chooses `n`: exit without changes.

## Step 7: Apply

Use `apply_patch` for manual edits. Never rewrite the whole file — preserve everything else (Architecture / Hard Rules / Git 规范 etc. are user-curated).

For multi-line replacements, ensure the `old_string` matches the file exactly (re-read if needed). For multiline appends, use a unique anchor (e.g., the last line of the file or a known section header).

## Step 8: Verify

After applying:
- Read `<TARGET>` again and confirm:
  - `@@KB_HOME@/context.md` substring present only if it was applied, or a compact Codex KB pointer section is present
  - "## KB 使用协议（跨项目知识库）" or compact "本地 kb" heading present
  - "kb-search" substring present
  - No more dead KB paths

## Step 9: Report

```
## /kb-integrate Complete

Applied N changes to <path>:
- ✓ added optional @-import of context.md, or kept compact Codex pointer only
- ✓ added KB usage protocol section
- ✓ updated stale path X → Y (file moved)

Stack relevance: K KB entries match this project's stack. After this change,
new sessions in this project will:
1. Point to the Codex KB skills instead of duplicating the full protocol in every project
2. Optionally auto-load essentials from context.md only when this project explicitly chooses that import
3. Know when exact identifiers / file names grep registry.md first vs when descriptive queries invoke kb-search.py
4. Verify before applying (KB is hypothesis, code is truth)

Try it: in a fresh Codex session in this project, ask:
  "search the KB for <some-known-topic-in-this-project's-domain>"
It should produce a kb-search.py invocation and ground the answer in the result.
```

If no changes were needed (project already integrated and current):

```
## /kb-integrate — already current

<path> already integrates with the KB and matches the current template. No changes needed.
```

## Edge cases

- **Multiple AGENTS.md candidates** (e.g., one at cwd and one at parent): ask user which to target.
- **Legacy CLAUDE.md only**: do not edit it unless the user explicitly requests Claude compatibility; otherwise offer to create `AGENTS.md`.
- **Project's instruction file uses a custom KB protocol the user wrote intentionally**: detect via "do not auto-update" / "custom protocol" comment markers. If present, skip protocol replacement and only fix dead paths.
- **Template itself is the target** (someone runs /kb-integrate while inside @KB_HOME@/): refuse with "this IS the template; nothing to wire".
- **Target file is in git but has uncommitted changes**: warn user before editing — they may lose work otherwise.
