#!/usr/bin/env python3
"""
prepare.py — sync-docs phases 1-4 (discover / hash / detect-changes / hash-exact dedup)

Deterministic, no LLM. Reads previous state from KB_HOME/hashes.json, scans the
configured roots, and emits:

  KB_HOME/work-order.json      — agent's to-do list (small, ~10KB)
  KB_HOME/survivors.json       — full survivor entry set (agent doesn't usually read)
  KB_HOME/duplicates-report.md — Step 4.3 artifact

The agent should read work-order.json and only act on the listed items. The 520
unchanged files never enter agent context.

Usage:
  prepare.py --kb <KB_HOME> [--scan-root <PATH> ...] [--verbose]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

DEFAULT_SCAN_ROOTS = [
    "/Users/cm/Downloads",
    "/Users/cm/Documents",
    "/Users/cm/projects",
    "/Users/cm/code",
]

# Path patterns excluded from find. Mirrors sync-docs.md Step 1 noise list.
FIND_EXCLUDE_PATHS = [
    "*/node_modules/*", "*/.venv/*", "*/venv/*", "*/.git/*",
    "*/dist/*", "*/build/*", "*/.cache/*", "*/__pycache__/*",
    "*/.expo/*", "*/.next/*", "*/.pytest_cache/*",
    "*/.specify/*", "*/.cursor/*", "*/.github/*",
    "*/claude-knowledge/_generated/*", "*/claude-knowledge/*",
    "*/test_fixture/*", "*/test_fixtures/*",
    "*/curseforge/*", "*/jre.bundle/*", "*/Jre_*/*",
    "*/legal/java.*", "*/legal/jdk.*", "*/legal/javafx.*",
    "*/java-runtime-*/*",
]
FIND_EXCLUDE_NAMES = [
    "LICENSE.md", "LICENCE.md", "CHANGELOG.md",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
]

ARCHIVE_TOKENS = (
    "archive", "_archive", "backup", "_backup",
    "_old", ".old", "draft", "worktrees/", ".claude/worktrees/",
)

# ----------------------------------------------------------------------------
# Step 1: discover files
# ----------------------------------------------------------------------------

def discover_files(scan_roots: list[str]) -> list[str]:
    found: set[str] = set()
    for root in scan_roots:
        if not os.path.isdir(root):
            continue
        cmd = ["find", root, "-name", "*.md", "-type", "f"]
        for pat in FIND_EXCLUDE_PATHS:
            cmd += ["-not", "-path", pat]
        for name in FIND_EXCLUDE_NAMES:
            cmd += ["-not", "-name", name]
        res = subprocess.run(cmd, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if line.strip():
                found.add(line.strip())
    return sorted(found)

# ----------------------------------------------------------------------------
# Step 2: hash and stat
# ----------------------------------------------------------------------------

def hash_and_stat(paths: list[str]) -> tuple[dict, list[dict]]:
    result, errors = {}, []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                data = fh.read()
            result[p] = {
                "hash": hashlib.md5(data).hexdigest(),
                "mtime": os.path.getmtime(p),
                "size": len(data),
            }
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return result, errors

# ----------------------------------------------------------------------------
# Step 3: detect changes (content-based identity, move detection)
# ----------------------------------------------------------------------------

def detect_changes(new_files: dict, previous: dict) -> dict:
    """Compare new hashes vs previous file-level entries. Returns categorisation."""
    prev_files = {
        eid: e for eid, e in previous.items()
        if e.get("entry_type") == "file"
    }
    prev_path_to_eid = {e["path"]: eid for eid, e in prev_files.items()}

    new_paths = set(new_files.keys())
    prev_paths = set(prev_path_to_eid.keys())

    unchanged, updated, new_, moves, deleted, ambiguous = {}, {}, {}, {}, [], []

    # First pass: paths present in both
    for path in new_paths & prev_paths:
        eid = prev_path_to_eid[path]
        if new_files[path]["hash"] == prev_files[eid]["hash"]:
            unchanged[eid] = path
        else:
            updated[eid] = path

    # Second pass: move detection for paths that vanished
    vanished = prev_paths - new_paths
    appeared = new_paths - prev_paths
    for old_path in vanished:
        eid = prev_path_to_eid[old_path]
        old_hash = prev_files[eid]["hash"]
        candidates = [
            np for np in appeared
            if new_files[np]["hash"] == old_hash
            and np not in prev_path_to_eid
        ]
        if len(candidates) == 1:
            moves[eid] = (old_path, candidates[0])
            appeared.discard(candidates[0])
        elif len(candidates) > 1:
            same_name = [c for c in candidates if os.path.basename(c) == os.path.basename(old_path)]
            if len(same_name) == 1:
                moves[eid] = (old_path, same_name[0])
                appeared.discard(same_name[0])
            else:
                ambiguous.append({"old_path": old_path, "candidates": candidates})
                deleted.append(eid)
        else:
            deleted.append(eid)

    # Third pass: leftover appeared = brand new
    for np in sorted(appeared):
        # Synthesize an entry_id from the hash for new files (hash-based identity)
        eid = new_files[np]["hash"][:16]
        new_[eid] = np

    return {
        "unchanged": unchanged,   # eid -> path
        "updated": updated,        # eid -> path
        "moved": moves,            # eid -> (old, new)
        "new": new_,               # eid -> path
        "deleted": deleted,        # list of eid
        "ambiguous": ambiguous,    # list of {old_path, candidates}
    }

# ----------------------------------------------------------------------------
# Step 3 (cont): build survivor set
# ----------------------------------------------------------------------------

def build_survivors(previous: dict, changes: dict, new_files: dict) -> dict:
    """Surface only entries that survive (carry-forward + new). Drop deleted, cascade-drop sections of deleted parents."""
    survivor_file_paths = set()
    survivors: dict = {}

    # Carry forward unchanged + updated + moved
    for eid, path in changes["unchanged"].items():
        e = dict(previous[eid])
        e["path"] = path
        survivors[eid] = e
        survivor_file_paths.add(path)

    for eid, path in changes["updated"].items():
        e = dict(previous[eid])
        e["path"] = path
        e["hash"] = new_files[path]["hash"]
        e["mtime"] = new_files[path]["mtime"]
        e["size"] = new_files[path]["size"]
        # mark for re-extract by clearing takeaway later — keep cached for now
        survivors[eid] = e
        survivor_file_paths.add(path)

    for eid, (old_path, new_path) in changes["moved"].items():
        e = dict(previous[eid])
        prevs = list(e.get("previous_paths", []))
        prevs.append(old_path)
        e["path"] = new_path
        e["previous_paths"] = prevs
        survivors[eid] = e
        survivor_file_paths.add(new_path)

    # Brand-new entries (file-level only; sections are derived later by agent / Step 5)
    for eid, path in changes["new"].items():
        meta = new_files[path]
        survivors[eid] = {
            "entry_type": "file",
            "path": path,
            "hash": meta["hash"],
            "mtime": meta["mtime"],
            "size": meta["size"],
            "title": None,
            "takeaway": None,
            "category": None,
            "kind": None,
        }
        survivor_file_paths.add(path)

    # Sections: carry forward iff parent_file survives, OR rebuild parent_file pointer if parent moved
    moved_old_to_new = {old: new for (old, new) in changes["moved"].values()}
    for eid, e in previous.items():
        if e.get("entry_type") != "section":
            continue
        parent = e.get("parent_file")
        if parent in moved_old_to_new:
            new_parent = moved_old_to_new[parent]
            survivors[eid] = {
                **e,
                "parent_file": new_parent,
                "path": new_parent + "#" + e["section_anchor"],
            }
        elif parent in survivor_file_paths:
            survivors[eid] = dict(e)
        # else: parent vanished → cascade-drop

    return survivors

# ----------------------------------------------------------------------------
# Step 4: hash-exact dedup
# ----------------------------------------------------------------------------

def _project_of(path: str, scan_roots: list[str]) -> str | None:
    """Return the first path segment AFTER the matching scan root."""
    for root in scan_roots:
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            continue
        if rel.startswith(".."):
            continue
        parts = rel.split(os.sep)
        if parts:
            return parts[0]
    return None

def display_path_of(absolute_path: str, scan_roots: list[str], section_anchor: str | None = None) -> str:
    """project/rel/path[#anchor] form for renderers and reports."""
    for root in scan_roots:
        try:
            rel = os.path.relpath(absolute_path, root)
        except ValueError:
            continue
        if not rel.startswith(".."):
            base = rel.replace(os.sep, "/")
            break
    else:
        base = absolute_path
    if section_anchor:
        return f"{base}#{section_anchor}"
    return base

def _score_canonicality(path: str, mtime: float) -> tuple[float, str]:
    """Higher score wins. Returns (score, human-readable trace).

    Penalties dominate bonuses by design: an archive path can NEVER win over
    a non-archive one regardless of mtime/length/docs-membership. This is the
    intended product semantic — files inside `archive/` etc. are explicitly
    deprecated by the user.
    """
    score = 0.0
    notes = []
    p_lower = path.lower()

    # Heavy penalties — these are the user's "this file is deprecated" signal
    if any(tok in p_lower for tok in ("archive", "_archive", "backup", "_backup")):
        score -= 100; notes.append("archive/backup -100")
    if "worktrees/" in p_lower or ".claude/worktrees/" in p_lower:
        score -= 100; notes.append("worktree -100")
    if any(tok in p_lower for tok in ("_old", ".old", "draft")):
        score -= 50; notes.append("old/draft -50")

    # Bonuses
    if "/docs/" in path or "/specs/" in path:
        score += 10; notes.append("docs/specs +10")

    # Recency: up to +5 for very recent files, decaying yearly
    now = time.time()
    days = max(0, (now - mtime) / 86400)
    recency = max(0.0, 5.0 - days / 365.0)
    score += recency
    if recency > 0.1:
        notes.append(f"recency +{recency:.1f}")

    # Short path: small tiebreak (longer path = slightly worse)
    score -= len(path) / 10000.0

    return score, " · ".join(notes) if notes else "neutral"

def _pick_canonical(paths: list[str], file_meta: dict) -> tuple[str, str]:
    """Score every path; highest wins (lex tiebreak for full determinism)."""
    scored = [
        (p, *_score_canonicality(p, file_meta[p]["mtime"]))
        for p in paths
    ]
    scored.sort(key=lambda x: (-x[1], x[0]))
    winner_path, winner_score, winner_trace = scored[0]
    return winner_path, f"score={winner_score:.2f} ({winner_trace})"

def dedup_canonical(survivors: dict, scan_roots: list[str]) -> tuple[list[dict], set[str]]:
    """Group by hash; pick canonical; return (dup_sets, alias_paths)."""
    file_entries = [e for e in survivors.values() if e.get("entry_type") == "file"]
    by_hash: dict[str, list[dict]] = {}
    for e in file_entries:
        by_hash.setdefault(e["hash"], []).append(e)

    file_meta = {e["path"]: {"mtime": e["mtime"]} for e in file_entries}

    dup_sets: list[dict] = []
    alias_paths: set[str] = set()
    for h, group in by_hash.items():
        if len(group) <= 1:
            continue
        paths = [e["path"] for e in group]
        canonical, why = _pick_canonical(paths, file_meta)
        aliases = [p for p in paths if p != canonical]
        dup_sets.append({
            "hash": h,
            "canonical": canonical,
            "aliases": aliases,
            "why": why,
            "canonical_mtime": file_meta[canonical]["mtime"],
            "alias_mtimes": {p: file_meta[p]["mtime"] for p in aliases},
        })
        alias_paths.update(aliases)
    return dup_sets, alias_paths

def drop_aliases(survivors: dict, alias_paths: set[str]) -> tuple[dict, list[str]]:
    """Drop file entries whose path is an alias; cascade-drop their sections."""
    dropped = []
    for eid in list(survivors.keys()):
        e = survivors[eid]
        if e.get("entry_type") == "file" and e["path"] in alias_paths:
            dropped.append(e["path"])
            del survivors[eid]
        elif e.get("entry_type") == "section" and e.get("parent_file") in alias_paths:
            dropped.append(e["path"])
            del survivors[eid]
    return survivors, dropped

# ----------------------------------------------------------------------------
# Step 4.3: write duplicates-report.md
# ----------------------------------------------------------------------------

def write_duplicates_report(kb_home: Path, dup_sets: list[dict], scan_roots: list[str], alias_count: int) -> None:
    out = kb_home / "duplicates-report.md"
    now = time.strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Hash-Exact Duplicate Report",
        f"> Generated by prepare.py on {now}",
        f"> Total duplicate sets: {len(dup_sets)} · Total aliases dropped from index: {alias_count}",
        "",
    ]
    if not dup_sets:
        lines.append("_No hash-exact duplicates detected._")
    else:
        lines += [
            "Each set below: one canonical path is indexed; the listed aliases are byte-identical",
            "copies that were NOT indexed (would duplicate search hits). Review and decide:",
            "- delete the alias on disk if it's truly redundant",
            "- or, if both should stay, change one so it diverges (then it becomes a separate entry)",
            "",
            "---",
            "",
        ]
        for i, ds in enumerate(dup_sets, 1):
            canon_disp = display_path_of(ds["canonical"], scan_roots)
            lines += [
                f"## Set {i} — hash: {ds['hash'][:8]}",
                f"**Canonical** (kept in KB):",
                f"- `{canon_disp}`  (mtime: {time.strftime('%Y-%m-%d', time.localtime(ds['canonical_mtime']))})",
                "",
                f"**Aliases** (NOT in KB):",
            ]
            for a in ds["aliases"]:
                disp = display_path_of(a, scan_roots)
                mt = time.strftime("%Y-%m-%d", time.localtime(ds["alias_mtimes"][a]))
                lines.append(f"- `{disp}`  (mtime: {mt})")
            lines += ["", f"**Why this canonical**: {ds['why']}", "", "---", ""]
    out.write_text("\n".join(lines), encoding="utf-8")

