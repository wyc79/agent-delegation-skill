"""Runtime adapters (DESIGN.md §4.6).

The orchestrator never touches a runtime directly; it calls this interface.
`local` spawns an agent CLI as a subprocess and always works. `herdr` uses
herdr's worktrees, panes and pre-authenticated sessions when available. `mock`
replays canned artifacts so the whole pipeline can be tested without spending
a token.

Swapping adapters must change who spawns the process and nothing else. If a
workflow decision leaks in here, the boundary has failed.
"""

import json
import os
import shutil
import subprocess

from .store import git


class RuntimeError_(RuntimeError):
    pass


class Session:
    def __init__(self, name, cwd, handle=None):
        self.name, self.cwd, self.handle = name, cwd, handle


class Adapter:
    """Seven operations. Everything else is orchestrator-side."""

    name = "base"

    def create_worktree(self, repo, branch, base, path):
        raise NotImplementedError

    def remove_worktree(self, repo, path):
        raise NotImplementedError

    def start_agent(self, role, kind, cwd, env):
        raise NotImplementedError

    def prompt(self, session, text, timeout):
        raise NotImplementedError

    def status(self, session):
        raise NotImplementedError

    def notify(self, kind, text):
        print("\n[%s] %s" % (kind, text))

    def teardown(self, session):
        pass


class LocalAdapter(Adapter):
    """git + subprocess. No external service, works everywhere including CI
    and Windows, where herdr is preview-only beta."""

    name = "local"

    # Non-interactive invocation per agent CLI. Prompt arrives on argv.
    # Permission mode: agents work inside a throwaway git worktree and hold no
    # git credentials, so the isolation boundary is the worktree, not the
    # prompt (DESIGN.md §11). A mode that blocks running tests would make TDD
    # and self-verification impossible, which is worse than useless.
    LAUNCH = {
        "claude": ["claude", "-p", "--permission-mode", "bypassPermissions"],
        "codex": ["codex", "exec", "--full-auto"],
        "cursor": ["cursor-agent", "-p", "--force"],
        "gemini": ["gemini", "-y", "-p"],
    }

    # Agent CLIs sandbox file access to the working directory. Both the task
    # directory (DESIGN.md §4.0) and the skill directory live outside the repo,
    # so they must be granted explicitly -- otherwise the agent cannot read the
    # protocol it is being told to follow, and cannot write its report.
    GRANTS = {"claude": "--add-dir"}

    def _argv(self, kind, env):
        argv = list(self.LAUNCH[kind])
        flag = self.GRANTS.get(kind)
        dirs = [env.get("AGENT_DELEGATION_TASK_DIR"), env.get("AGENT_DELEGATION_SKILL_DIR")]
        dirs = [d for d in dirs if d]
        if flag and dirs:
            argv += [flag] + dirs
        return argv

    def create_worktree(self, repo, branch, base, path):
        """Idempotent. A leftover branch or checkout from an interrupted run is
        normal -- resuming must reattach to it, not refuse to start."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git")):
            return path
        if os.path.exists(path) and os.listdir(path):
            raise RuntimeError_("worktree path exists and is not a worktree: %s" % path)
        git(["worktree", "prune"], repo, check=False)
        exists = git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                     repo, check=False)
        if exists:
            git(["worktree", "add", path, branch], repo)
        else:
            git(["worktree", "add", "-b", branch, path, base], repo)
        return path

    def remove_worktree(self, repo, path):
        # Best-effort: engines and antivirus hold file locks on Windows, so a
        # failure here is deferred to `worktree prune`, never forced (§4.4b).
        p = subprocess.run(["git", "worktree", "remove", "--force", path],
                           cwd=str(repo), capture_output=True, text=True)
        if p.returncode != 0:
            git(["worktree", "prune"], repo, check=False)
            return False
        return True

    def start_agent(self, role, kind, cwd, env):
        argv = self.LAUNCH.get(kind)
        if argv is None:
            raise RuntimeError_("unknown agent kind %r" % kind)
        if shutil.which(argv[0]) is None:
            raise RuntimeError_(
                "%r not found on PATH -- install it or pick another channel" % argv[0])
        s = Session("%s-local" % role, cwd)
        s.handle = {"argv": self._argv(kind, env), "env": dict(os.environ, **env),
                    "role": role}
        return s

    def prompt(self, session, text, timeout):
        # Prompt goes on stdin, not argv: variadic flags such as claude's
        # --add-dir will otherwise swallow a trailing positional prompt, and
        # stdin has no argument-length limit.
        try:
            p = subprocess.run(session.handle["argv"], cwd=session.cwd,
                               env=session.handle["env"], input=text,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"settled": "timeout", "output": "", "code": None}
        return {"settled": "idle" if p.returncode == 0 else "blocked",
                "output": (p.stdout or "") + (p.stderr or ""), "code": p.returncode}

    def status(self, session):
        return "idle"


class HerdrAdapter(LocalAdapter):
    """Uses herdr for worktrees, panes and authenticated sessions. Inherits
    LocalAdapter so any unavailable piece degrades instead of failing."""

    name = "herdr"

    def __init__(self, workspace=None):
        self.workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")

    @staticmethod
    def available():
        return os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None

    def _cli(self, args, check=True):
        p = subprocess.run(["herdr"] + args, capture_output=True, text=True)
        if p.returncode != 0:
            if check:
                raise RuntimeError_("herdr %s: %s" % (" ".join(args), p.stderr.strip()))
            return None
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"raw": p.stdout}

    def create_worktree(self, repo, branch, base, path):
        res = self._cli(["worktree", "create", "--cwd", str(repo), "--branch", branch,
                         "--base", base, "--path", path, "--no-focus"], check=False)
        if res is None:
            return LocalAdapter.create_worktree(self, repo, branch, base, path)
        return path

    def remove_worktree(self, repo, path):
        if self._cli(["worktree", "remove", "--path", path], check=False) is None:
            return LocalAdapter.remove_worktree(self, repo, path)
        return True

    def notify(self, kind, text):
        first = text.strip().splitlines()[0] if text.strip() else kind
        self._cli(["notification", "show", "--message", "%s: %s" % (kind, first[:160])],
                  check=False)
        print("\n[%s] %s" % (kind, text))


class MockAdapter(Adapter):
    """Replays scripted agent behaviour. Lets the full state machine be tested
    end to end deterministically -- the pipeline is the thing under test, not
    the model."""

    name = "mock"

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def create_worktree(self, repo, branch, base, path):
        return LocalAdapter.create_worktree(self, repo, branch, base, path)

    def remove_worktree(self, repo, path):
        return LocalAdapter.remove_worktree(self, repo, path)

    def start_agent(self, role, kind, cwd, env):
        return Session("%s-mock" % role, cwd, handle={"role": role, "env": env})

    def prompt(self, session, text, timeout):
        role = session.handle["role"]
        self.calls.append((role, text))
        fn = self.script.get(role)
        if fn is None:
            return {"settled": "idle", "output": "", "code": 0}
        fn(session.handle["env"], session.cwd)
        return {"settled": "idle", "output": "mock:%s" % role, "code": 0}

    def status(self, session):
        return "idle"

    def notify(self, kind, text):
        self.calls.append(("notify", kind))


def get(name, **kw):
    if name == "herdr":
        if not HerdrAdapter.available():
            return LocalAdapter()
        return HerdrAdapter(**kw)
    if name == "local":
        return LocalAdapter()
    if name == "mock":
        return MockAdapter(**kw)
    raise RuntimeError_("unknown adapter %r" % name)
