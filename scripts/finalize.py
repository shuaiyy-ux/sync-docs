#!/usr/bin/env python3
"""
finalize.py — sync-docs phases 6, 9-11 (categorize + render + atomic write)

Deterministic, no LLM. Reads:
  KB_HOME/survivors.json                  — entry set after dedup (from prepare.py)
  KB_HOME/agent-outputs.json              — agent-supplied LLM results
    {
      "takeaways": { "<entry_id>": {"title": "...", "takeaway": "..."}, ... },
      "sections":  { "<parent_path>": [ {section_anchor, section_hash, body_excerpt, title, takeaway}, ... ] },
      "clusters":  { "<cluster_id>": { "canonical_entry_id": "...", "members": [...], "synthesized_takeaway": "..." } },
      "conflicts": [ {"a_path": "...", "b_path": "...", "mechanism": "...", "reason": "..."} ]
    }
  KB_HOME/overrides.json                  — user category overrides (optional)

Writes (atomic):
  KB_HOME/hashes.json
  KB_HOME/context.md
  KB_HOME/registry.md

Usage:
  finalize.py --kb <KB_HOME> [--scan-root <PATH> ...]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants (mirror sync-docs.md)
# ----------------------------------------------------------------------------

CATEGORY_ORDER = [
    "Project Profiles", "Personal Knowledge", "Engineering Lessons",
    "Architecture", "Product Specs", "Security", "Dev Guides",
    "Planning", "Other",
]

RENDER_ORDER = [
    "Project Profiles", "Personal Knowledge", "Engineering Lessons",
    "Architecture", "Dev Guides", "Product Specs", "Security", "Planning",
]

CATEGORY_TOP_N = {
    "Engineering Lessons": 5,
    "Architecture": 5,
    "Dev Guides": 5,
    "Product Specs": 3,
    "Security": 3,
    "Planning": 3,
}

# No cap on context.md. 750 lines × ~50 chars ≈ 38 KB ≈ ~10k tokens, trivial in
# modern context windows. The previous compression cascade (drop categories →
# reduce top-N → compact-render) introduced 9 edge cases for a problem that no
# longer exists. Use top-N per category for signal quality only, not for cap fit.

# ----------------------------------------------------------------------------
# display_path
# ----------------------------------------------------------------------------

def display_path_of(absolute_path: str, scan_roots: list[str]) -> str:
    if "#" in absolute_path:
        base, anchor = absolute_path.rsplit("#", 1)
        return display_path_of(base, scan_roots) + "#" + anchor
    for root in scan_roots:
        try:
            rel = os.path.relpath(absolute_path, root)
        except ValueError:
            continue
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return absolute_path

def project_of(absolute_path: str, scan_roots: list[str]) -> str:
    for root in scan_roots:
        try:
            rel = os.path.relpath(absolute_path, root)
        except ValueError:
            continue
        if not rel.startswith(".."):
            parts = rel.split(os.sep)
            return parts[0] if parts else "?"
    return "?"

# ----------------------------------------------------------------------------
# Categorization (deterministic; Step 6)
# ----------------------------------------------------------------------------

PRIMARY_FILENAME_RULES = [
    # (category, path-contains list, glob-name list)
    ("Personal Knowledge",
     ["claude-knowledge/guides/", "/notes/", "/methodology/", "/playbooks/", "/guides/"],
     ["*hygiene*", "*playbook*", "*conventions*", "*-style*", "*manifesto*",
      "*-principles*", "*-rules*", "*-protocol*", "*-methodology*",
      "*-handbook*", "*-style-guide*", "*-checklist*"]),
    ("Engineering Lessons",
     ["/experience/", "/lessons/", "/postmortem/", "/learnings/", "/retros/",
      "/debug/", "/troubleshooting/"],
     ["*lessons*", "*pitfalls*", "*bugs*", "*postmortem*", "*gotchas*",
      "*tips*", "*tricks*", "*踩坑*", "*经验*", "*lessons-learned*",
      "*-debugging*", "*-troubleshoot*", "*observations*"]),
    ("Architecture",
     ["/architecture/", "/design/", "/system-design/", "/data-model/"],
     ["ARCHITECTURE*", "*-architecture*", "*-design.md", "*-data-model*",
      "architecture.md", "design.md", "system-design.md", "data-flow*"]),
    ("Product Specs",
     ["/specs/", "/requirements/", "/prd/", "/product/", "/features/", "/user-stories/"],
     ["*PRD*", "*-spec*", "spec-*.md", "*usecase*", "*journey*",
      "*user-story*", "*-requirements*", "*-features*", "feature-*.md"]),
    ("Security",
     ["/security/", "/audit/", "/threat-model/"],
     ["*security-*", "*-security*", "*audit*", "*threat-model*",
      "*permission*", "*authz*", "*authn*"]),
    ("Dev Guides",
     ["/docs/requirements/", "/docs/dev/", "/dev-guide/", "/setup/",
      "/getting-started/", "/onboarding/", "/cookbook/", "/howto/"],
     ["DEV_GUIDE*", "INITIALIZE*", "development.md", "SERVER.md", "SETUP*",
      "INSTALL*", "getting-started*", "dev-*.md", "*-cookbook*",
      "*-howto*", "*-recipe*", "*-walkthrough*"]),
    ("Planning",
     ["/planning/", "/roadmap/", "/milestones/"],
     ["*ROADMAP*", "*PROGRESS*", "*-plan.md", "plan-*.md", "*-milestone*",
      "TODO*", "*backlog*", "*next-steps*", "*phase-*.md", "*-progress*"]),
]

SECONDARY_KEYWORDS = [
    ("Engineering Lessons", [
        "lesson", "lesson learned", "pitfall", "bug", "踩坑", "教训", "坑：", "坑:",
        "postmortem", "post-mortem", "regression", "gotcha", "mistake",
        "root cause", "rca", "incident", "outage", "we got burned",
        "we found", "breaks when", "surprising", "quirk", "caveat",
        "the fix is", "the fix was",
    ]),
    ("Personal Knowledge", [
        "playbook", "manifesto", "methodology", "convention", "规范", "心得",
        "principle", "philosophy", "style guide", "checklist",
        "rules of thumb", "recipe", "pattern library", "how we",
        "our approach", "way of working",
    ]),
    ("Architecture", [
        "architecture", "system design", "data flow", "data model",
        "component diagram", "service boundary", "tech stack",
        "module structure", "sequence diagram", "layered", "microservice",
        "monolith", "event-driven", "data pipeline", "database schema",
    ]),
    ("Product Specs", [
        "user journey", "acceptance criteria", "feature spec", "prd",
        "product requirement", "user story", "as a user", "given/when/then",
        "gherkin", "success metric", "out of scope", "in scope",
        "flow:", "user flow",
    ]),
    ("Dev Guides", [
        "setup", "getting started", "how to", "step-by-step", "walkthrough",
        "tutorial", "cookbook", "recipe", "to run this", "to develop",
        "to install", "prerequisites", "environment variable",
        "npm run", "pnpm", "pip install", "yarn",
    ]),
    ("Security", [
        "cve", "vulnerability", "threat model", "attacker", "injection",
        "xss", "csrf", "auth bypass", "permission check",
        "principle of least", "secret", "credential", "oauth", "jwt", "token",
    ]),
    ("Planning", [
        "roadmap", "milestone", "q1 plan", "q2 plan", "quarter plan",
        "progress report", "状态更新", "下一步", "next phase", "phase 1",
        "phase 2", "mvp scope", "release plan", "cutover", "migration plan",
        "timeline", "eta",
    ]),
]

EVERGREEN_HINTS = [
    "dispatchqueue", "useeffect", "react", "swiftui", "fastapi", "django",
    "next.js", "expo", "swiftdata", "race condition", "memory leak",
    "async", "deadlock", "cors", "csrf", "oauth", "jwt", "websocket",
    "websockets", "graphql", "typescript", "rust", "kotlin", "swift",
    "kubernetes", "docker", "redis", "postgres", "sqlite",
    "playwright", "patchright", "puppeteer",
]
PROJECT_SPECIFIC_HINTS = [
    "we use", "我们用", "我们决定", "our backend", "our frontend",
    "our pipeline", "our service",
]

def _is_project_root_file(path: str, scan_roots: list[str]) -> bool:
    """Project Profiles only fire if the file is at project root (depth ≤ 2 from scan root)."""
    for root in scan_roots:
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            continue
        if rel.startswith(".."):
            continue
        depth = rel.count(os.sep)
        return depth <= 2  # spec: depth ≤ 2 from scan root
    return False

def categorize(entry: dict, scan_roots: list[str], overrides: dict) -> str:
    """Return category for an entry. Honors overrides first."""
    path = entry.get("path", "")
    if path in overrides:
        return overrides[path]

    parent = entry.get("parent_file", path) if entry.get("entry_type") == "section" else path
    base = os.path.basename(parent)
    base_lower = base.lower()

    # Project Profiles (file-level only, parent at root, not inside a dot-directory —
    # .flows/README.md etc. are tooling boilerplate, not a project's profile)
    if entry.get("entry_type") == "file" and base in ("CLAUDE.md", "README.md"):
        if _is_project_root_file(path, scan_roots) and "/." not in path:
            return "Project Profiles"

    # Primary path/filename rules
    for cat, path_contains, name_globs in PRIMARY_FILENAME_RULES:
        if any(tok.lower() in parent.lower() for tok in path_contains):
            return cat
        if any(fnmatch.fnmatch(base_lower, g.lower()) for g in name_globs):
            return cat

    # Secondary content fallback (title + takeaway)
    blob = ((entry.get("title") or "") + " " + (entry.get("takeaway") or "")).lower()
    if blob.strip():
        for cat, keywords in SECONDARY_KEYWORDS:
            if any(k in blob for k in keywords):
                return cat
    return "Other"

def classify_kind(takeaway: str | None) -> str:
    t = (takeaway or "").lower()
    if not t:
        return "unknown"
    if any(h in t for h in EVERGREEN_HINTS):
        return "evergreen"
    if any(h in t for h in PROJECT_SPECIFIC_HINTS):
        return "project-specific"
    return "unknown"

# ----------------------------------------------------------------------------
# context.md render (Step 9)
# ----------------------------------------------------------------------------

def _score(entry: dict) -> float:
    now = time.time()
    mtime = entry.get("mtime") or now
    days = max(0, (now - mtime) / 86400)
    recency = math.exp(-days / 365)
    cross = 1.0 if entry.get("kind") == "evergreen" else 0.3
    return recency * cross

def _git_last_commit_ts(project_root: str) -> float | None:
    try:
        import subprocess as sp
        r = sp.run(["git", "-C", project_root, "log", "-1", "--format=%ct"],
                   capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None

def _render_entry_block(entry: dict, scan_roots: list[str]) -> list[str]:
    title = entry.get("title") or os.path.basename(entry.get("path", "?"))
    proj = project_of(entry.get("parent_file") or entry["path"], scan_roots)
    disp = display_path_of(entry["path"], scan_roots)
    take = (entry.get("synthesized_takeaway") or entry.get("takeaway") or "").strip()
    lines = [f"### {title} — {proj}", f"> Source: {disp}"]
    if entry.get("cluster_id"):
        lines.append(f"> Cluster: {entry['cluster_id'][:6]}")
    lines += ["", take, "", "---", ""]
    return lines

def _build_section_blocks(entries_by_cat: dict, scan_roots: list[str],
                          top_ns: dict, render_categories: list[str]) -> list[str]:
    out: list[str] = []
    for cat in render_categories:
        items = entries_by_cat.get(cat, [])
        if not items:
            continue
        # Skip project-specific lessons; skip non-canonical cluster members; skip section parents.
        # Quality gates (context.md only — registry.md keeps everything greppable):
        #   [mechanical…] = first-paragraph fallback that never got LLM extraction;
        #   fragments = takeaways too short to stand alone or cut mid-sentence at a colon.
        filtered = []
        for e in items:
            if e.get("kind") == "project-specific":
                continue
            if e.get("cluster_member_of"):
                continue
            take = (e.get("synthesized_takeaway") or e.get("takeaway") or "").strip()
            if e.get("entry_type") == "file" and take.startswith("列表型聚合文档"):
                continue
            if take.startswith("[mechanical"):
                continue
            if len(take) < 50 or take.endswith((":", "：")):
                continue
            filtered.append(e)

        if cat == "Project Profiles":
            # Sort: live first by mtime desc, archived last
            live, dead = [], []
            for e in filtered:
                ts = _git_last_commit_ts(os.path.dirname(e["path"]))
                if ts is not None and (time.time() - ts) > 365 * 86400:
                    e = {**e, "title": "[archived] " + (e.get("title") or "")}
                    dead.append(e)
                else:
                    live.append(e)
            live.sort(key=lambda e: e.get("mtime", 0), reverse=True)
            ordered = live + dead
            # Cap profiles per project: root README/CLAUDE first (shallower path wins),
            # at most 3 entries — stops category-README farms (11 subdir READMEs) from
            # flooding the section.
            by_proj: dict[str, list] = defaultdict(list)
            for e in ordered:
                by_proj[project_of(e["path"], scan_roots)].append(e)
            keep = set()
            for proj, es in by_proj.items():
                es_sorted = sorted(es, key=lambda e: (
                    display_path_of(e["path"], scan_roots).count("/"),
                    -(e.get("mtime") or 0),
                ))
                keep.update(id(e) for e in es_sorted[:3])
            ordered = [e for e in ordered if id(e) in keep]
            out.append(f"## {cat}")
            out.append("")
            for e in ordered:
                out += _render_entry_block(e, scan_roots)
            out.append("---")
            out.append("")
        elif cat == "Personal Knowledge":
            ordered = sorted(filtered, key=_score, reverse=True)
            out.append(f"## {cat}")
            out.append("")
            for e in ordered:
                out += _render_entry_block(e, scan_roots)
            out.append("---")
            out.append("")
        else:
            n = top_ns.get(cat, CATEGORY_TOP_N.get(cat, 5))
            ordered = sorted(filtered, key=_score, reverse=True)[:n]
            if not ordered:
                continue
            out.append(f"## {cat} (top {n} by score)")
            out.append("")
            for e in ordered:
                out += _render_entry_block(e, scan_roots)
            out.append("---")
            out.append("")
    return out

def render_context_md(survivors: dict, scan_roots: list[str],
                       files_found: int) -> str:
    """Render context.md in full form, no cap, no compression."""
    kb_search_path = Path(__file__).resolve().parent / "kb-search.py"
    entries_by_cat: dict[str, list[dict]] = defaultdict(list)
    for e in survivors.values():
        cat = e.get("category", "Other")
        if cat == "Other":
            continue  # Other still excluded from context.md (signal quality, not cap)
        entries_by_cat[cat].append(e)

    file_count = sum(1 for e in survivors.values() if e.get("entry_type") == "file")
    section_count = sum(1 for e in survivors.values() if e.get("entry_type") == "section")

    header = [
        "# Cross-Project Knowledge Base",
        f"> Auto-generated by /sync-docs on {time.strftime('%Y-%m-%d %H:%M')}",
        f"> Scan roots: {', '.join(scan_roots)}",
        f"> Files indexed: {files_found} found, {len(survivors)} entries "
        f"({file_count} files + {section_count} sections)",
        f"> For deep queries: `python3 {kb_search_path} \"your query\"`",
        "",
        "---",
        "",
    ]
    footer = [
        "---",
        "> Full index: registry.md. Semantic search: kb-search.py.",
    ]
    body = header + _build_section_blocks(entries_by_cat, scan_roots,
                                          dict(CATEGORY_TOP_N), list(RENDER_ORDER)) + footer
    return "\n".join(body)

# ----------------------------------------------------------------------------
# registry.md render (Step 10)
# ----------------------------------------------------------------------------

def _table_row(*cells: str) -> str:
    return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |"

def render_registry_md(survivors: dict, scan_roots: list[str],
                        files_found: int, dup_sets: list[dict],
                        ambiguous_count: int, errors_count: int,
                        moved_count: int) -> str:
    file_count = sum(1 for e in survivors.values() if e.get("entry_type") == "file")
    section_count = sum(1 for e in survivors.values() if e.get("entry_type") == "section")
    clusters = {e["cluster_id"] for e in survivors.values()
                if e.get("cluster_id") and not e.get("cluster_member_of")}

    lines = [
        "# Documentation Registry",
        f"> Last synced: {time.strftime('%Y-%m-%d %H:%M')}",
        f"> Scan roots: {', '.join(scan_roots)}",
        "> Exact identifiers / file names / path fragments: grep this file first. Descriptive patterns: use `kb-search.py`.",
        "",
        "## Summary",
        f"- Total files scanned: {files_found}",
        f"- Unique entries: {len(survivors)} ({file_count} files + {section_count} sections)",
        f"- Hash-exact duplicate sets: {len(dup_sets)} "
        f"({sum(len(d['aliases']) for d in dup_sets)} aliases dropped — see duplicates-report.md)",
        f"- Semantic clusters: {len(clusters)}",
        f"- Moved entries (since last sync): {moved_count}",
        f"- Ambiguous moves needing review: {ambiguous_count}",
        f"- Read errors: {errors_count}",
        "",
        "## Index",
        "",
    ]

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for e in survivors.values():
        by_cat[e.get("category", "Other")].append(e)

    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"### {cat}")
        lines.append("")
        lines.append(_table_row("Entry", "Project", "Description", "Hash", "Cluster"))
        lines.append("|-------|---------|-------------|------|---------|")
        for e in sorted(items, key=lambda x: project_of(x.get("parent_file") or x["path"], scan_roots)):
            disp = display_path_of(e["path"], scan_roots)
            proj = project_of(e.get("parent_file") or e["path"], scan_roots)
            desc = (e.get("takeaway") or "")[:120]
            h = (e.get("hash") or "")[:6]
            cl = (e.get("cluster_id") or "")[:6] or "—"
            lines.append(_table_row(disp, proj, desc, h, cl))
        lines.append("")

    # Duplicate sets table
    lines += ["## Duplicate Sets (hash-exact)", "",
              "| Canonical | Aliases | Hash |", "|-----------|---------|------|"]
    for ds in dup_sets:
        canon = display_path_of(ds["canonical"], scan_roots)
        aliases = ", ".join(display_path_of(a, scan_roots) for a in ds["aliases"])
        lines.append(_table_row(canon, aliases, ds["hash"][:8]))
    lines.append("")

    return "\n".join(lines)

# ----------------------------------------------------------------------------
# Embeddings (folded in from former Phase B.4)
# ----------------------------------------------------------------------------

def update_embeddings(kb: Path, survivors: dict) -> dict:
    """Compute embeddings for any entry whose takeaway is not already vectorised.

    Self-bootstraps venv at @KB_HOME@/.venv. If sentence-transformers is missing,
    creates the venv and re-execs via the venv python so the import succeeds.
    Returns a stats dict for the final report.
    """
    targets = [(eid, e.get("takeaway") or "") for eid, e in survivors.items()
               if (e.get("takeaway") or "").strip()]
    if not targets:
        (kb / "embeddings_index.json").write_text("{}", encoding="utf-8")
        tmp_npy = kb / "embeddings.tmp.npy"
        import struct
        # Minimal .npy v1.0 file for an empty float32 matrix with the expected width.
        header = b"{'descr': '<f4', 'fortran_order': False, 'shape': (0, 384), }"
        padding = b" " * ((16 - ((10 + len(header) + 1) % 16)) % 16)
        with tmp_npy.open("wb") as fh:
            fh.write(b"\x93NUMPY\x01\x00")
            fh.write(struct.pack("<H", len(header) + len(padding) + 1))
            fh.write(header + padding + b"\n")
        os.replace(tmp_npy, kb / "embeddings.npy")
        return {
            "total_vectorised": 0,
            "newly_encoded": 0,
            "reused_from_cache": 0,
        }

    venv = kb / ".venv"
    venv_py = venv / "bin" / "python"
    if not venv_py.exists():
        kb.mkdir(parents=True, exist_ok=True)
        print(f"[finalize] bootstrapping venv at {venv} ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(venv_py), "-m", "pip", "install", "--quiet",
                        "--upgrade", "pip"], check=True)
        subprocess.run([str(venv_py), "-m", "pip", "install", "--quiet",
                        "sentence-transformers", "numpy"], check=True)

    # Inject venv site-packages so we can import without re-execing
    # (re-exec is fragile when system Python and venv Python resolve to the
    # same binary via symlink, as on Homebrew macOS).
    site_pkgs = sorted(venv.glob("lib/python*/site-packages"))
    if site_pkgs and str(site_pkgs[0]) not in sys.path:
        sys.path.insert(0, str(site_pkgs[0]))

    import numpy as np  # noqa: E402
    from sentence_transformers import SentenceTransformer  # noqa: E402

    idx_path = kb / "embeddings_index.json"
    npy_path = kb / "embeddings.npy"
    old_idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    old_arr = np.load(npy_path) if npy_path.exists() else np.zeros((0, 384), dtype=np.float32)

    to_encode = [(eid, t) for eid, t in targets if eid not in old_idx]

    if to_encode:
        model = SentenceTransformer("all-MiniLM-L6-v2")
        new_vecs = model.encode([t for _, t in to_encode], show_progress_bar=False)
    else:
        new_vecs = np.zeros((0, 384), dtype=np.float32)

    # Rebuild a contiguous array: reuse old rows where eid already has one
    encoded_lookup = {eid: i for i, (eid, _) in enumerate(to_encode)}
    new_idx, rows = {}, []
    for eid, _ in targets:
        new_idx[eid] = len(rows)
        if eid in old_idx and eid not in encoded_lookup:
            rows.append(old_arr[old_idx[eid]])
        else:
            rows.append(new_vecs[encoded_lookup[eid]])
    new_arr = np.vstack(rows).astype(np.float32) if rows else np.zeros((0, 384), dtype=np.float32)

    # Atomic writes
    tmp_npy = kb / "embeddings.tmp.npy"
    tmp_idx = kb / "embeddings_index.json.tmp"
    np.save(tmp_npy, new_arr)
    tmp_idx.write_text(json.dumps(new_idx))
    os.replace(tmp_npy, npy_path)
    os.replace(tmp_idx, idx_path)

    # Mark embedding_row on each survivor (for downstream invariants)
    for eid, row in new_idx.items():
        if eid in survivors:
            survivors[eid]["embedding_row"] = row

    return {
        "total_vectorised": len(new_idx),
        "newly_encoded": len(to_encode),
        "reused_from_cache": len(new_idx) - len(to_encode),
    }

# ----------------------------------------------------------------------------
# hashes.json atomic write (Step 11)
# ----------------------------------------------------------------------------

def write_hashes_json(kb: Path, survivors: dict, scan_roots: list[str],
                       errors: list[dict], conflicts: list[dict],
                       clusters: dict) -> None:
    obj = {
        "scan_roots": scan_roots,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "errors": errors,
        "cluster_index": clusters,
        "conflicts": conflicts,
        "files": survivors,
    }
    _atomic_write(kb / "hashes.json", json.dumps(obj, ensure_ascii=False, indent=2))

# ----------------------------------------------------------------------------
# Atomic write
# ----------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    ap.add_argument("--scan-root", action="append", default=None)
    ap.add_argument("--embed-only", action="store_true",
                    help="Update embeddings.npy + embeddings_index.json, then exit. "
                         "Used by the spec's Phase B between takeaway extraction and "
                         "clustering so the agent has fresh vectors to similarity-compare.")
    args = ap.parse_args()

    t0 = time.time()
    kb = Path(args.kb)
    scan_roots = args.scan_root or []

    survivors_path = kb / "survivors.json"
    if not survivors_path.exists():
        print(f"[finalize] missing {survivors_path} — did prepare.py run?", file=sys.stderr)
        return 2
    survivors = json.loads(survivors_path.read_text())

    # Use scan_roots from work-order if not overridden
    work_order_path = kb / "work-order.json"
    if work_order_path.exists():
        wo = json.loads(work_order_path.read_text())
        if not scan_roots:
            scan_roots = wo["scan_roots"]
        files_found = wo["summary"]["files_found"]
        dup_count = wo["summary"]["duplicate_sets"]
        alias_count = wo["summary"]["aliases_dropped"]
        ambiguous_count = wo["summary"]["ambiguous_moves"]
        errors = wo.get("read_errors", [])
        moved_count = wo["summary"]["moved"]
    else:
        files_found = 0
        dup_count = 0
        alias_count = 0
        ambiguous_count = 0
        errors = []
        moved_count = 0

    overrides_path = kb / "overrides.json"
    overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}

    agent_path = kb / "agent-outputs.json"
    agent = json.loads(agent_path.read_text()) if agent_path.exists() else {}

    # Merge agent-produced takeaways into survivors
    takeaways = agent.get("takeaways", {})
    for eid, payload in takeaways.items():
        if eid in survivors:
            if payload.get("title"):
                survivors[eid]["title"] = payload["title"]
            if payload.get("takeaway"):
                survivors[eid]["takeaway"] = payload["takeaway"]

    # Merge agent-produced sections (chunk-eligible files)
    for parent_path, sections in agent.get("sections", {}).items():
        for sec in sections:
            seid = sec.get("section_hash", sec.get("entry_id", ""))
            if not seid:
                continue
            survivors[seid] = {
                "entry_type": "section",
                "parent_file": parent_path,
                "section_anchor": sec["section_anchor"],
                "section_hash": sec.get("section_hash", seid),
                "path": parent_path + "#" + sec["section_anchor"],
                "title": sec.get("title"),
                "takeaway": sec.get("takeaway"),
                "hash": sec.get("section_hash", seid),
                "mtime": survivors.get(seid, {}).get("mtime") or time.time(),
                "size": len(sec.get("body_excerpt", "")),
            }

    # Dedup sections that differ only by anchor slug convention (e.g. one slugger
    # dropped CJK tokens: "#f-4-…-学到的-pattern" vs "#f-4-…-pattern"). Normalize to
    # ASCII alphanumerics; on collision keep the entry with the longer takeaway.
    def _norm_anchor(anchor: str | None) -> str:
        return re.sub(r"[^0-9a-z]", "", (anchor or "").lower())

    anchor_seen: dict[tuple, str] = {}
    for eid in sorted(survivors.keys()):
        e = survivors[eid]
        if e.get("entry_type") != "section":
            continue
        norm = _norm_anchor(e.get("section_anchor"))
        if len(norm) < 2:
            continue  # pure-CJK anchors normalize to nothing — don't risk false merges
        key = (e.get("parent_file"), norm)
        other = anchor_seen.get(key)
        if other is None:
            anchor_seen[key] = eid
            continue
        cur_len = len(e.get("takeaway") or "")
        oth_len = len(survivors[other].get("takeaway") or "")
        if (cur_len, eid) > (oth_len, other):
            del survivors[other]
            anchor_seen[key] = eid
        else:
            del survivors[eid]

    # Merge cluster assignments
    clusters = agent.get("clusters", {})
    for cid, cluster in clusters.items():
        canon = cluster.get("canonical_entry_id")
        members = cluster.get("members", [])
        synth = cluster.get("synthesized_takeaway")
        for mid in members:
            if mid in survivors:
                survivors[mid]["cluster_id"] = cid
                if mid != canon:
                    survivors[mid]["cluster_member_of"] = canon
        if canon and canon in survivors and synth:
            survivors[canon]["synthesized_takeaway"] = synth

    # Categorize + classify kind
    for eid, e in survivors.items():
        e["category"] = categorize(e, scan_roots, overrides)
        if e["category"] in ("Engineering Lessons", "Personal Knowledge"):
            e["kind"] = classify_kind(e.get("takeaway"))

    # Re-extract dup_sets for registry (they live in duplicates-report.md, but we need counts).
    # work-order summary already gives counts; we don't render the table from scratch here —
    # but registry wants a per-set table. For now, parse duplicates-report.md for the table.
    dup_sets = _parse_duplicates_report(kb / "duplicates-report.md", scan_roots)

    # --- Embeddings (folded in, was Phase B.4) ---
    emb_stats = update_embeddings(kb, survivors)

    if args.embed_only:
        print(f"finalize.py --embed-only done in {round(time.time() - t0, 2)}s")
        print(f"  embeddings: {emb_stats['total_vectorised']} total "
              f"({emb_stats['newly_encoded']} newly encoded, "
              f"{emb_stats['reused_from_cache']} reused from cache)")
        return 0

    # --- Render ---
    ctx_text = render_context_md(survivors, scan_roots, files_found)
    reg_text = render_registry_md(survivors, scan_roots, files_found, dup_sets,
                                   ambiguous_count, len(errors), moved_count)
    _atomic_write(kb / "context.md", ctx_text)
    _atomic_write(kb / "registry.md", reg_text)

    # Build cluster_index for hashes.json
    cluster_index = {cid: list(set(c.get("members", []))) for cid, c in clusters.items()}
    write_hashes_json(kb, survivors, scan_roots, errors,
                       agent.get("conflicts", []), cluster_index)

    ctx_lines = ctx_text.count("\n") + 1
    cat_counts = Counter(e.get("category", "Other") for e in survivors.values())

    print(f"finalize.py done in {round(time.time() - t0, 2)}s")
    print(f"  context.md: {ctx_lines} lines")
    print(f"  registry.md: {len(reg_text.splitlines())} lines")
    print(f"  hashes.json: {len(survivors)} entries")
    print(f"  embeddings: {emb_stats['total_vectorised']} total "
          f"({emb_stats['newly_encoded']} newly encoded, "
          f"{emb_stats['reused_from_cache']} reused from cache)")
    print(f"  categories: {dict(cat_counts)}")
    return 0

def _parse_duplicates_report(path: Path, scan_roots: list[str]) -> list[dict]:
    """Reverse-parse duplicates-report.md back into the dup_sets shape registry needs.

    Lightweight: only canonical/aliases/hash. mtimes not needed for registry table.
    """
    if not path.exists():
        return []
    sets = []
    current: dict | None = None
    state = None  # 'canonical' | 'aliases'
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^## Set \d+ — hash: ([0-9a-f]+)", line)
        if m:
            if current:
                sets.append(current)
            current = {"hash": m.group(1), "canonical": "", "aliases": []}
            state = None
            continue
        if not current:
            continue
        if line.startswith("**Canonical**"):
            state = "canonical"
            continue
        if line.startswith("**Aliases**"):
            state = "aliases"
            continue
        m2 = re.match(r"^- `([^`]+)`", line)
        if m2 and state == "canonical":
            current["canonical"] = m2.group(1)
        elif m2 and state == "aliases":
            current["aliases"].append(m2.group(1))
    if current:
        sets.append(current)
    return sets

if __name__ == "__main__":
    sys.exit(main())
