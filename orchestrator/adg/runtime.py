"""Runtime adapters.

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
import re
import shutil
import subprocess
import time

from . import quota
from .store import git


class RuntimeError_(RuntimeError):
    pass


def _result(stdout, stderr, code, kind=None, now=None, error_code=None):
    """Normalise a CLI run. Structured output carries the cost; plain text does
    not, and a missing cost is reported as unknown rather than as zero.

    Classification happens here, where the *raw* streams are still in scope: a
    JSON `--output-format` run replaces `output` with the parsed result field,
    and the rate-limit message usually arrived on stderr."""
    raw = (stdout or "") + (stderr or "")
    text, cost = raw, None
    try:
        data = json.loads(stdout or "")
    except ValueError:
        data = None
    usage, elapsed_ms = None, None
    if isinstance(data, dict):
        text = data.get("result") or data.get("output") or raw
        for key in ("total_cost_usd", "cost_usd"):
            if isinstance(data.get(key), (int, float)):
                cost = float(data[key])
                break
        usage = _tokens(data.get("usage"))
        # The CLI times its own call. Measured here because it is the only
        # honest source of AGENT time: differencing our own record stamps also
        # counts however long a human sat on an approval gate, and the stamps
        # are one-second resolution, so two quick steps look simultaneous.
        if isinstance(data.get("duration_ms"), (int, float)):
            elapsed_ms = int(data["duration_ms"])
        if isinstance(data.get("error"), dict):
            error_code = error_code or data["error"].get("code")
    res = {"settled": "idle" if code == 0 else "blocked",
           "output": text, "code": code, "cost_usd": cost, "usage": usage,
           "elapsed_ms": elapsed_ms, "error_code": error_code}
    return classify(res, kind, now, raw)


def classify(res, kind, now=None, text=None):
    """Attach `failure` and `reset_at` to an adapter result. One seam, so every
    adapter answers the quota question the same way."""
    probe = dict(res, output=text if text is not None else res.get("output"))
    failure, reset_at = quota.classify(
        kind, probe, now if now is not None else time.time())
    res["failure"], res["reset_at"] = failure, reset_at
    return res


def _tokens(usage):
    """Normalise the two token shapes we see. Cost can then be derived from our
    own price table when a CLI reports usage but not money."""
    if not isinstance(usage, dict):
        return None
    def pick(*names):
        for n in names:
            if isinstance(usage.get(n), (int, float)):
                return int(usage[n])
        return 0
    got = {"in": pick("input_tokens", "inputTokens")
                 + pick("cache_read_input_tokens", "cacheReadTokens")
                 + pick("cache_creation_input_tokens", "cacheWriteTokens"),
           "out": pick("output_tokens", "outputTokens")}
    return got if (got["in"] or got["out"]) else None


class Session:
    def __init__(self, name, cwd, handle=None):
        self.name, self.cwd, self.handle = name, cwd, handle


class Adapter:
    """Nine operations. Everything else is orchestrator-side."""

    name = "base"

    def create_worktree(self, repo, branch, base, path):
        raise NotImplementedError

    def remove_worktree(self, repo, path):
        raise NotImplementedError

    def can_run(self, kind):
        """Is this agent kind actually usable here? The registry lists what a
        deployment *could* use; only the runtime knows what is installed."""
        return True

    def start_agent(self, role, kind, cwd, env):
        raise NotImplementedError

    def prompt(self, session, text, timeout):
        raise NotImplementedError

    def follow_up(self, session, text, timeout):
        """Continue an existing session, keeping its context. Retrying a fix in
        a fresh session throws away everything the agent just learned and makes
        it re-read the protocol, the task and the repo -- most of the wall clock
        in a short task. Adapters that cannot continue fall back to a new turn."""
        return self.prompt(session, text, timeout)

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
    # prompt. A mode that blocks running tests would make TDD
    # and self-verification impossible, which is worse than useless.
    # --output-format json so the run reports what it cost. Without it
    # max_cost_usd is a limit that can never bind, which is worse than no limit
    # at all: it reads as protection.
    LAUNCH = {
        "claude": ["claude", "-p", "--permission-mode", "bypassPermissions",
                   "--output-format", "json"],
        "codex": ["codex", "exec", "--full-auto"],
        "cursor": ["cursor-agent", "-p", "--force", "--output-format", "json"],
        "gemini": ["gemini", "-y", "-p"],
    }

    # Some agent CLIs confine file access to the working directory. Both the
    # task directory and the skill directory live outside the
    # repo, so where that confinement exists it must be lifted explicitly --
    # otherwise the agent cannot read the protocol it is being told to follow,
    # and cannot write its report.
    #
    # Claude-only because it is the only kind measured to need it, not because
    # the others were assumed safe. Under the `--force` this file already passes,
    # cursor-agent reads and writes outside its cwd freely -- checked both
    # directions against a real seat -- so an --add-dir for it would be a no-op
    # today, and if that permissiveness ever narrowed it would take the shell
    # and write access with it, which no grant flag restores. codex and gemini
    # are UNVERIFIED: neither was installed here. Measure before adding one.
    GRANTS = {"claude": "--add-dir"}

    # Grant flags whose one occurrence takes every path. The rest are repeated
    # per path, and the difference is not cosmetic: claude declares
    # `--add-dir <directories...>`, cursor declares `--add-dir <path>`. Handing
    # cursor two paths after one flag does not error -- the second is silently
    # absorbed as a positional prompt argument, because its usage line ends in
    # `[prompt...]`. That is the same trap `prompt()` documents from the other
    # side, and it is why this table exists rather than a bare string.
    GRANT_VARIADIC = frozenset({"claude"})

    # Flag that resumes the CLI's own previous conversation in this directory.
    # Both are directory-scoped, which is what makes them safe here: every
    # subtask runs in its own worktree, so one agent cannot resume another's.
    CONTINUE = {"claude": "--continue", "cursor": "--continue"}

    def _argv(self, kind, env):
        argv = list(self.LAUNCH[kind])
        flag = self.GRANTS.get(kind)
        dirs = [env.get("AGENT_DELEGATION_TASK_DIR"), env.get("AGENT_DELEGATION_SKILL_DIR")]
        dirs = [d for d in dirs if d]
        if flag and dirs:
            if kind in self.GRANT_VARIADIC:
                argv += [flag] + dirs
            else:
                for d in dirs:
                    argv += [flag, d]
        return argv

    def can_run(self, kind):
        argv = self.LAUNCH.get(kind)
        return bool(argv) and shutil.which(argv[0]) is not None

    def create_worktree(self, repo, branch, base, path):
        """Idempotent. A leftover branch or checkout from an interrupted run is
        normal -- resuming must reattach to it, not refuse to start."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git")):
            # Reuse only if it really is this repository's worktree. Handing an
            # agent someone else's checkout is silent and catastrophic.
            here = git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                       path, check=False)
            mine = git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                       repo, check=False)
            if here and mine and os.path.realpath(here) == os.path.realpath(mine):
                return path
            raise RuntimeError_(
                "%s is a worktree of a different repository (%s, not %s)"
                % (path, here or "unknown", mine))
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
        # failure here is deferred to `worktree prune`, never forced (b).
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
        # Through can_run, not shutil.which directly: one seam decides whether a
        # CLI is usable, so routing tests can stub it without the vendor binary
        # installed -- and the answer here cannot drift from the one _pick used.
        if not self.can_run(kind):
            raise RuntimeError_(
                "%r not found on PATH -- install it or pick another channel" % argv[0])
        s = Session("%s-local" % role, cwd)
        s.handle = {"argv": self._argv(kind, env), "env": dict(os.environ, **env),
                    "role": role, "kind": kind}
        return s

    def prompt(self, session, text, timeout, argv=None):
        # Prompt goes on stdin, not argv: variadic flags such as claude's
        # --add-dir will otherwise swallow a trailing positional prompt, and
        # stdin has no argument-length limit.
        kind = (session.handle or {}).get("kind")
        try:
            p = subprocess.run(argv or session.handle["argv"], cwd=session.cwd,
                               env=session.handle["env"], input=text,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Still never quota -- a timeout says nothing about the provider's
            # window, and a wrong five-hour breaker hides a healthy seat -- but
            # `timeout`, not `other`, and the distinction has to be made here as
            # well as in `quota.classify`. This is the path a real hung agent
            # takes, so leaving it as `other` would mean the hop-on-timeout
            # never fires in practice while the classifier's tests all passed.
            # Two places decide what a timeout means; they must not disagree.
            return {"settled": "timeout", "output": "", "code": None,
                    "failure": "timeout", "reset_at": None}
        return _result(p.stdout, p.stderr, p.returncode, kind=kind)

    def follow_up(self, session, text, timeout):
        flag = self.CONTINUE.get(session.handle.get("kind"))
        if not flag:
            return self.prompt(session, text, timeout)
        return self.prompt(session, text, timeout,
                           argv=session.handle["argv"] + [flag])

    def status(self, session):
        return "idle"


class HerdrAdapter(LocalAdapter):
    """Uses herdr for worktrees, panes and authenticated sessions. Inherits
    LocalAdapter so any unavailable piece degrades instead of failing."""

    name = "herdr"

    def __init__(self, workspace=None, panes=True):
        self.workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")
        self.last_error = None
        # Pane sessions are visible but report no cost: total_cost_usd only
        # comes back from `claude -p --output-format json`. Turning panes off
        # trades that visibility back for an enforceable spend cap.
        self.panes = panes

    @staticmethod
    def available():
        return os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None

    def _cli(self, args, check=True):
        p = subprocess.run(["herdr"] + args, capture_output=True, text=True)
        payload = None
        try:
            payload = json.loads(p.stdout)
        except ValueError:
            payload = {"raw": p.stdout} if p.stdout.strip() else None
        # herdr reports failures as JSON on stderr with exit 1. Returning a bare
        # None for both "it failed" and "it said no" made every failure look
        # like a timeout, which is the least actionable thing it could be.
        if p.returncode != 0 or (isinstance(payload, dict) and "error" in payload):
            err = ""
            if isinstance(payload, dict):
                err = (payload.get("error") or {}).get("code", "")
            if not err:
                try:
                    err = (json.loads(p.stderr).get("error") or {}).get("code", "")
                except ValueError:
                    err = (p.stderr or "").strip()[:120]
            self.last_error = err or "exit %s" % p.returncode
            if check:
                raise RuntimeError_("herdr %s: %s" % (" ".join(args[:3]), self.last_error))
            return None
        self.last_error = None
        return payload

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

    # --- agent sessions in visible panes -----------------------------------
    # Without these, `--adapter herdr` still ran every agent as a hidden
    # subprocess: worktrees and notifications went through herdr and nothing
    # ever appeared in the UI. Each of these degrades to LocalAdapter, so a
    # herdr that refuses still runs the task.

    @staticmethod
    def _agent_name(role, task_dir):
        # herdr requires [a-z][a-z0-9_-]{0,31}, unique among live agents.
        tail = re.sub(r"[^a-z0-9]+", "", os.path.basename(task_dir.rstrip("/")).lower())[-6:]
        return re.sub(r"[^a-z0-9-]+", "-", "%s-%s" % (role.lower(), tail or "task"))[:32]

    # Every role runs in a pane, because every role this program dispatches
    # communicates through report files on disk. There used to be an exception
    # list -- `classifier`, `intake`, `reporter`, one-shot questions whose whole
    # answer was a line of text this program parsed back -- and a pane renders
    # on the terminal's alternate screen, so reading one back yields TUI chrome
    # rather than the reply. Those roles went with the protocol.
    #
    # The constraint outlives them: a role whose answer must be READ BACK cannot
    # run in a pane. Anything added here that is not report-file-shaped needs
    # `LocalAdapter.start_agent` directly.
    def start_agent(self, role, kind, cwd, env):
        if not self.panes:
            return LocalAdapter.start_agent(self, role, kind, cwd, env)
        pane = self._cli(["pane", "split", "--current", "--direction", "right",
                          "--cwd", cwd, "--no-focus"], check=False)
        pane_id = (((pane or {}).get("result") or {}).get("pane") or {}).get("pane_id")
        if not pane_id:
            return LocalAdapter.start_agent(self, role, kind, cwd, env)
        name = self._agent_name(role, env.get("AGENT_DELEGATION_TASK_DIR", role))
        args = ["agent", "start", name, "--kind", kind, "--pane", pane_id]
        grants = [d for d in (env.get("AGENT_DELEGATION_TASK_DIR"),
                              env.get("AGENT_DELEGATION_SKILL_DIR")) if d]
        if grants and self.GRANTS.get(kind):
            args += ["--"] + [self.GRANTS[kind]] + grants
        # A freshly split pane is not an available shell until its prompt is up;
        # herdr rejects `agent start` until then (agent_pane_busy).
        started = None
        for _ in range(10):
            started = self._cli(args, check=False)
            if started is not None:
                break
            time.sleep(2)
        if started is None:
            self._cli(["pane", "close", pane_id], check=False)
            return LocalAdapter.start_agent(self, role, kind, cwd, env)
        s = Session(name, cwd, handle={"role": role, "kind": kind, "pane": pane_id,
                                       "herdr": True, "env": env})
        return s

    def prompt(self, session, text, timeout, argv=None):
        if not (session.handle or {}).get("herdr"):
            return LocalAdapter.prompt(self, session, text, timeout, argv)
        kind = (session.handle or {}).get("kind")
        res = self._cli(["agent", "prompt", session.name, text, "--wait",
                         "--timeout", str(int(timeout * 1000))], check=False)
        if res is None:
            why = getattr(self, "last_error", "") or "unknown"
            return classify({"settled": "timeout" if "timeout" in why else "blocked",
                             "output": "herdr refused the prompt: %s" % why,
                             "code": None, "cost_usd": None,
                             "error_code": why}, kind)
        read = self._cli(["agent", "read", session.name, "--source",
                          "recent-unwrapped", "--lines", "400"], check=False) or {}
        out = ((read.get("result") or {}).get("text")
               or read.get("raw") or json.dumps(read))[-20000:]
        agent = (res.get("result") or {}).get("agent") or {}
        state = agent.get("agent_status") or agent.get("state") or "idle"
        # herdr reports liveness, never success -- `blocked` means it is waiting
        # on a human, which for an unattended run is a failure to report.
        return classify({"settled": "blocked" if state == "blocked" else "idle",
                         "output": out, "code": 0 if state != "blocked" else 1,
                         "cost_usd": None,
                         "error_code": agent.get("error_code")}, kind)

    def follow_up(self, session, text, timeout):
        if not (session.handle or {}).get("herdr"):
            return LocalAdapter.follow_up(self, session, text, timeout)
        return self.prompt(session, text, timeout)   # the pane keeps its context

    def status(self, session):
        if not (session.handle or {}).get("herdr"):
            return "idle"
        got = self._cli(["agent", "get", session.name], check=False) or {}
        agent = (got.get("result") or {}).get("agent") or {}
        return agent.get("agent_status") or agent.get("state") or "unknown"

    def teardown(self, session):
        if (session.handle or {}).get("herdr") and session.handle.get("pane"):
            self._cli(["pane", "close", session.handle["pane"]], check=False)


class MockAdapter(Adapter):
    """Replays scripted agent behaviour. Lets the full state machine be tested
    end to end deterministically -- the pipeline is the thing under test, not
    the model."""

    name = "mock"

    def __init__(self, script=None, now=None):
        self.script = script or {}
        self.calls = []
        # Injected so failover tests classify against a fixed clock and the
        # suite never sleeps.
        self.now = now or time.time

    def create_worktree(self, repo, branch, base, path):
        return LocalAdapter.create_worktree(self, repo, branch, base, path)

    def remove_worktree(self, repo, path):
        return LocalAdapter.remove_worktree(self, repo, path)

    def can_run(self, kind):
        return True

    def start_agent(self, role, kind, cwd, env):
        return Session("%s-mock" % role, cwd,
                       handle={"role": role, "kind": kind, "env": env})

    def prompt(self, session, text, timeout):
        role = session.handle["role"]
        self.calls.append((role, text))
        fn = self.script.get(role)
        if fn is None:
            return classify({"settled": "idle", "output": "", "code": 0},
                            session.handle.get("kind"), self.now())
        # A script may return a result dict to stand in for a real CLI failure
        # -- run it through the same classifier the local adapter uses, so a
        # test proves the shipped table and not a stub.
        res = {"settled": "idle", "output": "mock:%s" % role, "code": 0}
        override = fn(session.handle["env"], session.cwd)
        if isinstance(override, dict):
            res = dict(res, **override)
        return classify(res, session.handle.get("kind"), self.now())

    def follow_up(self, session, text, timeout):
        self.calls.append(("follow_up", session.handle["role"]))
        return self.prompt(session, text, timeout)

    def status(self, session):
        return "idle"

    def notify(self, kind, text):
        self.calls.append(("notify", kind))


def get(name, **kw):
    if name == "herdr":
        if not HerdrAdapter.available():
            return LocalAdapter()
        return HerdrAdapter(**kw)
    if name == "herdr-quiet":                     # herdr worktrees, no panes
        return HerdrAdapter(panes=False) if HerdrAdapter.available() else LocalAdapter()
    if name == "local":
        return LocalAdapter()
    if name == "mock":
        return MockAdapter(**kw)
    raise RuntimeError_("unknown adapter %r" % name)
