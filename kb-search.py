#!/usr/bin/env python3
"""
kb-search — semantic query against the cross-project knowledge base.

Loads the embeddings + index produced by /sync-docs, encodes the query with
the same model (all-MiniLM-L6-v2), and returns top-K cosine matches.

Filters out:
- aliases (hash-exact duplicates)
- non-canonical cluster members (only the canonical of a semantic cluster surfaces)
- file-level entries that are just ToCs for chunk-eligible parents

Usage:
  kb-search "race condition in dispatch queue"
  kb-search -k 5 "swiftdata migration"
  kb-search --category "Engineering Lessons" "permission script"
  kb-search --kind evergreen "memory leak"
  kb-search --json "..."         # machine-readable output

On first run, auto-bootstraps a Python venv at ~/Downloads/claude-knowledge/.venv
and installs sentence-transformers + numpy into it. Subsequent runs re-use it.
This is required because modern Homebrew/system Pythons reject global pip installs.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

KB = Path(os.environ.get("CLAUDE_KB_DIR", os.path.expanduser("~/Downloads/claude-knowledge")))
HASHES_PATH = KB / "hashes.json"
EMB_PATH = KB / "embeddings.npy"
IDX_PATH = KB / "embeddings_index.json"
QUERY_CACHE_PATH = KB / "query_cache.json"   # (query, emb_mtime) → encoded vector
VENV_DIR = KB / ".venv"
VENV_PY = VENV_DIR / "bin" / "python"

MODEL_NAME = "all-MiniLM-L6-v2"

# Tell HuggingFace to skip the network rate-limit check when the model is already cached.
# This saves ~200ms per invocation. Safe because if the model is missing locally,
# SentenceTransformer will error out and the user runs /sync-docs which auto-downloads.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def ensure_venv():
    """Create + populate the KB venv if missing, then re-exec under it."""
    # Detect "are we in the KB venv?" via sys.prefix (NOT sys.executable, which is a
    # symlink to the base interpreter — resolving it defeats the check).
    if Path(sys.prefix).resolve() == VENV_DIR.resolve():
        return
    KB.mkdir(parents=True, exist_ok=True)
    if not VENV_PY.exists():
        print(f"kb-search: bootstrapping venv at {VENV_DIR} (one-time, ~80MB model on first encode)...",
              file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "--quiet",
                               "sentence-transformers", "numpy"])
    # Re-exec self under the venv's python.
    os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve())] + sys.argv[1:])


def fail(msg, code=1):
    print(f"kb-search: {msg}", file=sys.stderr)
    sys.exit(code)


def load_artifacts():
    """Load JSON + embeddings only. Defer sentence-transformers import to actual encode time
    so cache-hit queries pay no model-load tax (~2s saved per warm call)."""
    if not HASHES_PATH.exists():
        fail(f"no hashes.json at {HASHES_PATH} — run /sync-docs first")
    if not EMB_PATH.exists() or not IDX_PATH.exists():
        fail(f"no embeddings at {EMB_PATH} — run /sync-docs to generate")
    import numpy as np
    with HASHES_PATH.open() as fh:
        hashes = json.load(fh)
    with IDX_PATH.open() as fh:
        idx = json.load(fh)
    arr = np.load(EMB_PATH)
    return hashes, idx, arr, np


def encode_query(query: str, np):
    """Import + load model only when called. Caller is expected to have already tried
    the query cache; this is the cache-miss fall-through path."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    return model.encode([query], show_progress_bar=False)[0]


def build_parent_paths(hashes: dict) -> set:
    """Set of file paths that have at least one section child."""
    out = set()
    for e in hashes["files"].values():
        if e.get("entry_type") == "section" and e.get("parent_file"):
            out.add(e["parent_file"])
    return out


def is_renderable(entry: dict, parent_paths: set) -> bool:
    """Whether this entry should appear in search results."""
    if entry.get("alias_of"):
        return False
    if entry.get("cluster_member_of"):
        return False
    if entry.get("kind") == "project-specific":
        return False
    # Skip the file-level entry of any chunk-eligible parent — its sections carry the real content.
    if entry.get("entry_type") == "file" and entry.get("path") in parent_paths:
        return False
    return True


