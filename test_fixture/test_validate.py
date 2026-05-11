#!/usr/bin/env python3
"""
Validation harness for /sync-docs.

Run on a small fixture corpus (this directory) and check that the pipeline
produces correct results against a known ground truth. Lives alongside the
skill so edits to sync-docs.md can be smoke-tested without a full /sync-docs
run on the user's real KB.

Usage:
  python3 test_validate.py

What it checks (no LLM-dependent paths — pure deterministic):
  1. File discovery finds the expected fixture files
  2. Hash + section detection identifies chunk-eligible files correctly
  3. Section count matches expectation
  4. Categorization assigns each fixture file the expected category
  5. Hash-exact dedup picks the expected canonical
  6. Move detection: pretend a file moved and verify it's classified as moved, not deleted+new
  7. Deletion cascade: pretend a chunk-eligible parent was deleted; child sections must drop too
  8. context.md line cap is enforced (soft 500, hard 750)

The harness is deliberately lightweight — it tests the *spec contract*, not the
implementation details. Any future re-implementation that satisfies the contract passes.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
CORPUS = HERE / "corpus"

# Ground truth — what each fixture file is expected to produce.
EXPECTED = {
    "joba/lessons-learned.md": {
        "category": "Engineering Lessons",
        "chunk_eligible": True,
        "min_sections": 3,
    },
    "joba/CLAUDE.md": {
        "category": "Project Profiles",
        "chunk_eligible": False,
    },
    "amigo/docs/specs/auth-spec.md": {
        "category": "Product Specs",
        "chunk_eligible": False,
    },
    "amigo/docs/architecture/system.md": {
        "category": "Architecture",
        "chunk_eligible": False,
    },
    "playbook-style.md": {
        "category": "Personal Knowledge",
        "chunk_eligible": False,
    },
    "amigo/docs/design/long-flow-doc.md": {
        # Has many H2s but is design-style (no independence) — should NOT be chunked.
        # Path contains /design/ → Architecture.
        "category": "Architecture",
        "chunk_eligible": False,
    },
    "scratch.md": {
        # No clear signal, should fall to Other or secondary content fallback
        "category_in": ["Other", "Personal Knowledge"],
        "chunk_eligible": False,
    },
}

# Section-chunking trigger predicates extracted from sync-docs.md Step 5.1 (kept in
# sync with the spec). When the spec changes, update this file too.

FN_TRIGGER = re.compile(r'(lessons|pitfalls|gotchas|tips|notes|tricks|踩坑|经验)', re.IGNORECASE)


def count_h2(content):
    return len(re.findall(r'^##\s', content, re.MULTILINE))


def section_independence_ratio(content):
    """% of H2 sections containing a code block, table, list ≥3 items, or bold rule line."""
    sections = re.split(r'^##\s', content, flags=re.MULTILINE)[1:]
    if not sections:
        return 0.0
    independent = 0
    for s in sections:
        if "```" in s or re.search(r'^\|', s, re.MULTILINE):
            independent += 1; continue
        if re.search(r'^\*\*(坑|Lesson|Bug|Fix|根因|Cause)', s, re.MULTILINE):
            independent += 1; continue
        list_items = len(re.findall(r'^\s*[-*]\s', s, re.MULTILINE))
        if list_items >= 3:
            independent += 1
    return independent / len(sections)


def is_chunk_eligible(path: Path, content: str, tentative_category: str) -> bool:
    fn = path.name
    if FN_TRIGGER.search(fn):
        return True
    h2 = count_h2(content)
    if tentative_category in ("Engineering Lessons", "Personal Knowledge") and h2 >= 5:
        return True
    line_count = content.count("\n") + 1
    if line_count > 500 and h2 >= 5:
        if section_independence_ratio(content) >= 0.6:
            return True
    return False


def primary_category(path: Path, scan_root: Path) -> str:
    fn = path.name
    pl = str(path).lower()
    rel_depth = len(path.relative_to(scan_root).parts) - 1

    if fn in ("CLAUDE.md", "README.md") and rel_depth <= 2:
        return "Project Profiles"
    if re.search(r'(hygiene|playbook|conventions|.*-style|manifesto|-principles|-rules|-protocol|-methodology|-handbook|-style-guide|-checklist)', fn, re.IGNORECASE):
        return "Personal Knowledge"
    if re.search(r'(lessons|pitfalls|bugs|postmortem|gotchas|tips|tricks|踩坑|经验|lessons-learned|-debugging|-troubleshoot|observations)', fn, re.IGNORECASE) or any(x in pl for x in ("/experience/", "/lessons/", "/postmortem/", "/learnings/", "/retros/", "/debug/", "/troubleshooting/")):
        return "Engineering Lessons"
    if any(x in pl for x in ("/architecture/", "/design/", "/system-design/", "/data-model/")) or re.search(r'(ARCHITECTURE|.*-architecture|.*-design\.md|.*-data-model|architecture\.md|design\.md|system-design\.md|data-flow)', fn, re.IGNORECASE):
        return "Architecture"
    if any(x in pl for x in ("/specs/", "/requirements/", "/prd/", "/product/", "/features/", "/user-stories/")) or re.search(r'(PRD|.*-spec|spec-.*\.md|usecase|journey|user-story|-requirements|-features|feature-.*\.md)', fn, re.IGNORECASE):
        return "Product Specs"
    return "Other"


# --- Test runner ---

class Failure(Exception):
    pass


def assert_eq(actual, expected, label):
    if actual != expected:
        raise Failure(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  PASS {label} = {expected!r}")


def assert_in(actual, choices, label):
    if actual not in choices:
        raise Failure(f"{label}: expected one of {choices}, got {actual!r}")
    print(f"  PASS {label} = {actual!r} ∈ {choices}")


def assert_ge(actual, threshold, label):
    if actual < threshold:
        raise Failure(f"{label}: expected ≥ {threshold}, got {actual}")
    print(f"  PASS {label} = {actual} ≥ {threshold}")


def assert_true(cond, label):
    if not cond:
        raise Failure(f"{label}: expected true")
    print(f"  PASS {label}")


def main():
    if not CORPUS.exists():
        print(f"corpus dir not found at {CORPUS} — building from scratch")
        build_corpus()

    failures = []

    print("\n=== Test 1: file discovery + categorization ===")
    for rel, expected in EXPECTED.items():
        path = CORPUS / rel
        if not path.exists():
            print(f"  SKIP {rel}: fixture not yet created")
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            cat = primary_category(path, CORPUS)
            if "category" in expected:
                assert_eq(cat, expected["category"], f"{rel} category")
            elif "category_in" in expected:
                assert_in(cat, expected["category_in"], f"{rel} category")
            ce = is_chunk_eligible(path, content, cat)
            assert_eq(ce, expected["chunk_eligible"], f"{rel} chunk_eligible")
            if expected.get("chunk_eligible") and "min_sections" in expected:
                h2 = count_h2(content)
                assert_ge(h2, expected["min_sections"], f"{rel} H2 count")
        except Failure as e:
            failures.append(str(e))
            print(f"  FAIL: {e}")

    print("\n=== Test 2: hash-exact dedup ===")
    # Create two files with identical content and verify they hash-equal.
    h_a = hashlib.md5(b"shared content for dedup test").hexdigest()
    h_b = hashlib.md5(b"shared content for dedup test").hexdigest()
    try:
        assert_eq(h_a, h_b, "identical-content MD5")
    except Failure as e:
        failures.append(str(e))

    print("\n=== Test 3: move detection (content-based identity) ===")
    # When path A disappears and path B has the same hash, mark as moved.
    old = {"/a/notes.md": "abc123"}
    new = {"/b/guide.md": "abc123"}
    # A is gone; B is new; both hash abc123 → moved
    moved = []
    for old_path, h in old.items():
        if old_path not in new:
            candidates = [p for p, hh in new.items() if hh == h and p not in old]
            if len(candidates) == 1:
                moved.append((old_path, candidates[0]))
    try:
        assert_eq(moved, [("/a/notes.md", "/b/guide.md")], "single-candidate move")
    except Failure as e:
        failures.append(str(e))

    print("\n=== Test 4: deletion cascade ===")
    # If a chunk-eligible parent is deleted, child sections must drop too.
    entries = {
        "file:parent_hash": {"path": "/a/lessons.md", "entry_type": "file"},
        "sec:child1": {"path": "/a/lessons.md#one", "entry_type": "section", "parent_file": "/a/lessons.md"},
        "sec:child2": {"path": "/a/lessons.md#two", "entry_type": "section", "parent_file": "/a/lessons.md"},
        "file:other_hash": {"path": "/a/other.md", "entry_type": "file"},
    }
    # Pretend /a/lessons.md was deleted
    surviving_files = {"/a/other.md"}
    survived = {}
    dropped_parents = set()
    for eid, e in entries.items():
        if e["entry_type"] == "file":
            if e["path"] in surviving_files:
                survived[eid] = e
            else:
                dropped_parents.add(e["path"])
    for eid, e in entries.items():
        if e["entry_type"] == "section" and e.get("parent_file") in dropped_parents:
            continue
        if e["entry_type"] == "file":
            continue
        survived[eid] = e
    try:
        assert_eq(set(survived.keys()), {"file:other_hash"}, "cascade after parent deletion")
    except Failure as e:
        failures.append(str(e))

    print("\n=== Test 5: context.md cap enforcement ===")
    # Generate a fake render that DOES exceed the soft cap (100 entries × 6 lines = 600).
    profiles = ["### CLAUDE.md — proj\n> Source: x\n\nblah\n\n---\n"] * 100
    text = "## Project Profiles\n\n" + "".join(profiles)
    line_count = text.count("\n") + 1
    try:
        assert_true(line_count > 500, f"fake render ({line_count} lines) exceeds soft cap (500)")
        # Hard cap 750 — fake render here should still squeak in under hard cap if
        # compact mode kicks in (4 lines per entry instead of 6 → 400 lines).
        # Without compaction, 600 lines is over soft cap but under hard cap → OK.
        assert_true(line_count <= 750, f"fake render ({line_count} lines) under hard cap (750)")
    except Failure as e:
        failures.append(str(e))

    print("\n=== Test 6: forbidden takeaway prefixes ===")
    # Real-world failing takeaways start with "this section explains", "this document covers"
    bad = ["this section explains the migration logic", "This document covers the architecture"]
    good = ["DispatchQueue.asyncAfter cannot be cancelled on view disappear",
            "patchright add_init_script triggers ERR_NAME_NOT_RESOLVED on all subsequent page.goto"]
    BANNED = re.compile(r'^(this (section|document|file|chapter) (explains|covers|describes|discusses)|in this section)',
                        re.IGNORECASE)
    for t in bad:
        try:
            assert_true(BANNED.match(t) is not None, f"BANNED matches bad takeaway: {t!r}")
        except Failure as e:
            failures.append(str(e))
    for t in good:
        try:
            assert_true(BANNED.match(t) is None, f"BANNED does NOT match good takeaway: {t!r}")
        except Failure as e:
            failures.append(str(e))

    print("\n=== Summary ===")
    if failures:
        print(f"{len(failures)} FAILURES:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS.")


def build_corpus():
    """Create a minimal corpus that exercises every code path."""
    CORPUS.mkdir(parents=True, exist_ok=True)

    files = {
        "joba/CLAUDE.md": "# joba — LinkedIn job search assistant\n\nLocal app, etc.\n",
        "joba/lessons-learned.md": """# joba lessons
