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

from . import dsh, quota
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
    # What the QUOTA CLASSIFIER is allowed to read, which is not the same as
    # what the log keeps. Everything except the agent's own `result` prose.
    #
    # An agent whose job is a retry handler writes "429" and "rate_limit" in its
    # summary; one reviewing a limiter quotes "quota exceeded"; one grepping for
    # RESOURCE_EXHAUSTED prints the matches. Classifying the whole transcript
    # read those as the PROVIDER refusing, and a `quota_exhausted` verdict does
    # not merely lose an attempt -- it opens a five-hour breaker on a healthy
    # seat, machine-wide, across every repository. Five of six such transcripts
    # were misread before this split.
    #
    # The envelope's other fields stay in scope on purpose: `error`, `subtype`,
    # `is_error` and stderr are where a CLI reports a refusal, and a message
    # that arrives there is the provider talking. Only `result`/`output` are
    # definitionally the agent's own words.
    #
    # Plain-text CLIs (no parseable envelope) keep the whole stream, because
    # there is nothing to separate the two with. `fixtures/provider-messages.json`
    # carries that remaining exposure as cases marked `wrong`.
    probe = raw
    if isinstance(data, dict):
        rest = {k: v for k, v in data.items() if k not in ("result", "output")}
        probe = json.dumps(rest) + "\n" + (stderr or "")
    usage, elapsed_ms, turns = None, None, None
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
        # Turns, when the CLI counts them. Cost on a cached run is driven by
        # how many turns an agent takes, not by how long its prompt is: every
        # turn re-sends the accumulated context, so one extra tool call is
        # worth far more than one extra paragraph. Without this the record can
        # say an agent cost 2x another and not say why, which is exactly the
        # question a run comparing itself to a hand-rolled control has to
        # answer.
        if isinstance(data.get("num_turns"), (int, float)):
            turns = int(data["num_turns"])
        if isinstance(data.get("error"), dict):
            error_code = error_code or data["error"].get("code")
    res = {"settled": "idle" if code == 0 else "blocked",
           "output": text, "code": code, "cost_usd": cost, "usage": usage,
           "elapsed_ms": elapsed_ms, "turns": turns, "error_code": error_code}
    return classify(res, kind, now, probe)


def settle_unclassified(res):
    """Settle a failure WITHOUT asking the pattern table anything.

    The adapter decides what text classification is allowed to read, and an
    adapter is entitled to answer "nothing". This is that answer: the result
    gets a `failure` derived only from how the call settled, never from prose.

    Which means no `quota_exhausted` can come out of here, so no breaker, no
    cooldown and no reset parse -- the run sees a plain failed attempt. That is
    the cheap, loud outcome. The expensive, silent one is a five-hour breaker on
    a healthy seat, machine-wide, across every repository, opened because an
    agent working on rate-limiting code said "429" in its own transcript.

    `timeout` survives, because a timeout is not a reading of text: it is how
    the call ended. It hops to another seat and deliberately does not cool one,
    which is the same thing it means everywhere else. `quota.failed` decides
    "did this fail at all" here as it does there, so the two cannot drift.
    """
    if not quota.failed(res):
        res["failure"], res["reset_at"] = None, None
    elif res.get("settled") == "timeout":
        res["failure"], res["reset_at"] = "timeout", None
    else:
        res["failure"], res["reset_at"] = "other", None
    return res


def classify(res, kind, now=None, text=None):
    """Attach `failure` and `reset_at` to an adapter result. One seam, so every
    adapter answers the quota question the same way."""
    probe = dict(res, output=text if text is not None else res.get("output"))
    failure, reset_at = quota.classify(
        kind, probe, now if now is not None else time.time())
    res["failure"], res["reset_at"] = failure, reset_at
    # Carried on any failure, and read only by the corpus capture and the run
    # log. Nothing routes on it. It exists because the text the PATTERN TABLE
    # saw is not the text the log keeps -- the local path holds the agent's own
    # prose back from classification -- so a record built from `output` would
    # be a record of a different question from the one detection was asked.
    #
    # Every failure, not only a wall: the run log has to be able to show why a
    # message was read as `other`, and "here is what it read" is most of that
    # answer. An adapter that classifies nothing sets nothing, and that absence
    # is itself the record -- see `settle_unclassified`.
    if failure:
        res["classified_text"] = probe.get("output")
    return res


