---
name: kb-search
description: |
  Semantic search across the cross-project knowledge base. Default action for
  descriptive lookups such as past lessons, similar bugs, cross-project patterns,
  or anything beyond an exact identifier lookup.

  Invoke this proactively (without being asked) when:
  - Writing a new feature / changing a contract / changing UI: search prior implementations.
  - Debugging an unfamiliar symptom you can only describe in words.
  - Touching an unfamiliar subsystem.
  - The user says "have we done this before?" / "之前怎么处理的" / "有没有类似的".
  - A descriptive `grep` against `@KB_HOME@/registry.md` returns 0 results — escalate
    here, do NOT report "not found".

  Do NOT use for: exact identifier / file / symbol lookups (use `grep` against
  `registry.md` instead — faster and deterministic when you know the literal string),
  trivial single-file edits, or work where the user has given an explicit path.
  Examples of exact lookups: `start.sh`, `DispatchQueue.main.asyncAfter`,
  `api_health_bad`, `src/common/runtime.py`.
---

You are running a semantic search against the cross-project knowledge base built by `/sync-docs`. The KB lives at `@KB_HOME@/`. Embeddings + index are at `@KB_HOME@/embeddings.*`, `@KB_HOME@/hashes.json`.

## Step 1: Form the query

If the user invoked `/kb-search <query>`, the query is in `$ARGUMENTS`. If `$ARGUMENTS` is empty, ask the user one short clarifying question (what they're looking for) and stop.

Good queries describe a **phenomenon or intent**, not a keyword:
- ✅ "race condition in dispatch queue causing duplicate events"
- ✅ "swiftdata schema migration breaks when adding non-optional column"
- ✅ "long-lived browser process should be owned by startup script, not spawned per task"
- ❌ "race" (too vague — use grep on registry.md if you want keyword)
- ❌ "DispatchQueue.main.async" (exact identifier — use grep on the repo, not KB)
- ❌ "start.sh" (exact file name — grep registry.md first; semantic search can run only after rewriting the intent)

## Step 2: Choose flags from context

Default invocation:

```bash
python3 @SKILL_HOME@/scripts/kb-search.py "<query>"
```

Add flags based on context:

| Context | Flag |
|---|---|
| Looking for durable / cross-project lessons only (skip project-specific notes) | `--kind evergreen` |
| Looking only for a specific category (e.g. "Engineering Lessons", "Product Specs") | `--category "<name>"` |
| Want fewer / more results than default 10 | `-k <N>` |
| Project has a known stack — bias toward matches in that stack | `--stack <token1>,<token2>` (e.g. `--stack swift,swiftdata` or `--stack fastapi,sqlalchemy`) |
| Need machine-readable for further processing | `--json` |
| Need a smoke test with no cache mutation | `--read-only` or `--no-cache` |

## Step 3: Run and interpret

Run the command. Each hit comes with:

- `score` (cosine similarity; > 0.25 = strong, 0.15–0.25 = consider, < 0.15 = noise)
- `path` (file + optional `#section` anchor — read just that section if anchored)
- snippet (first ~5 lines of the hit)
- category / kind / project / cluster_id

## Step 4: Verify before relying

KB is hypothesis; code is truth. For each hit you intend to use:

1. **Read the original** at `path` — anchored `#section` means read just that section, not the whole file.
2. **Grep current code** to confirm the lesson's pattern still exists. If the lesson references a file or symbol no longer in the codebase, the lesson is stale.
3. **Check metadata**: if marked `[unverified]` (> 180 days old) or `kind: project-specific` (and you're in a different project), default to NOT applying without further confirmation.
4. **Conflict resolution**: if KB and current code disagree, code wins. Note the staleness to the user; do not silently follow stale advice.

## Step 5: Report concisely

Tell the user:
- The 1–3 most relevant hits (path + 1-line takeaway each)
- Whether you verified against current code
- Any caveats (stale dates, project-specific, conflicting with current code)

Don't dump the raw search output. Synthesize.

## Edge cases

- **Exact identifier detected**: do not run semantic search first. Use `grep "<literal>" @KB_HOME@/registry.md @KB_HOME@/context.md`; if that has no hits, report no literal match and only use semantic search after rewriting the query into a phenomenon or intent.
- **No hits with `score > 0.15`**: report honestly "KB had no strong matches for `<query>`"; suggest a rephrasing if the user wants you to retry.
- **First-time run on a fresh KB**: `kb-search.py` auto-bootstraps `~/Downloads/claude-knowledge/.venv` and downloads the ~80MB MiniLM model. Mention this once if it takes > 30s.
- **`kb-search.py` errors with "no hashes.json"**: KB isn't built yet — recommend `/sync-docs` first.

## What `/kb-search` is NOT

- Not a substitute for `grep` when you have an exact identifier, file name, path fragment, or error text — `grep` is faster and deterministic for literal-string lookup.
- Not a substitute for reading the project's own `docs/` — local docs always trump cross-project KB.
- Not a substitute for asking the user when the query itself is unclear.