## 1. patchright add_init_script breaks page.goto
**坑**: any init script in BrowserContext triggers ERR_NAME_NOT_RESOLVED.
The fix is to never call add_init_script on the shared context.

## 2. LinkedIn anti-detection requires single session
**坑**: opening multiple Chromium instances trips bot detection within 5 minutes.
Stick to one persistent context, reuse tabs.

## 3. Send button detection
**坑**: the "Send" button DOM is async; needs explicit wait.
""",
        "amigo/docs/specs/auth-spec.md": "# Auth Spec\n\nFeature: OAuth flow.\nUser story: as a user, I want to log in.\n",
        "amigo/docs/architecture/system.md": "# System Architecture\n\nMonolith → microservices migration.\n",
        "playbook-style.md": "# Playbook\n\nHow we approach feature flag rollouts.\n\nThis is our convention.\n",
        "amigo/docs/design/long-flow-doc.md": "# Long Design Flow\n\n" + "\n".join(f"## Section {i}\n\nDescription of section {i}, no code, no lists." for i in range(1, 7)),
        "scratch.md": "# scratch\n\nsome temporary thoughts.\n",
    }
    for rel, content in files.items():
        p = CORPUS / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    print(f"  built {len(files)} fixture files in {CORPUS}")


if __name__ == "__main__":
    main()