# ----------------------------------------------------------------------------
# Glue
# ----------------------------------------------------------------------------

def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True, help="KB_HOME directory")
    ap.add_argument("--scan-root", action="append", default=None,
                    help="Override scan roots. Repeatable. Defaults to standard list.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    kb = Path(args.kb)
    kb.mkdir(parents=True, exist_ok=True)
    scan_roots = args.scan_root or [r for r in DEFAULT_SCAN_ROOTS if os.path.isdir(r)]

    # --- Load previous state ---
    prev_hashes_path = kb / "hashes.json"
    if prev_hashes_path.exists():
        prev = json.loads(prev_hashes_path.read_text())
        previous = prev.get("files", {})
        prev_scan_roots = prev.get("scan_roots", scan_roots)
    else:
        previous = {}
        prev_scan_roots = scan_roots

    # --- Step 1: discover ---
    t_discover = time.time()
    paths = discover_files(scan_roots)

    # --- Step 2: hash ---
    t_hash = time.time()
    new_files, read_errors = hash_and_stat(paths)

    # --- Step 3: detect changes ---
    t_detect = time.time()
    changes = detect_changes(new_files, previous)
    survivors = build_survivors(previous, changes, new_files)

    # --- Step 4: dedup ---
    t_dedup = time.time()
    dup_sets, alias_paths = dedup_canonical(survivors, scan_roots)
    survivors, dropped_aliases = drop_aliases(survivors, alias_paths)
    write_duplicates_report(kb, dup_sets, scan_roots, len(dropped_aliases))

    # --- Build work-order for the agent ---
    # Agent only needs to LLM-process: new + updated entries (file-level).
    # Sections inside changed files will be derived by agent during Step 5.
    needs_extract = []
    for eid in list(changes["new"].keys()) + list(changes["updated"].keys()):
        if eid not in survivors:
            continue  # was an alias, dropped
        e = survivors[eid]
        needs_extract.append({
            "entry_id": eid,
            "path": e["path"],
            "display_path": display_path_of(e["path"], scan_roots),
            "hash": e["hash"],
            "mtime": e["mtime"],
            "size": e["size"],
            "change_type": "new" if eid in changes["new"] else "updated",
        })

    work_order = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kb_home": str(kb),
        "scan_roots": scan_roots,
        "summary": {
            "files_found": len(paths),
            "read_errors": len(read_errors),
            "unchanged": len(changes["unchanged"]),
            "updated": len(changes["updated"]),
            "moved": len(changes["moved"]),
            "new": len(changes["new"]),
            "deleted": len(changes["deleted"]),
            "ambiguous_moves": len(changes["ambiguous"]),
            "duplicate_sets": len(dup_sets),
            "aliases_dropped": len(dropped_aliases),
            "survivor_entries": len(survivors),
            "survivor_files": sum(1 for e in survivors.values() if e.get("entry_type") == "file"),
            "survivor_sections": sum(1 for e in survivors.values() if e.get("entry_type") == "section"),
        },
        "needs_takeaway_extraction": needs_extract,
        "ambiguous_moves": changes["ambiguous"],
        "read_errors": read_errors,
        "duplicates_report_path": str(kb / "duplicates-report.md"),
        "timings_seconds": {
            "discover": round(t_hash - t_discover, 2),
            "hash": round(t_detect - t_hash, 2),
            "detect": round(t_dedup - t_detect, 2),
            "dedup_and_report": round(time.time() - t_dedup, 2),
            "total": round(time.time() - t0, 2),
        },
    }
    _atomic_write_json(kb / "work-order.json", work_order)
    _atomic_write_json(kb / "survivors.json", survivors)

    # Stdout summary — human-readable, agent uses work-order.json
    s = work_order["summary"]
    print(f"prepare.py done in {work_order['timings_seconds']['total']}s")
    print(f"  files_found={s['files_found']} read_errors={s['read_errors']}")
    print(f"  unchanged={s['unchanged']} updated={s['updated']} moved={s['moved']} "
          f"new={s['new']} deleted={s['deleted']}")
    print(f"  duplicate_sets={s['duplicate_sets']} aliases_dropped={s['aliases_dropped']}")
    print(f"  survivors: {s['survivor_entries']} ({s['survivor_files']} files + {s['survivor_sections']} sections)")
    print(f"  needs_takeaway_extraction: {len(needs_extract)} entries")
    print(f"  work_order: {kb / 'work-order.json'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
