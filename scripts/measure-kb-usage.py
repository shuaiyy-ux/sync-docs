#!/usr/bin/env python3
"""
measure-kb-usage.py — granular KB usage measurement from JSONL session logs.

Mines ~/.claude/projects/*/[uuid].jsonl for every event where Claude touched the
cross-project knowledge base (~/Downloads/claude-knowledge/), captures the
trigger context, the query, the result, and the downstream actions, then runs
an LLM judge to classify whether the access actually helped.

Output:
  ~/Downloads/claude-knowledge/logs/kb-events.jsonl    — raw events + judgments
  ~/Downloads/claude-knowledge/logs/kb-usage-report.md — aggregate report

Usage:
  python3 measure-kb-usage.py
  python3 measure-kb-usage.py --no-judge           # heuristic only, no LLM cost
  python3 measure-kb-usage.py --since 2026-04-01   # date filter
  python3 measure-kb-usage.py --project EmailDigest
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

KB_PATH_MARKER = "claude-knowledge"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
KB_DIR = Path.home() / "Downloads" / "claude-knowledge"
LOGS_DIR = KB_DIR / "logs"
EVENTS_OUT = LOGS_DIR / "kb-events.jsonl"
REPORT_OUT = LOGS_DIR / "kb-usage-report.md"

TRIGGER_CHARS = 1500
RESULT_CHARS = 1500
NEXT_TOOL_WINDOW = 5
NEXT_TEXT_WINDOW = 3
EDIT_DETECT_WINDOW = 10


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--no-judge", action="store_true",
                   help="skip the LLM judge pass (heuristics only)")
    p.add_argument("--since", type=str, default=None,
                   help="only include events on or after YYYY-MM-DD")
    p.add_argument("--project", type=str, default=None,
                   help="only scan a single project name (e.g. EmailDigest)")
    p.add_argument("--judge-model", type=str, default="claude-sonnet-4-6",
                   help="model passed to claude -p for judging")
    p.add_argument("--judge-workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=None,
                   help="cap total events processed (debug)")
    return p.parse_args()


def project_name_from_dir(dirname: str) -> str:
    """`-Users-cm-Downloads-EmailDigest` → `EmailDigest`."""
    if dirname.startswith("-Users-cm-Downloads-"):
        return dirname[len("-Users-cm-Downloads-"):].replace("-", "_") or "ROOT"
    return dirname.lstrip("-")


def discover_jsonls(project_filter: str | None):
    out = []
    if not PROJECTS_DIR.exists():
        return out
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        if not d.name.startswith("-Users-cm-Downloads-"):
            continue
        proj = project_name_from_dir(d.name)
        if project_filter and project_filter.lower() not in proj.lower():
            continue
        for f in sorted(d.glob("*.jsonl")):
            out.append((proj, f))
    return out


def parse_jsonl_lines(path: Path):
    """Yield (lineno, event) for each parseable line."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_text(content_block) -> str:
    """Pull text out of an assistant/user message content block (list or str)."""
    if isinstance(content_block, str):
        return content_block
    if isinstance(content_block, list):
        chunks = []
        for c in content_block:
            if isinstance(c, dict):
                if c.get("type") == "text" and "text" in c:
                    chunks.append(c["text"])
                elif c.get("type") == "tool_result":
                    inner = c.get("content")
                    if isinstance(inner, list):
                        for x in inner:
                            if isinstance(x, dict) and x.get("type") == "text":
                                chunks.append(x.get("text", ""))
                    elif isinstance(inner, str):
                        chunks.append(inner)
        return "\n".join(chunks)
    return ""


def event_role(event) -> str:
    return event.get("type") or event.get("role") or ""


def event_text(event) -> str:
    msg = event.get("message", {})
    if isinstance(msg, dict):
        return extract_text(msg.get("content", ""))
    return ""


def event_tool_uses(event):
    """Yield (tool_use_dict) for each tool_use in an assistant event."""
    msg = event.get("message", {})
    if not isinstance(msg, dict):
        return
    content = msg.get("content", [])
    if not isinstance(content, list):
        return
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_use":
            yield c


