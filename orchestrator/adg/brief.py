"""Human-facing briefs.

Humans never see raw internal artifacts. Everything at a gate is rendered into
plain language for a competent programmer who has never seen this repo.

The jargon lint is the part with teeth: a brief containing bare protocol tokens
(AC-2, st-3, NEEDS_HUMAN) is rejected. Cheap to enforce, and it catches the
failure mode where a summary is technically accurate and completely unreadable.
"""

import re

# Bare ids that must be expanded on first use, e.g. "the requirement that old
# saves still load (AC-2)" rather than a naked "AC-2".
#
# The list is shorter than it was: `dev-<n>`, `f-<n>`, `rung <n>`,
# REQUEST_CHANGES and the signal-type names were vocabulary of the role
# protocol, and a lint for words nothing can emit is a rule that never fires.
# `AC-<n>` stays because a caller's own acceptance ids reach the brief through
# the job blocks, and `st-<n>` because job ids do.
_BARE = [
    (re.compile(r"(?<![\w(])AC-\d+"), "acceptance-criterion id"),
    (re.compile(r"(?<![\w(])st-\d[\w-]*"), "job id"),
    (re.compile(r"\b(ESCALATE_TO_HUMAN|NEEDS_HUMAN)\b"), "status enum"),
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
            # And allowed as a table cell of its own. The rule is about PROSE
            # that leans on an id the reader has never been told the meaning
            # of; an identifier column is the one place a bare id is the
            # clearest thing to write, and there is nothing to expand it into.
            if _is_own_cell(text, m.start(), m.end()):
                continue
            problems.append("%s used bare: %r -- explain it in words first" % (label, token))
    return problems


def _is_own_cell(text, start, end):
    """Is this match the entire contents of a markdown table cell?"""
    left = text.rfind("|", 0, start)
    right = text.find("|", end)
    if left < 0 or right < 0 or "\n" in text[left:start] or "\n" in text[end:right]:
        return False
    return not text[left + 1:start].strip() and not text[end:right].strip()


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


def _by_job(state, limit=8):
    """What each job touched, and what it touched outside the boundary it was
    given.

    This is the whole of what `delegate` measures. `_finish_subtask` computes
    `scope_violations` per job and writes it into task.json, and it reached the
    run's stdout and nothing else -- so the brief listed every changed file
    flattened into one alphabetical heap, with no way to tell which job wrote
    what or whether any of it was out of bounds. Meanwhile the paragraph the
    merge gate prints beside it points at "the scope column above" as the
    evidence a human is supposed to weigh.

    A caller reading a brief is deciding whether to land a change built by
    agents it never saw. "st-2 also wrote build.gradle" is the single most
    useful sentence in that decision, and it was being computed and thrown away.
    """
    subs = [s for s in (state.get("subtasks") or []) if s.get("actual_files")]
    if not subs:
        return []
    # The own-checks column appears only when some job declared one. On a plan
    # that uses none it would be a column of "none declared" saying nothing.
    any_own = any((s.get("own_checks") or {}).get("declared") for s in subs)
    head = "| Job | Files | Outside its scope |"
    rows = [head + (" Own checks |" if any_own else ""),
            "|---|---|---|" + ("---|" if any_own else "")]
    for sub in subs[:limit]:
        files = sub.get("actual_files") or []
        out = set(sub.get("scope_violations") or [])
        shown = ", ".join("`%s`" % f for f in files[:6])
        if len(files) > 6:
            shown += " (+%d more)" % (len(files) - 6)
        row = "| %s | %s | %s |" % (
            sub.get("id", "?"), shown,
            ", ".join("**`%s`**" % f for f in sorted(out)) if out else "—")
        if any_own:
            # "none declared" is not a pass, and must not read as one beside a
            # job that ran three. A job with no checks of its own has only the
            # project-wide commands standing behind it.
            oc = sub.get("own_checks") or {}
            n = oc.get("declared") or 0
            if not n:
                cell = "none declared"
            elif oc.get("failed"):
                cell = "**failed: %s**" % ", ".join("`%s`" % c for c in oc["failed"])
            else:
                cell = "%d/%d passed" % (oc.get("passed") or 0, n)
            row += " %s |" % cell
        rows.append(row)
    if len(subs) > limit:
        rows.append("| … | %d more jobs | |" % (len(subs) - limit))
    flagged = sorted({f for s in subs for f in (s.get("scope_violations") or [])})
    if flagged:
        rows += ["",
                 "%d file(s) were written outside the scope their job declared. "
                 "Nothing was reverted — this is a measurement, and what to do "
                 "about it is yours." % len(flagged)]
    return rows


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
        # `panes` when the record has it. The old inference from the adapter
        # name is kept only for task files written before it existed: a
        # `--no-panes` run is HerdrAdapter(panes=False), still named "herdr",
        # so inferring put the "cost is unreliable here" caveat on exactly the
        # rows whose cost the CLI did report.
        in_pane = bool(h["panes"]) if "panes" in h else (h.get("adapter") == "herdr")
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

    # Per job when the run got that far, by area otherwise. The paused brief is
    # the "otherwise": it is written from `_quota_park` with the union of what
    # anything managed before the seats went dark, and a scope column there
    # would be reporting a boundary against work that is not finished.
    per_job = _by_job(state) if kind == "merge" else []
    if per_job:
        md += ["## What changed", ""] + per_job + [""]
    elif files:
        md += ["## What changed", ""] + _files_by_area(list(files)) + [""]

    if verify is not None:
        md += ["## Evidence", "", "- Automated checks: %s." % verify.summary()]
        if verify.skipped:
            md.append("- Not run: %s. These were skipped deliberately, so they are "
                      "not evidence of anything." % "; ".join(verify.skipped))
        for f in verify.failures()[:3]:
            # A job's own check names its job. Without this a failure is a
            # property of the batch -- "1/2 checks passed, sh check-grade.sh" --
            # and finding which of four jobs caused it means reading the diff.
            if f.get("required") and f.get("job"):
                owner = " (%s's own check)" % f["job"]
            elif f.get("tier") == "gate":
                # Named as gate-stage because it means something different from
                # the others: it ran once over the assembled change, after every
                # job was done, and it did NOT reject anything. It is here to be
                # weighed, which is a different question from "did the work
                # build" and should not read as the same kind of red.
                owner = " (gate-stage check — it did not reject the change; " \
                        "this is for you to weigh)"
            else:
                owner = ""
            md.append("- Failed: `%s`%s" % (f["cmd"], owner))
            # The last lines, not just the command. A slow check is often the
            # only thing that can say whether the change works, and "Failed:
            # `sh check-grade.sh`" tells a human that something is wrong while
            # withholding the one line that says what -- which is the line the
            # check was configured to produce.
            tail = [ln for ln in (f.get("output") or "").splitlines() if ln.strip()][-3:]
            md += ["      %s" % ln[:160] for ln in tail]
        md.append("")

    # No "what didn't go to plan" section. It rendered when `deviations.md`
    # held an entry, and the protocol that told agents to append one -- with the
    # template and the reference describing the format -- is gone. A departure
    # from the plan now lands where the caller reads it: the job's own report,
    # in `summary` and `signals`.

    md += _cost_section(state)

    if extra:
        md += [extra.strip(), ""]

    md += ["## Full detail", "",
           "Everything above is a summary. The underlying files are in:", "",
           "`%s`" % task.path, ""]
    return "\n".join(md)


def write(task, kind, decision_text, **kw):
    """Render, lint, and persist. A brief that fails the lint is still written
    -- the human still needs it -- but the problems are surfaced rather than
    swallowed.

    There is no rewrite step. A `polish` callable used to run the rendered text
    through a model before linting it, and nothing has passed one since the
    reporter role was removed: a brief is assembled from the record, and paying
    a model to restate the record is the kind of step this program leaves to
    whoever is reading."""
    text = render(task, kind, decision_text, **kw)
    problems = lint(text)
    task.write_text("brief.md", text)
    return text, problems