def cosine_topk(query_vec, arr, np, k, penalties=None):
    # Both query and arr should be L2-normalized for proper cosine. sentence-transformers
    # encode() does NOT normalize by default — do it here.
    q = query_vec / (np.linalg.norm(query_vec) + 1e-12)
    norms = np.linalg.norm(arr, axis=1) + 1e-12
    a = arr / norms[:, None]
    sims = a @ q
    if penalties is not None:
        sims = sims - penalties
    top = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in top]


# Paths inside the sync-docs repo describe the indexer itself. They trigger false-positive
# matches when the query uses an example identifier the spec also mentions (e.g. "swiftdata
# migration" surfaces the cluster-spec section because that's the example used in Step 8).
# Apply a fixed score penalty unless the query mentions sync-docs / kb-search / skill terms.
SELF_REFERENTIAL_PREFIX = "/Users/cm/Downloads/sync-docs/"
SELF_REFERENTIAL_TOKENS = ("sync-docs", "kb-search", "kb_integrate", "kb-integrate",
                          "skill spec", "embedding pipeline", "cluster_id",
                          "synthesized_takeaway", "registry.md", "context.md",
                          "hashes.json", "overrides.json")


def query_is_about_skill(query_lower: str) -> bool:
    return any(tok in query_lower for tok in SELF_REFERENTIAL_TOKENS)


def project_for_path(path: str) -> str:
    """Best-effort project name extraction from path (matches measure-kb-usage convention)."""
    parts = path.split("/")
    # Look for first segment under a scan root that looks like a project dir.
    for marker in ("Downloads", "Documents", "projects", "code"):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                return parts[i + 1]
    return parts[-2] if len(parts) >= 2 else "?"


def render_text(rows, hashes, idx_to_eid):
    if not rows:
        print("(no results)")
        return
    for rank, (row, score) in enumerate(rows, 1):
        eid = idx_to_eid.get(row)
        if eid is None:
            continue
        e = hashes["files"].get(eid)
        if e is None:
            continue
        title = e.get("title") or "(no title)"
        cat = e.get("category", "?")
        kind = e.get("kind", "?")
        proj = project_for_path(e.get("path", ""))
        cluster = e.get("cluster_id")
        cluster_str = f"  cluster:{cluster[:8]}" if cluster else ""
        path = e.get("path", "")
        takeaway = e.get("synthesized_takeaway") or e.get("takeaway") or ""
        # Trim takeaway to ~600 chars for terminal readability
        if len(takeaway) > 600:
            takeaway = takeaway[:600] + "…"

        print(f"\n[{rank}] score={score:.3f}  {cat} / {kind}  ({proj}){cluster_str}")
        print(f"    {title}")
        print(f"    path: {path}")
        for line in takeaway.split("\n"):
            print(f"    │ {line}")