def _tokens(usage):
    """Normalise the two token shapes we see, keeping the three input classes
    apart. Cost can then be derived from our own price table when a CLI reports
    usage but not money.

    `in` stays the total, because it is what the record and the briefs have
    always shown and it is the honest answer to "how much did this agent read".
    The split rides alongside it, because the three classes are not the same
    price: a cache read is a tenth of fresh input and a cache write is a
    quarter more. Folding them together and pricing the sum at the fresh rate
    over-charged every agent whose context was mostly cache -- which is every
    agent on a long turn, and is exactly the shape of a coding run.
    """
    if not isinstance(usage, dict):
        return None
    def pick(*names):
        for n in names:
            if isinstance(usage.get(n), (int, float)):
                return int(usage[n])
        return 0
    fresh = pick("input_tokens", "inputTokens")
    cache_read = pick("cache_read_input_tokens", "cacheReadTokens")
    cache_write = pick("cache_creation_input_tokens", "cacheWriteTokens")
    got = {"in": fresh + cache_read + cache_write,
           "out": pick("output_tokens", "outputTokens"),
           "fresh_in": fresh, "cache_read": cache_read,
           "cache_write": cache_write}
    return got if (got["in"] or got["out"]) else None


class Session:
    def __init__(self, name, cwd, handle=None):
        self.name, self.cwd, self.handle = name, cwd, handle
        # Every prompt the orchestrator has sent this session, and the foreign
        # input lines it has already counted against it. Both are maintained by
        # `machine`, not by any adapter: they are what "did somebody else type
        # into this pane?" is answered by subtraction from. The second exists
        # because a pane transcript is a rolling window re-read on every turn,
        # so without it the same human line is counted again on every read.
        self.sent, self.counted = [], set()


# Where each agent CLI looks for a repository's standing conventions. One entry
# per `agent_kind` the LAUNCH tables below can start; first match wins.
AGENT_CONFIG = {
    "claude": ["CLAUDE.md", ".claude/CLAUDE.md"],
    "cursor": [".cursor/rules", ".cursorrules"],
    "codex": ["AGENTS.md"],
    "gemini": ["GEMINI.md"],
    # dsh ships `@deepseek-ai/dsh-skill` and a subdir-AGENTS.md injector, and
    # AGENTS.md is the convention that package names. Listed so `delegate init`
    # audits a dsh seat like any other rather than reporting it as having no
    # conventions at all -- which would read as "this seat is fine" when the
    # real answer is "nobody asked".
    "dsh": ["AGENTS.md"],
}


