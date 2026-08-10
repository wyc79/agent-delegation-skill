"""Optional code-winnow integration (https://github.com/wyc79/code-winnow-skill).

Only the **deterministic scanner** is used here. `scripts/scan.py` is stdlib-only
and finishes in well under a second, which makes it a deterministic check in the
sense of D5 rather than an exception to it -- so it runs alongside build, test
and lint, including on tasks that skip LLM review. code-winnow's six judgment
passes and their arbitrated merge are a different, far more expensive thing and
are not called from here.

The skill is **referenced, never vendored**. Copying its scanner in would mean
maintaining a fork of someone else's actively developed tool, and a stale fork
that reports nothing looks exactly like a clean scan. Absent, this degrades to
"no scan ran", reported honestly rather than silently.

Its findings are advisory. Authority in this system comes from acceptance
criteria, the plan, and the deterministic checks -- a style judgement that could
block a merge would reintroduce the taste loop the reviewer rules exist to
prevent. The one exception is already covered elsewhere: a file outside its
declared scope is caught mechanically by verify.scope_violations.
"""

import json
import os
import subprocess

ENV_OVERRIDE = "ADG_WINNOW_SCAN"

# Standard skill locations across runtimes, in the order a human would expect
# a project-local install to win.
SEARCH = (
    os.path.join(".claude", "skills", "code-winnow", "scripts", "scan.py"),
    os.path.join(".agents", "skills", "code-winnow", "scripts", "scan.py"),
    os.path.join("~", ".claude", "skills", "code-winnow", "scripts", "scan.py"),
    os.path.join("~", ".cursor", "skills", "code-winnow", "scripts", "scan.py"),
    os.path.join("~", ".agents", "skills", "code-winnow", "scripts", "scan.py"),
)


def find(repo, configured=None):
    """Absolute path to scan.py, or None. Checked once per run by the caller."""
    for cand in (configured, os.environ.get(ENV_OVERRIDE)):
        if cand and os.path.isfile(os.path.expanduser(cand)):
            return os.path.abspath(os.path.expanduser(cand))
    for rel in SEARCH:
        path = os.path.expanduser(rel) if rel.startswith("~") else os.path.join(repo, rel)
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def misconfigured(configured=None):
    """Why an *explicit* path failed to resolve, or None.

    SEARCH covers the runtimes we know; anywhere else is reached by setting
    `winnow_scan` or ADG_WINNOW_SCAN. A typo there would otherwise fall through
    to autodetect and report the same "not installed" as an untouched machine --
    the one case where the honest degradation this module promises reads as a
    lie, because the user did configure it.
    """
    for cand, src in ((configured, "winnow_scan"),
                      (os.environ.get(ENV_OVERRIDE), ENV_OVERRIDE)):
        if cand and not os.path.isfile(os.path.expanduser(cand)):
            return "%s points at %s, which is not a file" % (src, cand)
    return None


def run(task, scan_py, cwd, base_ref, run_id):
    """Scan the change and persist the raw output. Returns a summary dict, or
    None when the scan could not run -- never a fabricated clean result."""
    cmd = ["python3", scan_py, "--scope", "branch", "--base", base_ref, "--json"]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": False, "why": "scanner failed to run: %s" % e}
    # scan.py's contract on this path is 0 = complete, 2 = incomplete -- a file
    # in scope was binary, minified or unreadable. Exit 2 still prints a valid
    # report carrying real findings, so treating it as a failed run discarded
    # them: a P1 in one file was thrown away because a *different* file was
    # minified. (Its exit 1 comes only from --report-name and --meta, which this
    # never calls, so accepting it was dead code.) Partial coverage is a fact to
    # report, not a reason to report nothing.
    if p.returncode not in (0, 2) or not p.stdout.strip():
        detail = (p.stderr or "").strip()[:200]
        return {"ran": False, "why": "scanner exited %s%s"
                                     % (p.returncode, ": " + detail if detail else
                                        " with no diagnostic")}
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return {"ran": False, "why": "scanner output was not JSON"}

    task.write_text(os.path.join("verify", run_id + "-winnow.json"),
                    json.dumps(data, indent=2) + "\n")
    return summarize(data, run_id)


def summarize(data, run_id=None):
    """Normalise defensively: this is another project's schema, and a key that
    moves must degrade to a smaller summary rather than crash the pipeline."""
    # `findings` is the key scan.py has always emitted, on every JSON path
    # including the empty-scope one. There used to be a `candidates` fallback
    # beside it, guessing at a rename that has never happened -- and it could
    # not have helped: a renamed key lands on the `files`-is-nonzero branch
    # below and reports a real scan as zero findings either way. Speculating
    # about another project's schema buys nothing you can test.
    findings = data.get("findings") or []
    # An empty scope is not a clean scan. scan.py answers "no diff found" with
    # exit 0 and zero findings, which is byte-for-byte what a reviewed change
    # that came back clean looks like -- and reporting it as clean is precisely
    # the fabricated result this module's docstring refuses to produce.
    #
    # Findings decide it first, and deliberately: you cannot get a finding out
    # of a file nobody opened. Keying this on `files` alone would mean a renamed
    # key in someone else's schema silently converted every real scan into
    # "nothing was scanned" -- fail-safe in direction, but it buries live P1s,
    # which is the same defect as discarding them on an exit code.
    if not findings:
        # Only an explicit count is a claim about scope. Absent, we cannot tell
        # a clean tree from an unexamined one, and saying so beats guessing.
        if "files" not in data:
            return {"ran": False, "run_id": run_id,
                    "why": "scanner reported no file count — cannot tell a "
                           "clean scan from an empty one"}
        if not data.get("files"):
            return {"ran": False, "run_id": run_id,
                    "why": ("every changed file was skipped as vendored, "
                            "generated or unreadable — nothing was scanned"
                            if data.get("errors") else
                            "no diff in scope — nothing was scanned")}
    out = {"ran": True, "run_id": run_id, "total": len(findings),
           # False when a file in scope could not be read. The findings that did
           # land are still true; the coverage behind them is not whole, and a
           # reader who is not told that will read silence as absence.
           "complete": bool(data.get("complete", True)), "notable": []}
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = (f.get("severity") or "").upper()
        if sev in ("P1", "P2"):
            out["notable"].append({
                "severity": sev,
                "path": f.get("path") or f.get("file") or "?",
                "line": f.get("line"),
                "message": (f.get("message") or f.get("why") or "").strip()[:200],
            })
    out["notable"].sort(key=lambda x: (x["severity"], x["path"], x["line"] or 0))
    return out


def as_text(summary):
    """One short block for a reviewer prompt or a human brief."""
    if not summary:
        return ""
    if not summary.get("ran"):
        return "Chaff scan: did not run (%s)." % summary.get("why", "unavailable")
    # Partial coverage leads, because it changes how everything under it reads.
    lines = ([] if summary.get("complete", True) else
             ["Chaff scan was INCOMPLETE — some files in scope could not be read, "
              "so what follows is partial and absence of a finding proves nothing."])
    if not summary["notable"]:
        lines.append("Chaff scan: %d minor note(s), nothing significant."
                     % summary["total"])
        return "\n".join(lines)
    lines.append("Chaff scan flagged %d item(s) worth a look (advisory — these are "
                 "style and robustness notes, not requirement failures):"
                 % len(summary["notable"]))
    for n in summary["notable"][:10]:
        lines.append("  [%s] %s:%s — %s" % (n["severity"], n["path"], n["line"], n["message"]))
    return "\n".join(lines)