def render_json(rows, hashes, idx_to_eid):
    out = []
    for row, score in rows:
        eid = idx_to_eid.get(row)
        if eid is None:
            continue
        e = hashes["files"].get(eid)
        if e is None:
            continue
        out.append({
            "score": score,
            "entry_id": eid,
            "path": e.get("path"),
            "title": e.get("title"),
            "category": e.get("category"),
            "kind": e.get("kind"),
            "cluster_id": e.get("cluster_id"),
            "takeaway": e.get("synthesized_takeaway") or e.get("takeaway"),
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="Semantic search over the sync-docs KB")
    ap.add_argument("query", nargs="+", help="search query (one or more words)")
    ap.add_argument("-k", "--top-k", type=int, default=10, help="number of results (default 10)")
    ap.add_argument("--category", default=None, help="filter to a single category")
    ap.add_argument("--kind", default=None, choices=["evergreen", "project-specific", "unknown"],
                    help="filter to a single kind")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of human-readable")
    ap.add_argument("--include-aliases", action="store_true",
                    help="don't filter aliases / non-canonical cluster members")
    ap.add_argument("--include-self-referential", action="store_true",
                    help="don't penalize entries from the sync-docs skill itself")
    ap.add_argument("--stack", default=None,
                    help="bias results toward this stack (e.g. 'swift,swiftui,swiftdata' or 'fastapi,sqlalchemy'). "
                         "Adds a small positive score boost to entries whose path or takeaway contains any stack token.")
    args = ap.parse_args()

    query = " ".join(args.query).strip()
    if not query:
        fail("empty query")

    hashes, idx, arr, np = load_artifacts()
    idx_to_eid = {row: eid for eid, row in idx.items()}
    parent_paths = build_parent_paths(hashes)

    # Determine whether to hard-exclude self-referential entries.
    query_lower = query.lower()
    apply_self_filter = (not args.include_self_referential) and (not query_is_about_skill(query_lower))

    # Build mask of allowable rows.
    allowed = np.ones(arr.shape[0], dtype=bool)
    for eid, e in hashes["files"].items():
        row = idx.get(eid)
        if row is None:
            continue
        keep = True
        if not args.include_aliases and not is_renderable(e, parent_paths):
            keep = False
        if args.category and e.get("category") != args.category:
            keep = False
        if args.kind and e.get("kind") != args.kind:
            keep = False
        path = e.get("path") or ""
        if apply_self_filter and path.startswith(SELF_REFERENTIAL_PREFIX):
            keep = False
        if not keep:
            allowed[row] = False

    if not allowed.any():
        fail("no entries match the given filters", code=2)

    # Query embedding cache. Keyed by (query, embeddings.npy mtime). When the index is rebuilt,
    # cached query vectors stay valid (same model, same query → same vector) so we don't really
    # need to invalidate on emb_mtime — but invalidating on KB schema changes is cheap insurance.
    emb_mtime = int(EMB_PATH.stat().st_mtime)
    cache_key = f"{emb_mtime}::{MODEL_NAME}::{query}"
    qv = None
    if QUERY_CACHE_PATH.exists():
        try:
            cache = json.load(QUERY_CACHE_PATH.open())
            if cache_key in cache:
                qv = np.asarray(cache[cache_key], dtype=np.float32)
        except Exception:
            cache = {}
    else:
        cache = {}

    if qv is None:
        qv = encode_query(query, np)
        cache[cache_key] = qv.tolist()
        # Cap cache to last 256 entries
        if len(cache) > 256:
            cache = dict(list(cache.items())[-256:])
        # Atomic write
        tmp = str(QUERY_CACHE_PATH) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh)
        os.rename(tmp, str(QUERY_CACHE_PATH))

    allowed_idx = np.where(allowed)[0]
    sub = arr[allowed_idx]

    # Stack boost (only adjustment now — self-referential is hard-filtered above).
    stack_tokens = []
    if args.stack:
        stack_tokens = [t.strip().lower() for t in args.stack.split(",") if t.strip()]

    penalties = np.zeros(len(sub), dtype=np.float32)
    if stack_tokens:
        for sub_i, row in enumerate(allowed_idx):
            eid = idx_to_eid.get(int(row))
            if eid is None:
                continue
            e = hashes["files"].get(eid, {})
            haystack = ((e.get("path") or "") + " " + (e.get("takeaway") or "") + " " + (e.get("title") or "")).lower()
            if any(tok in haystack for tok in stack_tokens):
                penalties[sub_i] -= 0.08

    rows = cosine_topk(qv, sub, np, args.top_k, penalties=penalties)
    rows = [(int(allowed_idx[i]), s) for i, s in rows]

    if args.json:
        render_json(rows, hashes, idx_to_eid)
    else:
        render_text(rows, hashes, idx_to_eid)


if __name__ == "__main__":
    try:
        # Cheap availability check via importlib.util.find_spec — does NOT actually import
        # sentence_transformers (saving ~2.6s on every cache-hit call). The full import
        # only fires inside encode_query() when the query cache misses.
        import importlib.util
        if importlib.util.find_spec("numpy") is None or importlib.util.find_spec("sentence_transformers") is None:
            raise ImportError("missing deps")
    except ImportError:
        ensure_venv()
    main()
