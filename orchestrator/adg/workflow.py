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
        for stage, spec in stages.items():
            # `review:` with nothing under it parses as None, and every
            # accessor below then reads it differently -- `enabled` called it
            # off, `discipline` called it empty. A stage with no body is a
            # manifest mistake, and refusing it here is the same trade
            # `yamlite` makes: a config that silently parses to the wrong shape
            # is worse than one that will not load.
            if not isinstance(spec, dict):
                raise WorkflowError(
                    "%s: stage %r must be a mapping, got %r. A stage with "
                    "nothing to say still needs a body -- `%s: {}`."
                    % (path, stage, spec, stage))
        self.stages = stages
        # Resolved at load, not on first use. `card()` is reached from
        # `prompts.compose`, which runs stages into a task and after real
        # spend; a WorkflowError there arrives at the generic handler as
        # CRASHED, for what is a typo in a file this class has already read.
        # `cli.main` loads the manifest before any subcommand precisely so
        # that a bad one fails with the path in the message.
        self._card_by_role = self._resolve_cards()

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

    def _resolve_cards(self):
        """role -> the one card it reads, for every role this manifest names.

        Two stages CAN share a role -- `brainstorm` and `plan` both dispatch the
        planner -- which made a second declaration silently dead: repointing
        `plan.card` alone changed nothing, because a first-match lookup let
        whichever stage came first answer for the role. Rather than pick one
        quietly, disagreement is refused. A manifest that means two different
        cards for one role is asking for something this lookup cannot express.
        """
        seen = {}
        for stage, spec in self.stages.items():
            if spec.get("role") and spec.get("card"):
                seen.setdefault(spec["role"], {}).setdefault(
                    spec["card"], []).append(stage)
        out = {}
        for role, cards in seen.items():
            if len(cards) > 1:
                raise WorkflowError(
                    "%s: role %r is given different cards by different stages "
                    "(%s). One role reads one card; split the role or unify "
                    "the card."
                    % (self.path, role,
                       "; ".join("%s -> %s" % (", ".join(v), k)
                                 for k, v in cards.items())))
            out[role] = next(iter(cards))
        # Roles a stage dispatches alongside its own. `test-author` is the one
        # that exists: it runs inside `plan`, before any implementation exists,
        # which is the property that lets it encode what was ASKED rather than
        # what was built. It is not a stage, so it is not in `stages` --
        # and a stage's own card wins over one declared here.
        for role, spec in (self.data.get("extra_roles") or {}).items():
            if isinstance(spec, dict) and spec.get("card"):
                out.setdefault(role, spec["card"])
        return out

    def card(self, role):
        """Instructions for a role, or None when the workflow supplies none.

        None is not an error. A stage whose discipline comes entirely from a
        foreign skill has no card of its own, and inventing a path to a file
        that does not exist would send the agent hunting for it.
        """
        name = self._card_by_role.get(role)
        return os.path.join(self.path, name) if name else None

    # --- what the machine asks ---------------------------------------------

    def order(self):
        """Stage ids in declared order. Insertion order is the file's order --
        the manifest is read top to bottom, so the list reads the way it looks.
        """
        return [s for s in self.stages]

    def enabled(self, stage):
        """Is this stage switched on?

        A stage the manifest does not declare is **not** off. The machine owns
        the graph; a manifest switches a stage off by saying so, and saying
        nothing about one leaves it as the machine has it. This answered False
        for an undeclared stage while `machine.run` only ever asked about
        declared ones -- two answers to one question, and the disagreement
        would have surfaced the first time somebody wrote a manifest with no
        `review:` block and got a review anyway.
        """
        spec = self.stages.get(stage)
        if spec is None:
            return True
        return spec.get("enabled", True) is not False

    def next_enabled(self, after, terminal="done", order=None):
        """The next stage that will actually run, skipping disabled ones.

        A stage is skipped, never removed: the handlers set their own successor
        (`_stage_classify` can send the run to `brainstorm` or straight to
        `plan`), so the machine still decides where it goes and this only says
        which of those destinations is switched on. Falling off the end is
        `done` rather than an error, because disabling the tail of a workflow is
        a legitimate thing to declare.

        `order` is the caller's own stage sequence, and `machine.run` passes
        `STAGES`. Walking the manifest's declared stages alone contradicted
        `enabled()`, which treats an undeclared stage as on: a manifest that
        declared `review: {enabled: false}` and never mentioned `integrate`
        skipped review straight to `done`, so the run ended without the stage
        that writes the patch. The machine owns the graph, so the machine's
        order is the one to walk.
        """
        order = list(order or self.order())
        if after not in order:
            # An id this sequence does not contain has no position in it, so it
            # has no "next". Falling back to the whole list returned the FIRST
            # stage, which would send a run back to `intake` and around again.
            return terminal
        rest = order[order.index(after) + 1:]
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

    def criteria(self, stage):
        """A stage's judgement text, when the manifest supplies one.

        Distinct from `discipline`, which is *method* -- how a role should go
        about its work -- and is injected beside that role's card. This is the
        substance of a one-shot question the program asks and parses itself:
        `classify` has no card to carry it, so the criteria used to be a string
        constant in `machine.py` that opened "Classify this software task",
        which is domain policy hardcoded into the machine and exactly what a
        manifest is for.

        What stays code is the frame around it. `_classified` routes on the two
        tiers by name, so the vocabulary and the VERDICT line the parser reads
        are not a manifest's to change -- a workflow that invented a third tier
        would parse as neither and fall through to the safe default. A manifest
        moves the judgement, not the question.
        """
        return ((self.stages.get(stage) or {}).get("criteria") or "").strip()

    def wants_skill(self, stage):
        """Which companion a stage would use if it were installed.

        Nothing in the runtime calls this yet: `delegate init` reports which
        companions ARE installed (`companions.detect`) and not which ones this
        workflow would take up if they were. The docstring used to say `init`
        did, which is the kind of claim that reads as a feature.
        """
        return ((self.stages.get(stage) or {}).get("discipline") or {}).get("skill")
