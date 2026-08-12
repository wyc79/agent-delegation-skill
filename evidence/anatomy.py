#!/usr/bin/env python3
"""Read a `claude -p --output-format stream-json` transcript and say who did what.

    python3 anatomy.py <stream.jsonl>

**The one thing this exists to get right: `parent_tool_use_id`.** A subagent's
assistant messages are emitted into the parent's stream, not a separate one.
They are distinguishable only by that field being non-null. Filter on
`type == "assistant"` alone and a subagent's Writes and commits read as the
dispatcher's own -- which inverts the finding you are trying to make, because
"the dispatcher wrote the file itself" and "the agent it dispatched wrote the
file" are opposite conclusions drawn from identical-looking events.

What it prints:

  * the dispatcher's own tool calls, in order, timestamped
  * every dispatch: when it went out, when it returned, how long it ran
  * the concurrency profile -- how many agents were live at once, and the dead
    time between the first dispatch and the last, which is what one-dispatch-
    per-message actually costs
  * who committed what
"""
import json
import sys
from datetime import datetime, timezone


def load(path):
    out = []
    for line in open(path):
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def when(ev):
    s = ev.get("timestamp")
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hhmmss(t):
    return t.strftime("%H:%M:%S") if t else "   ?    "


def tool_uses(ev):
    for blk in ((ev.get("message") or {}).get("content") or []):
        if isinstance(blk, dict) and blk.get("type") == "tool_use":
            yield blk


def brief(blk, width=88):
    inp = blk.get("input") or {}
    d = inp.get("command") or inp.get("file_path") or inp.get("description") or ""
    return " ".join(str(d).split())[:width]


def main(path):
    evs = load(path)
    first = next((when(e) for e in evs if when(e)), None)
    last = max([w for w in (when(e) for e in evs) if w] or [None])

    dispatches = {}   # tool_use_id -> record
    own = []          # the dispatcher's own calls
    commits = []

    for ev in evs:
        t = when(ev)
        if ev.get("type") == "assistant":
            mine = ev.get("parent_tool_use_id") is None
            for blk in tool_uses(ev):
                if mine and blk.get("name") in ("Task", "Agent"):
                    dispatches[blk["id"]] = {
                        "desc": (blk.get("input") or {}).get("description") or "?",
                        "start": t, "end": None,
                        "chars": len(json.dumps(blk.get("input") or {})),
                    }
                elif mine:
                    own.append((t, blk.get("name"), brief(blk)))
                cmd = str((blk.get("input") or {}).get("command") or "")
                if "git commit" in cmd:
                    commits.append((t, ev.get("parent_tool_use_id"), " ".join(cmd.split())[:70]))
        # completions arrive as system/task_updated with an epoch-ms end_time
        if ev.get("subtype") == "task_updated" and (ev.get("patch") or {}).get("status") == "completed":
            end = datetime.fromtimestamp((ev["patch"]["end_time"]) / 1000.0, timezone.utc)
            for rec in dispatches.values():
                if rec.get("task_id") == ev.get("task_id"):
                    rec["end"] = end
        if ev.get("subtype") in ("task_progress", "task_notification"):
            tid, tuid = ev.get("task_id"), ev.get("tool_use_id")
            if tuid in dispatches:
                dispatches[tuid]["task_id"] = tid
                if ev.get("subtype") == "task_notification" and ev.get("status") == "completed":
                    dispatches[tuid].setdefault("end", None)

    # second pass now that tool_use_id -> task_id is known
    for ev in evs:
        if ev.get("subtype") == "task_updated" and (ev.get("patch") or {}).get("status") == "completed":
            end = datetime.fromtimestamp((ev["patch"]["end_time"]) / 1000.0, timezone.utc)
            for rec in dispatches.values():
                if rec.get("task_id") == ev.get("task_id"):
                    rec["end"] = end

    cost = None
    for ev in evs:
        if ev.get("type") == "result" and ev.get("total_cost_usd") is not None:
            cost = ev["total_cost_usd"]      # cumulative; last one wins

    print("=" * 78)
    print("%s" % path)
    span = (last - first).total_seconds() / 60.0 if first and last else 0
    print("wall clock %.1f min   (%s -> %s)   cost $%.2f"
          % (span, hhmmss(first), hhmmss(last), cost or 0))
    print("=" * 78)

    print("\n--- the dispatcher's own tool calls (%d) ---" % len(own))
    for t, name, d in own:
        print("  %s %-8s %s" % (hhmmss(t), name, d))

    print("\n--- dispatches (%d) ---" % len(dispatches))
    recs = sorted(dispatches.values(), key=lambda r: r["start"] or first)
    for r in recs:
        dur = (r["end"] - r["start"]).total_seconds() if r["end"] and r["start"] else None
        print("  %-26s sent %s  done %s  ran %s  prompt %d chars"
              % (r["desc"][:26], hhmmss(r["start"]), hhmmss(r["end"]),
                 ("%3.0fs" % dur) if dur else "  ?", r["chars"]))

    if len(recs) > 1:
        sent = [r["start"] for r in recs if r["start"]]
        stagger = (max(sent) - min(sent)).total_seconds()
        ends = [r["end"] for r in recs if r["end"]]
        print("\n  stagger between first and last dispatch: %.0fs" % stagger)
        if ends and sent:
            # peak concurrency, sampled at every event boundary
            marks = sorted(set(sent + ends))
            peak = max(sum(1 for r in recs if r["start"] and r["end"]
                           and r["start"] <= m < r["end"]) for m in marks)
            longest = max((r["end"] - r["start"]).total_seconds()
                          for r in recs if r["start"] and r["end"])
            actual = (max(ends) - min(sent)).total_seconds()
            print("  peak concurrency: %d of %d" % (peak, len(recs)))
            print("  parallel phase ran %.0fs; slowest single job %.0fs; "
                  "cost of the stagger %.0fs" % (actual, longest, actual - longest))

    print("\n--- commits ---")
    for t, parent, cmd in commits:
        who = "DISPATCHER" if parent is None else (
            "agent:%s" % dispatches.get(parent, {}).get("desc", parent)[:20])
        print("  %s %-28s %s" % (hhmmss(t), who, cmd))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