def event_tool_results(event):
    """Yield tool_result dicts (typically inside user messages)."""
    msg = event.get("message", {})
    if not isinstance(msg, dict):
        return
    content = msg.get("content", [])
    if not isinstance(content, list):
        return
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_result":
            yield c


def event_timestamp(event) -> str:
    """Best-effort timestamp."""
    for k in ("timestamp", "createdAt", "created_at"):
        if k in event:
            return str(event[k])
    msg = event.get("message", {})
    if isinstance(msg, dict):
        for k in ("timestamp", "createdAt"):
            if k in msg:
                return str(msg[k])
    return ""


def truncate(s: str, n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + f"…[+{len(s)-n} chars]"


def is_kb_tool_use(tu: dict) -> bool:
    """A tool_use is a KB event if its serialized input contains the KB marker."""
    try:
        return KB_PATH_MARKER in json.dumps(tu.get("input", {}))
    except Exception:
        return False


PATH_RE = re.compile(
    r"(?:~/Downloads/claude-knowledge|/Users/[^/]+/Downloads/claude-knowledge)"
    r"(?:/[A-Za-z0-9._/-]+)?"
)


def extract_kb_paths(text: str) -> list[str]:
    """Pull KB paths out of a text blob using a strict regex."""
    if not text:
        return []
    out = []
    for m in PATH_RE.finditer(text):
        p = m.group(0).rstrip(".,;:'\"`)]")
        if p.startswith("~"):
            p = str(Path(p).expanduser())
        out.append(p)
    seen = set()
    deduped = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped[:8]


def parse_since(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"--since must be YYYY-MM-DD, got {s!r}")


def event_dt(event) -> datetime | None:
    ts = event_timestamp(event)
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def session_summary(events):
    """Pre-pass: build per-event metadata for context windows."""
    summaries = []
    for idx, ev in events:
        role = event_role(ev)
        if role == "assistant":
            text = event_text(ev)
            tus = list(event_tool_uses(ev))
            summaries.append({"idx": idx, "role": "assistant", "text": text,
                              "tool_uses": tus, "raw": ev})
        elif role == "user":
            text = event_text(ev)
            results = list(event_tool_results(ev))
            summaries.append({"idx": idx, "role": "user", "text": text,
                              "tool_results": results, "raw": ev})
        else:
            summaries.append({"idx": idx, "role": role, "raw": ev})
    return summaries


def find_kb_events(summaries):
    """Yield (summary_index, tool_use_index_within_msg, tool_use_dict)."""
    for i, s in enumerate(summaries):
        if s.get("role") != "assistant":
            continue
        for j, tu in enumerate(s.get("tool_uses", [])):
            if is_kb_tool_use(tu):
                yield i, j, tu


def find_tool_result(summaries, start_idx, tool_use_id):
    """Walk forward looking for the matching tool_result (typically next user msg)."""
    for s in summaries[start_idx + 1: start_idx + 6]:
        if s.get("role") != "user":
            continue
        for tr in s.get("tool_results", []):
            if tr.get("tool_use_id") == tool_use_id:
                content = tr.get("content")
                if isinstance(content, list):
                    chunks = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            chunks.append(c.get("text", ""))
                    return "\n".join(chunks)
                if isinstance(content, str):
                    return content
                return json.dumps(content)
    return ""


def collect_event(summaries, kb_idx, tu_pos, tu, project, session_id):
    """Build the structured event row for one KB tool_use."""
    s = summaries[kb_idx]
    ev = s["raw"]

    trigger_user_msg = ""
    for back in summaries[max(0, kb_idx - 8): kb_idx][::-1]:
        if back.get("role") == "user" and back.get("text"):
            trigger_user_msg = back["text"]
            break

    trigger_assistant_intent = s.get("text", "") or ""

    tool_name = tu.get("name", "")
    inp = tu.get("input", {})
    try:
        query = json.dumps(inp, ensure_ascii=False)
    except Exception:
        query = str(inp)

    result_preview = find_tool_result(summaries, kb_idx, tu.get("id", ""))

    next_tools = []
    next_text_chunks = []
    did_read_more_kb = False
    did_edit_after = False
    seen_assistant_msgs = 0
    seen_tool_uses = 0
    edits_window = 0

    for s2 in summaries[kb_idx + 1:]:
        if s2.get("role") == "assistant":
            seen_assistant_msgs += 1
            if seen_assistant_msgs <= NEXT_TEXT_WINDOW and s2.get("text"):
                next_text_chunks.append(s2["text"])
            for tu2 in s2.get("tool_uses", []):
                if seen_tool_uses == 0 and tu2.get("id") == tu.get("id"):
                    continue
                seen_tool_uses += 1
                edits_window += 1
                if seen_tool_uses <= NEXT_TOOL_WINDOW:
                    nt_input = tu2.get("input", {})
                    try:
                        nt_input_str = json.dumps(nt_input, ensure_ascii=False)
                    except Exception:
                        nt_input_str = str(nt_input)
                    next_tools.append({
                        "name": tu2.get("name", ""),
                        "input": truncate(nt_input_str, 400),
                    })
                    if is_kb_tool_use(tu2):
                        did_read_more_kb = True
                if tu2.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    if edits_window <= EDIT_DETECT_WINDOW:
                        did_edit_after = True
        if seen_tool_uses >= NEXT_TOOL_WINDOW and seen_assistant_msgs >= NEXT_TEXT_WINDOW:
            break

    return {
        "session_id": session_id,
        "project": project,
        "timestamp": event_timestamp(ev),
        "tool_name": tool_name,
        "tool_use_id": tu.get("id", ""),
        "trigger_user_msg": truncate(trigger_user_msg, TRIGGER_CHARS),
        "trigger_assistant_intent": truncate(trigger_assistant_intent, TRIGGER_CHARS),
        "query": truncate(query, 2000),
        "result_preview": truncate(result_preview, RESULT_CHARS),
        "next_5_tools": next_tools,
        "next_assistant_text": truncate("\n---\n".join(next_text_chunks), 2000),
        "did_read_more_kb": did_read_more_kb,
        "did_edit_after": did_edit_after,
        "kb_paths_in_query": extract_kb_paths(query),
    }


JUDGE_PROMPT = """You are auditing whether Claude's access to a cross-project knowledge base (KB) was useful.

Below is one KB-access event from a real coding session. Decide:
1. usefulness: did the KB content visibly inform what Claude did next?
2. staleness: did Claude treat the KB content as current, borderline, or stale?
3. why: 1-2 sentences explaining your call.

Output ONLY a single JSON object with this exact shape (no prose, no fences):
{"usefulness":"applied|consulted_no_action|contradicted|unrelated_match|unknown","staleness":"current|borderline|stale|unknown","kb_paths_referenced":["..."],"reason":"..."}

Definitions:
- applied: KB content visibly informed the next action (Claude read referenced source, edited code per the advice, or cited a fact from KB)
- consulted_no_action: KB was read/grepped but no follow-up action grounded in the result
- contradicted: Claude noticed KB was wrong/outdated and went a different direction
- unrelated_match: grep matched but the content was not relevant to the actual task
- unknown: insufficient context to tell

EVENT
project: {project}
session: {session_id}
timestamp: {timestamp}
tool: {tool_name}

what claude was doing (preceding assistant text):
{trigger_assistant_intent}

most recent user message before this:
{trigger_user_msg}

the KB tool call:
{query}

result that came back:
{result_preview}

next {N} tool calls in same session:
{next_tools_str}

assistant text in next {M} turns:
{next_assistant_text}

heuristics: did_read_more_kb={did_read_more_kb}, did_edit_after={did_edit_after}

Output JSON only.
"""


def call_judge(event: dict, model: str) -> dict:
    next_tools_str = "\n".join(
        f"- {t['name']}: {t['input']}" for t in event.get("next_5_tools", [])
    ) or "(none)"
    prompt = JUDGE_PROMPT.format(
        project=event["project"],
        session_id=event["session_id"],
        timestamp=event.get("timestamp", ""),
        tool_name=event["tool_name"],
        trigger_assistant_intent=event["trigger_assistant_intent"] or "(none)",
        trigger_user_msg=event["trigger_user_msg"] or "(none)",
        query=event["query"],
        result_preview=event["result_preview"] or "(empty)",
        next_tools_str=next_tools_str,
        next_assistant_text=event["next_assistant_text"] or "(none)",
        did_read_more_kb=event["did_read_more_kb"],
        did_edit_after=event["did_edit_after"],
        N=NEXT_TOOL_WINDOW,
        M=NEXT_TEXT_WINDOW,
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            capture_output=True, text=True, timeout=120,
        )
        out = proc.stdout.strip()
        start = out.find("{")
        end = out.rfind("}")
        if start == -1 or end == -1:
            return {"usefulness": "unknown", "staleness": "unknown",
                    "kb_paths_referenced": [], "reason": f"judge returned non-JSON: {out[:200]}"}
        return json.loads(out[start:end + 1])
    except subprocess.TimeoutExpired:
        return {"usefulness": "unknown", "staleness": "unknown",
                "kb_paths_referenced": [], "reason": "judge timeout"}
    except Exception as e:
        return {"usefulness": "unknown", "staleness": "unknown",
                "kb_paths_referenced": [], "reason": f"judge error: {e}"}


def load_existing_judgments() -> dict:
    """Re-use prior judgments keyed by (session_id, tool_use_id)."""
    if not EVENTS_OUT.exists():
        return {}
    out = {}
    for _, ev in parse_jsonl_lines(EVENTS_OUT):
        if "judge" in ev and ev.get("tool_use_id"):
            out[(ev["session_id"], ev["tool_use_id"])] = ev["judge"]
    return out


def write_events(events: list[dict]):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_OUT.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def render_report(events: list[dict], judged: bool) -> str:
    by_proj: dict[str, list[dict]] = {}
    by_session: dict[str, set] = {}
    for e in events:
        by_proj.setdefault(e["project"], []).append(e)
        by_session.setdefault(e["session_id"], set()).add(e["project"])

    n_total = len(events)
    n_sessions = len(by_session)

    usefulness_counts = {"applied": 0, "consulted_no_action": 0,
                         "contradicted": 0, "unrelated_match": 0, "unknown": 0}
    staleness_counts = {"current": 0, "borderline": 0, "stale": 0, "unknown": 0}
    if judged:
        for e in events:
            j = e.get("judge", {})
            u = j.get("usefulness", "unknown")
            usefulness_counts[u] = usefulness_counts.get(u, 0) + 1
            s = j.get("staleness", "unknown")
            staleness_counts[s] = staleness_counts.get(s, 0) + 1

    path_hits: dict[str, list[dict]] = {}
    for e in events:
        for p in e.get("kb_paths_in_query", []):
            path_hits.setdefault(p, []).append(e)
        if judged:
            for p in e.get("judge", {}).get("kb_paths_referenced", []) or []:
                path_hits.setdefault(p, []).append(e)

    lines = []
    lines.append(f"# KB Usage Report ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    lines.append(f"> Sessions touching KB: {n_sessions} | KB events: {n_total}")
    lines.append(f"> Judged: {'yes' if judged else 'no (heuristics only)'}")
    lines.append("")
    lines.append("## Per-project engagement")
    lines.append("")
    lines.append("| Project | Sessions | Events | Events/session | did_edit_after | did_read_more_kb |")
    lines.append("|---|---|---|---|---|---|")
    for proj, evs in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        sess = {e["session_id"] for e in evs}
        ed = sum(1 for e in evs if e["did_edit_after"])
        rd = sum(1 for e in evs if e["did_read_more_kb"])
        per = len(evs) / max(1, len(sess))
        lines.append(f"| {proj} | {len(sess)} | {len(evs)} | {per:.1f} | {ed} | {rd} |")
    lines.append("")

    if judged:
        lines.append("## Usefulness breakdown")
        lines.append("")
        lines.append("| Bucket | Count | % |")
        lines.append("|---|---|---|")
        for k, c in usefulness_counts.items():
            pct = (c / n_total * 100) if n_total else 0
            lines.append(f"| {k} | {c} | {pct:.1f}% |")
        lines.append("")
        lines.append("## Staleness breakdown")
        lines.append("")
        for k, c in staleness_counts.items():
            lines.append(f"- {k}: {c}")
        lines.append("")

    lines.append("## Top KB paths referenced")
    lines.append("")
    lines.append("| Path | Hits | Application rate |")
    lines.append("|---|---|---|")
    for path, hits in sorted(path_hits.items(), key=lambda kv: -len(kv[1]))[:15]:
        if judged:
            applied = sum(1 for h in hits if h.get("judge", {}).get("usefulness") == "applied")
            rate = f"{applied}/{len(hits)} ({applied / len(hits) * 100:.0f}%)"
        else:
            rate = "—"
        lines.append(f"| {path} | {len(hits)} | {rate} |")
    lines.append("")

    if judged:
        applied_examples = [e for e in events if e.get("judge", {}).get("usefulness") == "applied"][:3]
        stale_examples = [e for e in events if e.get("judge", {}).get("usefulness") == "contradicted"
                          or e.get("judge", {}).get("staleness") == "stale"][:3]
        unrelated_examples = [e for e in events if e.get("judge", {}).get("usefulness") == "unrelated_match"][:3]

        def render_example(e):
            j = e.get("judge", {})
            return (f"\n#### {e['project']} — {e.get('timestamp', '')}\n"
                    f"- query: `{truncate(e['query'], 200)}`\n"
                    f"- judge: {j.get('usefulness')} / {j.get('staleness')}\n"
                    f"- reason: {j.get('reason', '')}\n")

        lines.append("## Examples — applied")
        for e in applied_examples:
            lines.append(render_example(e))
        lines.append("\n## Examples — stale / contradicted")
        for e in stale_examples:
            lines.append(render_example(e))
        lines.append("\n## Examples — unrelated matches (suggests bad triggers)")
        for e in unrelated_examples:
            lines.append(render_example(e))
    lines.append("")
    lines.append(f"## Raw data\n- {EVENTS_OUT}")
    return "\n".join(lines)


def main():
    args = parse_args()
    since_dt = parse_since(args.since)

    jsonls = discover_jsonls(args.project)
    if not jsonls:
        sys.exit("no JSONLs found under ~/.claude/projects/-Users-cm-Downloads-*/")

    print(f"scanning {len(jsonls)} JSONL files…")
    all_events = []
    for proj, path in jsonls:
        events = list(parse_jsonl_lines(path))
        if not events:
            continue
        if since_dt:
            keep = False
            for _, ev in events:
                d = event_dt(ev)
                if d and d >= since_dt:
                    keep = True
                    break
            if not keep:
                continue
        summaries = session_summary(events)
        session_id = path.stem
        for kb_idx, tu_pos, tu in find_kb_events(summaries):
            ev = summaries[kb_idx]["raw"]
            d = event_dt(ev)
            if since_dt and d and d < since_dt:
                continue
            all_events.append(collect_event(summaries, kb_idx, tu_pos, tu, proj, session_id))
            if args.limit and len(all_events) >= args.limit:
                break
        if args.limit and len(all_events) >= args.limit:
            break

    print(f"collected {len(all_events)} KB events")
    if not all_events:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        REPORT_OUT.write_text("# KB Usage Report\n\nNo KB events found in the scanned range.\n",
                              encoding="utf-8")
        EVENTS_OUT.write_text("", encoding="utf-8")
        print(f"wrote empty report → {REPORT_OUT}")
        return

    judged = False
    if not args.no_judge:
        prior = load_existing_judgments()
        to_judge = []
        for e in all_events:
            key = (e["session_id"], e["tool_use_id"])
            if key in prior:
                e["judge"] = prior[key]
            else:
                to_judge.append(e)
        if to_judge:
            print(f"judging {len(to_judge)} events with {args.judge_model} ({args.judge_workers} workers)…")
            with ThreadPoolExecutor(max_workers=args.judge_workers) as ex:
                futures = {ex.submit(call_judge, e, args.judge_model): e for e in to_judge}
                done_count = 0
                for fut in as_completed(futures):
                    e = futures[fut]
                    e["judge"] = fut.result()
                    done_count += 1
                    if done_count % 10 == 0 or done_count == len(to_judge):
                        print(f"  judged {done_count}/{len(to_judge)}")
        judged = True

    write_events(all_events)
    report = render_report(all_events, judged)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_OUT.write_text(report, encoding="utf-8")
    print(f"wrote {len(all_events)} events → {EVENTS_OUT}")
    print(f"wrote report → {REPORT_OUT}")


if __name__ == "__main__":
    main()
