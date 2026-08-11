"""Human-facing briefs.

Humans never see raw internal artifacts. Everything at a gate is rendered into
plain language for a competent programmer who has never seen this repo.

The jargon lint is the part with teeth: a brief containing bare protocol tokens
(AC-2, st-3, dev-1, rung 2, REQUEST_CHANGES) is rejected. Cheap to enforce, and
it catches the failure mode where a summary is technically accurate and
completely unreadable.
"""

import re

# Bare ids that must be expanded on first use, e.g. "the requirement that old
# saves still load (AC-2)" rather than a naked "AC-2".
_BARE = [
    (re.compile(r"(?<![\w(])AC-\d+"), "acceptance-criterion id"),
    (re.compile(r"(?<![\w(])st-\d[\w-]*"), "subtask id"),
    (re.compile(r"(?<![\w(])dev-\d+"), "deviation id"),
    (re.compile(r"(?<![\w(])f-\d+"), "finding id"),
    (re.compile(r"\brung \d+", re.I), "escalation-ladder jargon"),
    (re.compile(r"\b(REQUEST_CHANGES|ESCALATE_TO_HUMAN|REPLAN|NEEDS_HUMAN)\b"), "verdict enum"),
    (re.compile(r"\b(test_stuck|scope_overrun|edit_churn|plan_conflict)\b"), "signal name"),
]


def lint(text):
    """Return a list of jargon problems. Empty means the brief is clean."""
    problems = []
    for pattern, label in _BARE:
        for m in pattern.finditer(text):
            token = m.group(0)
            # Allowed when expanded: "... (AC-2)" -- i.e. inside parentheses
            # directly after prose.
            before = text[max(0, m.start() - 1):m.start()]
            if before == "(":
                continue
            problems.append("%s used bare: %r -- explain it in words first" % (label, token))
    return problems


def _files_by_area(files, limit=12):
    from collections import defaultdict
    groups = defaultdict(list)
    for f in files:
        parts = f.split("/")
        groups["/".join(parts[:2]) if len(parts) > 1 else "(top level)"].append(f)
    lines = []
    for area in sorted(groups)[:limit]:
        names = groups[area]
        shown = ", ".join(n.split("/")[-1] for n in names[:4])
        more = "" if len(names) <= 4 else " (+%d more)" % (len(names) - 4)
        lines.append("- **%s** — %s%s" % (area, shown, more))
    return lines


def _cost_section(state):
    """Who actually did the work, and what it cost. Users should not have to
    open task.json to find out which provider ran their code, and an unreported
    cost is shown as unreported rather than folded into a total as zero."""
    spent = state.get("spent", {})
    limits_ = state.get("limits", {})
    md = ["## Who did the work, and what it cost", ""]

    rows = {}
    for h in state.get("delegation_history") or []:
        if h.get("outcome") == "quota_exhausted":
            # A seat that refused the call did no work and cost nothing. Counting
            # it as a run that "reported no cost" appends a warning that the real
            # total is higher, which is the opposite of true.
            continue
        in_pane = (h.get("adapter") == "herdr")
        key = (h.get("model") or "unknown", h.get("channel") or "unknown", in_pane)
        row = rows.setdefault(key, {"steps": [], "usd": 0.0, "silent": 0, "est": False})
        row["steps"].append(h.get("role") or h.get("stage") or "step")
        if isinstance(h.get("usd"), (int, float)):
            row["usd"] += float(h["usd"])
            row["est"] = row["est"] or bool(h.get("usd_estimated"))
        else:
            row["silent"] += 1

    # Two different facts, and collapsing them misleads. An agent in a terminal
    # pane *cannot* be billed -- that is how the mode works, and the user chose
    # it. A subprocess that returned no cost is a surprise worth flagging.
    paned = sum(r["silent"] for k, r in rows.items() if k[2])
    unexplained = sum(r["silent"] for k, r in rows.items() if not k[2])

    if rows:
        md += ["| Model | Provider | Did | Cost |", "|---|---|---|---|"]
        for (model, channel, in_pane), row in sorted(rows.items()):
            cost = ("~$%.2f (estimated from tokens)" if row["est"] else "$%.2f") % row["usd"] \
                if row["usd"] else ""
            if row["silent"]:
                note = ("cannot be measured in a pane" if in_pane
                        else "%d run%s reported none" % (row["silent"],
                                                         "" if row["silent"] == 1 else "s"))
                cost = "%s (%s)" % (cost, note) if cost else note
            md.append("| `%s` | %s%s | %s | %s |" % (
                model, channel, " — in a terminal pane" if in_pane else "",
                ", ".join(sorted(set(row["steps"]))), cost))
        md.append("")

    total = float(spent.get("usd", 0.0))
    cap = float(limits_.get("max_cost_usd", 0))
    md.append("- Total billed: $%.2f of a $%.2f cap." % (total, cap))
    if paned:
        md.append("- Agents running in a terminal pane cannot be billed — you can "
                  "watch them work, but they report nothing back, so their spend is "
                  "absent from the total above and the cap does not apply to them. "
                  "Re-run with `--no-panes` if you need the cap enforced.")
    if unexplained:
        md.append("- Some runs outside a pane reported no cost, which is unexpected. "
                  "The real total is higher than the figure above.")
    md.append("")
    return md


