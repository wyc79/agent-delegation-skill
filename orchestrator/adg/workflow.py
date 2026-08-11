"""The workflow, declared instead of hardcoded.

`machine.STAGES` and `prompts.compose` between them decided three things no
deployment could change: which stages exist, which role each dispatches, and
that every role's instructions live at `roles/<role>.md` inside this repo. That
last one is what made this project a *workflow* rather than a runtime that hosts
one -- there was no way to point a stage at somebody else's method.

What is declarative here, and what is deliberately not:

**Declared** -- the workflow's content. Which stages are enabled, which role
each dispatches, where that role's instructions come from, and which discipline
a stage borrows from an installed skill. These are the things that differ
between "the bundled role protocol", "superpowers", and something a user wrote.

**Still code** -- the state machine. `implement` runs worktrees, waves, an
escalation ladder and a rework loop; `classify` is one line of text this program
parses. They are not interchangeable, and a manifest that listed them as if they
were would be a lie that fails on the first non-default workflow. The graph and
its guards stay authored and versioned, which is the property that makes a run
replayable.

So a manifest cannot invent a stage. It can enable one, disable one, point it at
different instructions, and hand its discipline to a foreign skill -- which is
exactly what hosting superpowers requires.
"""

import os

from . import yamlite

MANIFEST = "workflow.yaml"

# Where the bundled default lives, relative to `orchestrator/`. A deployment
# overrides it with `--workflow <dir>` or $AGENT_DELEGATION_WORKFLOW.
DEFAULT_DIR = os.path.join("workflows", "default")

ENV_VAR = "AGENT_DELEGATION_WORKFLOW"


class WorkflowError(ValueError):
    pass


# The workflow in force for this process. Module-level because `prompts.compose`
# is called from a dozen places that have no business threading a manifest
# through, and because a run uses exactly one: swapping it mid-task would mean
# two stages of the same task read different protocols.
_CURRENT = None


def current():
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = Workflow.load()
    return _CURRENT


def use(directory):
    """Point this process at a workflow. `--workflow` and the env var both land
    here, and so do tests, which is the reason it exists as a seam at all."""
    global _CURRENT
    _CURRENT = Workflow.load(directory)
    return _CURRENT


def default_dir():
    orchestrator = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(orchestrator, DEFAULT_DIR)


class Workflow:
    """A loaded manifest. Every accessor answers for a stage or a role, so the
    caller never reaches into the dict and no key name leaks into `machine`."""

    def __init__(self, path, data):
        self.path = path
        self.data = data or {}
        stages = self.data.get("stages")
        if not isinstance(stages, dict) or not stages:
            raise WorkflowError("%s declares no stages" % path)
        self.stages = stages

    # --- loading -----------------------------------------------------------

    @classmethod
    def load(cls, directory=None):
        directory = directory or os.environ.get(ENV_VAR) or default_dir()
        directory = os.path.abspath(os.path.expanduser(directory))
        manifest = os.path.join(directory, MANIFEST)
        if not os.path.exists(manifest):
            raise WorkflowError(
                "no %s in %s. A workflow directory must declare one; the "
                "bundled default is at %s" % (MANIFEST, directory, default_dir()))
        with open(manifest, encoding="utf-8") as fh:
            data = yamlite.load(fh.read())
        return cls(directory, data)

    # --- what an agent reads -----------------------------------------------

    def protocol(self):
        """The file every dispatched agent reads before its role card. Absent
        means this workflow has no shared protocol, which is legitimate: a
        workflow whose stages are foreign skills has nothing of its own to say.
        """
        name = self.data.get("protocol")
        return os.path.join(self.path, name) if name else None

    def card(self, role):
        """Instructions for a role, or None when the workflow supplies none.

        None is not an error. A stage whose discipline comes entirely from a
        foreign skill has no card of its own, and inventing a path to a file
        that does not exist would send the agent hunting for it.
        """
        # First match wins, and two stages CAN share a role -- `brainstorm` and
        # `plan` both dispatch the planner. That made a second declaration
        # silently dead: repointing `plan.card` alone changed nothing, because
        # `brainstorm` is declared first and answers for the role. Rather than
        # pick one quietly, disagreement is refused. A manifest that means two
        # different cards for one role is asking for something this lookup
        # cannot express, and finding that out at load time beats finding out
        # from an agent that read the wrong instructions.
        found = {}
        for stage, spec in self.stages.items():
            if spec.get("role") == role and spec.get("card"):
                found.setdefault(spec["card"], []).append(stage)
        if len(found) > 1:
            raise WorkflowError(
                "%s: role %r is given different cards by different stages (%s). "
                "One role reads one card; split the role or unify the card."
                % (self.path, role,
                   "; ".join("%s -> %s" % (", ".join(v), k) for k, v in found.items())))
        if found:
            return os.path.join(self.path, next(iter(found)))
        # Roles a stage dispatches alongside its own. `test-author` is the one
        # that exists: it runs inside `plan`, before any implementation exists,
        # which is the property that lets it encode what was ASKED rather than
        # what was built. It is not a stage, so it is not in `stages`.
        extra = (self.data.get("extra_roles") or {}).get(role) or {}
        if extra.get("card"):
            return os.path.join(self.path, extra["card"])
        return None

    # --- what the machine asks ---------------------------------------------

    def order(self):
        """Stage ids in declared order. Insertion order is the file's order --
        the manifest is read top to bottom, so the list reads the way it looks.
        """
        return [s for s in self.stages]

    def enabled(self, stage):
        spec = self.stages.get(stage)
        return bool(spec) and spec.get("enabled", True) is not False

    def next_enabled(self, after, terminal="done"):
        """The next stage that will actually run, skipping disabled ones.

        A stage is skipped, never removed: the handlers set their own successor
        (`_stage_classify` can send the run to `brainstorm` or straight to
        `plan`), so the machine still decides where it goes and this only says
        which of those destinations is switched on. Falling off the end is
        `done` rather than an error, because disabling the tail of a workflow is
        a legitimate thing to declare.
        """
        order = self.order()
        if after in order:
            rest = order[order.index(after) + 1:]
        else:
            rest = order
        for stage in rest:
            if self.enabled(stage):
                return stage
        return terminal

    def discipline(self, stage, companions=None):
        """The method text for a stage, chosen by what is installed.

        `skill` names a companion; when it is present the `text` is used, and
        when it is not the `fallback` is. That pair is the whole compatibility
        story in one place: pointing a stage at `superpowers:brainstorming` is a
        manifest edit, not a patch to `machine._stage_brainstorm`, which is
        where this decision used to be hardcoded for exactly one skill.
        """
        spec = (self.stages.get(stage) or {}).get("discipline") or {}
        if not spec:
            return ""
        wanted = spec.get("skill")
        have = bool((companions or {}).get((wanted or "").split(":")[0]))
        if wanted and not have:
            return (spec.get("fallback") or "").strip()
        return (spec.get("text") or spec.get("fallback") or "").strip()

    def wants_skill(self, stage):
        """Which companion a stage would use if it were installed.

        Nothing in the runtime calls this yet: `delegate init` reports which
        companions ARE installed (`companions.detect`) and not which ones this
        workflow would take up if they were. The docstring used to say `init`
        did, which is the kind of claim that reads as a feature.
        """
        return ((self.stages.get(stage) or {}).get("discipline") or {}).get("skill")
