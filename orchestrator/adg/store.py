"""Task state living outside the repository.

Nothing here ever writes into the working tree. The project key is derived from
the git *common* directory, which is identical for a main checkout and all of
its worktrees -- that is what lets every agent in every worktree find the same
task state with no configuration.

The derivation must stay byte-identical to the agent-facing recipe in
orchestrator/workflows/default/references/task-dir.md, or agents and orchestrator will read
different directories.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import tempfile
import time

TASK_DIR_ENV = "AGENT_DELEGATION_TASK_DIR"
_WINDOWS = sys.platform.startswith("win")


class StoreError(RuntimeError):
    pass


def git(args, cwd, check=True):
    p = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise StoreError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def common_dir(repo):
    """Canonical git common dir: forward slashes, no trailing slash, and
    lowercased on Windows because the filesystem is case-insensitive there."""
    raw = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
    canon = raw.replace("\\", "/").rstrip("/")
    return canon.lower() if _WINDOWS else canon


def project_key(repo):
    cd = common_dir(repo)
    name = os.path.basename(os.path.dirname(cd))
    digest = hashlib.sha256(cd.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (name, digest)


def state_root():
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "agent-delegation")
    if _WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "agent-delegation")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "agent-delegation")


def project_dir(repo):
    return os.path.join(state_root(), "projects", project_key(repo))


def atomic_write(path, text):
    """Write via temp file + replace so a crash never leaves a half file --
    task.json is the resume point and a truncated one loses the run, and
    channels.json is shared between concurrent processes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Task:
    """One task directory. The orchestrator is the only writer of task.json."""

    def __init__(self, path):
        self.path = path
        # Wave threads all mutate this file. Atomic replace prevents a torn
        # file; it does nothing about lost updates, so every read-modify-write
        # goes through one lock. Without it the attempt and spend counters --
        # the things the limits are made of -- silently drop writes.
        self._lock = threading.RLock()

    def mutate(self, fn):
        """Apply fn(state) to task.json under the lock and persist it."""
        with self._lock:
            state = self.state
            fn(state)
            self.write_json("task.json", state)
            return state

    # --- construction -----------------------------------------------------
    @classmethod
    def create(cls, repo, task_id, request, limits, mode="attended"):
        path = os.path.join(project_dir(repo), "tasks", task_id)
        if os.path.exists(path):
            raise StoreError("task %s already exists at %s" % (task_id, path))
        os.makedirs(os.path.join(path, "reports"))
        os.makedirs(os.path.join(path, "verify"))
        task = cls(path)
        task.write_json("task.json", {
            "id": task_id,
            "mode": mode,
            "status": "intake",
            "project_key": project_key(repo),
            "repo": {"path": os.path.abspath(repo), "common_dir": common_dir(repo)},
            "limits": limits,
            "spent": {"usd": 0.0, "attempts": {}, "review_loops": 0, "replans": 0},
            "subtasks": [],
            "delegation_history": [],
            "gates": [],
        })
        task.write_text("task.md", request)
        return task

    @classmethod
    def open(cls, repo, task_id=None):
        env = os.environ.get(TASK_DIR_ENV)
        if env and (task_id is None or os.path.basename(env.rstrip("/")) == task_id):
            return cls(env)
        tasks = os.path.join(project_dir(repo), "tasks")
        if not os.path.isdir(tasks):
            raise StoreError("no tasks for this project (looked in %s)" % tasks)
        if task_id:
            path = os.path.join(tasks, task_id)
            if not os.path.isdir(path):
                raise StoreError("no such task: %s" % path)
            return cls(path)
        found = sorted(d for d in os.listdir(tasks) if not d.startswith("."))
        if len(found) != 1:
            raise StoreError("specify a task id; found %d" % len(found))
        return cls(os.path.join(tasks, found[0]))

    @classmethod
    def list(cls, repo):
        tasks = os.path.join(project_dir(repo), "tasks")
        if not os.path.isdir(tasks):
            return []
        return [cls(os.path.join(tasks, d)) for d in sorted(os.listdir(tasks))
                if os.path.isdir(os.path.join(tasks, d))]

    # --- io ---------------------------------------------------------------
    def file(self, *parts):
        return os.path.join(self.path, *parts)

    def read_text(self, name, default=None):
        try:
            with open(self.file(name), encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            if default is None:
                raise
            return default

    def write_text(self, name, text):
        atomic_write(self.file(name), text)

    def read_json(self, name):
        return json.loads(self.read_text(name))

    def write_json(self, name, data):
        self.write_text(name, json.dumps(data, indent=2) + "\n")

    def append(self, name, line):
        os.makedirs(self.path, exist_ok=True)
        with open(self.file(name), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line.rstrip("\n") + "\n")

    # --- task.json --------------------------------------------------------
    @property
    def state(self):
        return self.read_json("task.json")

    def update(self, **changes):
        return self.mutate(lambda s: s.update(changes))

    def repo_path(self):
        return self.state["repo"]["path"]

    def record_delegation(self, entry):
        entry.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.mutate(lambda s: s["delegation_history"].append(entry))

    def record_gate(self, kind, decision, note=""):
        entry = {"kind": kind, "state": decision, "by": "human", "note": note,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.mutate(lambda s: s["gates"].append(entry))

    def reports(self):
        d = self.file("reports")
        if not os.path.isdir(d):
            return {}
        out = {}
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as fh:
                        out[name] = json.load(fh)
                except (OSError, ValueError):
                    out[name] = None
        return out