# An entry, not just bytes in the file. The log opens with a heading, prose and
# a fenced example -- `templates/deviations.md` is what agents are told to start
# from -- so "the file is non-empty" says nothing about whether anything was
# logged. The old guard asked whether the file failed to start with `#`, which
# the template always does: every task that used the template hid every
# deviation from every brief, however many were appended below.
#
# The entry shape is the one `references/deviations.md` documents and the report
# schema's id pattern agrees with: `dev-<subtask-or-role>-<n> | ...` at the start
# of a line. The template's own example carries `<placeholders>` inside the
# angle brackets, so it cannot be mistaken for a real entry.
_DEVIATION = re.compile(r"(?m)^\s*(?:[-*]\s*)?dev-[a-z0-9][a-z0-9-]*\s*\|")


def _has_deviation_entry(text):
    return bool(_DEVIATION.search(text or ""))


def render(task, kind, decision_text, files=(), verify=None, extra=None):
    """Build a gate brief. Decision first: it is what the reader must act on."""
    state = task.state
    md = ["# %s — %s" % (state["id"], kind.replace("_", " ").title()), ""]
    md += ["## What you're being asked", "", decision_text.strip(), ""]

    request = task.read_text("task.md", "").strip()
    if request:
        first = [ln for ln in request.splitlines() if ln.strip() and not ln.startswith("#")]
        md += ["## The task", "", (first[0] if first else "").strip(), ""]

    history = state.get("delegation_history") or []
    if history:
        md += ["## What happened", ""]
        for h in history[-8:]:
            outcome = {"complete": "finished", "escalate": "handed off for more help",
                       "blocked": "got stuck",
                       "quota_exhausted": "ran out of its provider's capacity and "
                                          "handed over to another one",
                       }.get(h.get("outcome"), h.get("outcome", "ran"))
            md.append("- The %s step %s." % (h.get("role", "agent"), outcome))
        md.append("")

    if files:
        md += ["## What changed", ""] + _files_by_area(list(files)) + [""]

    if verify is not None:
        md += ["## Evidence", "", "- Automated checks: %s." % verify.summary()]
        if verify.skipped:
            md.append("- Not run: %s. These were skipped deliberately, so they are "
                      "not evidence of anything." % "; ".join(verify.skipped))
        for f in verify.failures()[:3]:
            md.append("- Failed: `%s`" % f["cmd"])
        md.append("")

    if _has_deviation_entry(task.read_text("deviations.md", "")):
        md += ["## What didn't go to plan", "", "See the deviations log for details.", ""]

    md += _cost_section(state)

    if extra:
        md += [extra.strip(), ""]

    md += ["## Full detail", "",
           "Everything above is a summary. The underlying files are in:", "",
           "`%s`" % task.path, ""]
    return "\n".join(md)


def write(task, kind, decision_text, polish=None, **kw):
    """Render, optionally rewrite in plain language, lint, and persist. A brief
    that fails the lint is still written -- the human still needs it -- but the
    problems are surfaced rather than swallowed."""
    text = render(task, kind, decision_text, **kw)
    if polish:
        text = polish(text)
    problems = lint(text)
    task.write_text("brief.md", text)
    return text, problems