def conventions(repo, kind):
    """Does a job dispatched on `kind` actually see this repo's conventions?

    Returns (path_or_None, reaches_the_worktree).

    **Existence in the repo is not the question, and answering it that way gets
    the dangerous case backwards.** Every job runs in a `git worktree`, which
    checks out TRACKED FILES ONLY -- so an untracked or ignored `CLAUDE.md`
    sits in the main checkout, is read by the caller's own session, and is
    absent from every worktree this program dispatches into. An audit run from
    the main repo sees the file and reports it present; the agents never get it.
    Keeping CLAUDE.md out of git is a common habit, so this is the likely case
    rather than the exotic one.
    """
    for rel in AGENT_CONFIG.get(kind, []):
        if not os.path.exists(os.path.join(repo, rel)):
            continue
        p = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                           cwd=repo, capture_output=True, text=True)
        return rel, p.returncode == 0
    return None, False


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
        # DeepSeek Harness, pinned to 0.1.0-rc.6. `--profile headless` answers
        # one task in the invoking directory and exits; it has NO structured
        # output flag (`--help` lists only `-h`), so stdout is the agent's final
        # message as prose and nothing else. Everything structured comes from
        # the persisted session log afterwards -- see
        # orchestrator/docs/dsh-adapter-notes.md, which records what was
        # verified against the installed package and what was not.
        "dsh": ["dsh", "--profile", "headless"],
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
        if kind == "dsh":
            return self._prompt_dsh(session, text, timeout, argv)
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

    def _prompt_dsh(self, session, text, timeout, argv=None, now=None):
        """One headless dsh turn, then whatever its session log will admit to.

        Two things make this its own path. The task goes on **argv**, not
        stdin: `dsh --profile headless` declares `[task...]` positionally and
        reads no prompt from stdin, so the shared path would hand it an empty
        task and wait. And stdout is the final assistant message as prose --
        there is no structured-output flag on headless, checked against
        `--help` -- so the classifier is shown stderr and the session log's
        structured failure, and never the printed answer. An agent whose job
        involves rate limits must not be able to cool its own seat.
        """
        now = time.time() if now is None else now
        started = now
        base = list(argv or session.handle["argv"])
        try:
            p = subprocess.run(base + [text], cwd=session.cwd,
                               env=session.handle["env"],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"settled": "timeout", "output": "", "code": None,
                    "failure": "timeout", "reset_at": None}
        res = {"settled": "idle" if p.returncode == 0 else "blocked",
               "output": p.stdout or "", "stderr": p.stderr or "",
               "code": p.returncode, "cost_usd": None, "usage": None,
               "elapsed_ms": None, "turns": None, "error_code": None}
        # Enrichment first, so the structured failure is in scope when the
        # table is asked. Best-effort by construction: `enrich` cannot raise,
        # and everything below still works from the exit code alone.
        dsh.enrich(res, session.cwd, env=session.handle.get("env"),
                   since=started - 1)
        classify(res, "dsh", now, text=dsh.probe_text(res))
        stated = dsh.stated_reset(res, now)
        if stated and res.get("failure") == "quota_exhausted":
            # A provider-stated delay beats every regex in the prose table, so
            # it wins where it exists rather than being a fallback for it.
            res["reset_at"] = stated
        return res

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
    LocalAdapter so any unavailable piece degrades instead of failing.

    **In pane mode, quota walls are not detected at all, and that is
    deliberate.** herdr exposes no channel that carries the provider's own
    words. Checked against the shipped API schema (`herdr api schema`, protocol
    schema_version 1) and against a live `herdr agent list`:

      * `AgentInfo` -- what `agent prompt` and `agent get` return -- has no
        error field of any kind. Its whole outcome vocabulary is
        `agent_status: idle | working | blocked | done | unknown`, which is
        liveness and says nothing about why. (This class used to read
        `agent.get("error_code")`; herdr has never emitted one, so that read
        was a permanent None dressed as a structured channel.)
      * `ErrorResponse{code, message}` is herdr refusing the REQUEST --
        `agent_pane_busy`, a stalled prompt, a timeout. That is herdr talking
        about herdr, not a provider talking about quota.
      * All four `agent read --source` values (visible, recent,
        recent-unwrapped, detection) are renderings of the pane's scrollback.
        A pane is a PTY, so a CLI's stderr is already merged into the screen
        by construction: there is no stderr-like channel to separate out.

    That leaves only the transcript, which is prose the AGENT wrote, and
    classifying an agent's own words as the provider refusing is the failure
    `runtime._result` documents at length for the local path. So the pane path
    settles unclassified (`settle_unclassified`): a wall shows up as a failed
    job, and resuming after the window reopens is the caller's move.

    `--no-panes` is unaffected. It runs `LocalAdapter.prompt`, a real
    subprocess with a real stderr and a real JSON envelope, and keeps the full
    envelope-minus-prose probe.
    """

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
            # herdr's own error code, kept on the record and deliberately NOT
            # named `error_code`: that key is the one `quota.classify` reads,
            # and this is herdr saying it would not take the request, not a
            # provider saying it is out. Handing it to the table would let
            # herdr's vocabulary open a breaker on a seat herdr never called.
            return settle_unclassified({
                "settled": "timeout" if "timeout" in why else "blocked",
                "output": "herdr refused the prompt: %s" % why,
                "code": None, "cost_usd": None, "herdr_error": why})
        read = self._cli(["agent", "read", session.name, "--source",
                          "recent-unwrapped", "--lines", "400"], check=False) or {}
        out = ((read.get("result") or {}).get("text")
               or read.get("raw") or json.dumps(read))[-20000:]
        agent = (res.get("result") or {}).get("agent") or {}
        state = agent.get("agent_status") or agent.get("state") or "idle"
        # herdr reports liveness, never success -- `blocked` means it is waiting
        # on a human, which for an unattended run is a failure to report.
        #
        # `pane_transcript` says where `output` came from, and only this branch
        # can say it: this is a real terminal a human can type into, and what
        # comes back is the pane's scrollback rather than the agent's answer.
        # `machine` reads it to decide whether counting input lines means
        # anything. A subprocess run has no keyboard on it, and its output is
        # the agent's own prose, where a line beginning `>` is a blockquote and
        # not somebody talking.
        #
        # And it is the same fact that keeps `out` away from the pattern table.
        # Scrollback is what the AGENT wrote; herdr offers nothing else (see the
        # class docstring), so this settles unclassified rather than guessing.
        return settle_unclassified({
            "settled": "blocked" if state == "blocked" else "idle",
            "output": out, "code": 0 if state != "blocked" else 1,
            "cost_usd": None, "pane_transcript": True})

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
