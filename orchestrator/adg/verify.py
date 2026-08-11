"""Deterministic verification.

Runs the project's own commands. No LLM: this is the ground truth every agent
and reviewer argues from, and it runs before any review so we never pay
reviewer tokens to discover a compile error.

Commands are split fast/slow because on a game engine a five-minute boot inside
a tight fix loop destroys the economics -- fast runs every iteration, slow at
stage boundaries only.
"""

import json
import os
import subprocess
import time

from . import yamlite

CONFIG_NAMES = (".adg.yaml", ".adg.yml", ".delegate.yaml")

DEFAULT = {
    "fast": [],
    "slow": [],
    "hotspots": [],
    "ignore": [],
}

# Build output and caches are not authored changes. Counting them produces
# phantom scope violations and inflates churn signals.
ALWAYS_IGNORE = [
    "__pycache__/*", "*.pyc", "*.pyo", ".pytest_cache/*",
    "node_modules/*", "dist/*", "build/*", "target/*",
    ".godot/*", "Library/*", "Temp/*", "obj/*", "Binaries/*",
    "Intermediate/*", "DerivedDataCache/*", "Saved/*", "*.generated.h",
]


def load_project_config(repo):
    """Per-project commands. Absent config is legal -- it just means no
    automated verification, which is reported honestly rather than faked."""
    for name in CONFIG_NAMES:
        path = os.path.join(repo, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cfg = yamlite.load(fh.read()) or {}
            merged = dict(DEFAULT)
            merged.update(cfg)
            merged["_source"] = name
            return merged
    return dict(DEFAULT, _source=None)


class Result:
    def __init__(self, run_id, ok, steps, skipped):
        self.run_id, self.ok, self.steps, self.skipped = run_id, ok, steps, skipped

    def summary(self):
        if not self.steps:
            return "no checks configured"
        passed = sum(1 for s in self.steps if s["ok"])
        return "%d/%d checks passed" % (passed, len(self.steps))

    def failures(self):
        return [s for s in self.steps if not s["ok"]]

    def as_dict(self):
        return {"run_id": self.run_id, "ok": self.ok, "steps": self.steps,
                "skipped": self.skipped, "summary": self.summary()}


_RUN_SEQ = [0]
_RUN_LOCK = __import__("threading").Lock()


def _run_id(tier):
    with _RUN_LOCK:
        _RUN_SEQ[0] += 1
        n = _RUN_SEQ[0]
    return "v-%s-%s-%03d" % (tier, time.strftime("%Y%m%d-%H%M%S", time.gmtime()), n)


def run(task, repo, cwd, tier="fast", timeout=900):
    """Run one tier of checks and persist the output under verify/<run-id>."""
    cfg = load_project_config(repo)
    commands = cfg.get(tier) or []
    run_id = _run_id(tier)
    steps = []
    for cmd in commands:
        started = time.time()
        try:
            p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                               text=True, timeout=timeout)
            out, code = (p.stdout or "") + (p.stderr or ""), p.returncode
        except subprocess.TimeoutExpired:
            out, code = "timed out after %ds" % timeout, 124
        steps.append({
            "cmd": cmd, "ok": code == 0, "exit": code,
            "seconds": round(time.time() - started, 1),
            "output": out[-4000:],  # tail is where failures live
        })
    skipped = [] if tier == "slow" else list(cfg.get("slow") or [])
    result = Result(run_id, all(s["ok"] for s in steps) if steps else True, steps, skipped)
    task.write_text(os.path.join("verify", run_id + ".json"),
                    json.dumps(result.as_dict(), indent=2) + "\n")
    return result


def is_ignored(path, extra=()):
    import fnmatch
    for pat in list(ALWAYS_IGNORE) + list(extra or []):
        if fnmatch.fnmatch(path, pat) or path.startswith(pat.rstrip("*/") + "/"):
            return True
    return False


def changed_files(repo, cwd, base_ref, ignore=()):
    """Actual files touched, from git rather than self-report -- this is what
    the mechanical scope check compares against the plan. Generated
    output is filtered out so it cannot masquerade as an authored change."""
    p = subprocess.run(["git", "diff", "--name-only", base_ref],
                       cwd=cwd, capture_output=True, text=True)
    tracked = [f for f in p.stdout.splitlines() if f.strip()]
    q = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                       cwd=cwd, capture_output=True, text=True)
    everything = set(tracked) | {f for f in q.stdout.splitlines() if f.strip()}
    return sorted(f for f in everything if not is_ignored(f, ignore))


def scope_violations(files, allowed_globs, case_insensitive=None):
    """Files outside every declared scope. Case-insensitive on Windows and
    macOS, where Foo.cs and foo.cs are one file and a case-sensitive compare
    would silently grant two agents write access to it (b)."""
    import fnmatch
    import sys
    if case_insensitive is None:
        case_insensitive = sys.platform.startswith(("win", "darwin"))
    def norm(s):
        return s.lower() if case_insensitive else s
    globs = [norm(g) for g in (allowed_globs or [])]
    out = []
    for f in files:
        nf = norm(f)
        if not any(fnmatch.fnmatch(nf, g) or nf.startswith(g.rstrip("*/") + "/")
                   for g in globs):
            out.append(f)
    return out


def scopes_overlap(a, b):
    """Do two write scopes touch the same files? Glob-vs-glob, so it is
    approximate -- deliberately erring toward "yes", because a false overlap
    costs wall clock and a missed one costs an unmergeable conflict."""
    import fnmatch
    import sys
    ci = sys.platform.startswith(("win", "darwin"))
    def norm(g):
        g = g.lower() if ci else g
        return g.rstrip("/*")
    for x in a or []:
        for y in b or []:
            nx, ny = norm(x), norm(y)
            if not nx or not ny:
                return True
            if nx == ny or nx.startswith(ny + "/") or ny.startswith(nx + "/"):
                return True
            if fnmatch.fnmatch(nx, y.lower() if ci else y) or \
               fnmatch.fnmatch(ny, x.lower() if ci else x):
                return True
    return False
