"""Optional code-winnow integration (https://github.com/wyc79/code-winnow-skill).

Only the **deterministic scanner** is used here. `scripts/scan.py` is stdlib-only
and finishes in well under a second, which makes it a deterministic check in the
sense of D5 rather than an exception to it -- so it runs alongside build, test
and lint, including on tasks that skip LLM review. code-winnow's five-judge
review pipeline is a different, far more expensive thing and is not called from
here.

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


def run(task, scan_py, cwd, base_ref, run_id):
    """Scan the change and persist the raw output. Returns a summary dict, or
    None when the scan could not run -- never a fabricated clean result."""
    cmd = ["python3", scan_py, "--scope", "branch", "--base", base_ref, "--json"]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": False, "why": "scanner failed to run: %s" % e}
    if p.returncode not in (0, 1) or not p.stdout.strip():
        return {"ran": False, "why": "scanner exited %s: %s"
                                     % (p.returncode, (p.stderr or "").strip()[:200])}
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
    findings = data.get("findings") or data.get("candidates") or []
    out = {"ran": True, "run_id": run_id, "total": len(findings), "notable": []}
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
    if not summary["notable"]:
        return "Chaff scan: %d minor note(s), nothing significant." % summary["total"]
    lines = ["Chaff scan flagged %d item(s) worth a look (advisory — these are "
             "style and robustness notes, not requirement failures):" % len(summary["notable"])]
    for n in summary["notable"][:10]:
        lines.append("  [%s] %s:%s — %s" % (n["severity"], n["path"], n["line"], n["message"]))
    return "\n".join(lines)
