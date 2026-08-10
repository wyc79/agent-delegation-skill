"""Human-facing briefs (DESIGN.md §9.4).

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
                       "blocked": "got stuck"}.get(h.get("outcome"), h.get("outcome", "ran"))
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

    deviations = task.read_text("deviations.md", "").strip()
    if deviations and not deviations.startswith("#"):
        md += ["## What didn't go to plan", "", "See the deviations log for details.", ""]

    spent = state.get("spent", {})
    limits_ = state.get("limits", {})
    md += ["## Cost", "", "- Spent $%.2f of a $%.2f cap." % (
        float(spent.get("usd", 0.0)), float(limits_.get("max_cost_usd", 0))), ""]

    if extra:
        md += [extra.strip(), ""]

    md += ["## Full detail", "",
           "Everything above is a summary. The underlying files are in:", "",
           "`%s`" % task.path, ""]
    return "\n".join(md)


def write(task, kind, decision_text, **kw):
    """Render, lint, and persist. A brief that fails the lint is still written
    (the human still needs it) but the problems are surfaced, not swallowed."""
    text = render(task, kind, decision_text, **kw)
    problems = lint(text)
    task.write_text("brief.md", text)
    return text, problems
