"""The workflow, declared instead of hardcoded.

`machine.STAGES` and `prompts.compose` between them decided two things no
deployment could change: which stages run, and that the instructions every
dispatched agent reads live inside this repo. The second is what made this a
*workflow* rather than a runtime that hosts one -- there was no way to point an
agent at somebody else's contract.

What is declarative here, and what is deliberately not:

**Declared** -- what a dispatched agent reads. Which stages are enabled, which
role each dispatches, and where that role's instructions come from. A caller
with its own agent-facing protocol points `--workflow` at it and keeps the
placement, isolation and failover underneath.

**Still code** -- the state machine. `implement` runs worktrees, waves and a
bounded retry loop against the caller's checks; `integrate` merges and verifies
at each step. The graph and its guards stay authored and versioned, which is the
property that makes a run replayable.

So a manifest cannot invent a stage. It can enable one, disable one, and point
it at different instructions.
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


# The role each stage dispatches under. `machine` imports these rather than
# spelling the names again, so the manifest is validated against the string the
# machine will actually ask `card()` for -- not against a second copy of it.
#
# A manifest cannot repoint these, and until this existed it could appear to.
# `role:` was read only into the role -> card map, so renaming a stage's role
# left `card(<the machine's name>)` returning None -- which `card` documents as
# legitimate, meaning "this workflow supplies no card". The agent was then
# dispatched with its card silently missing, and nothing anywhere said so. That
# is the failure mode this project names twice in `registry.default.yaml`: a key
# that reads like control and moves nothing.
STAGE_ROLES = {"implement": "implementer", "integrate": "integrator"}


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
            # `integrate:` with nothing under it parses as None, and the
            # accessors below then disagree about what it means -- `enabled`
            # reads it as off, everything else as empty. A stage with no body is
            # a manifest mistake, and refusing it here is the same trade
            # `yamlite` makes: a config that silently parses to the wrong shape
            # is worse than one that will not load.
            if not isinstance(spec, dict):
                raise WorkflowError(
                    "%s: stage %r must be a mapping, got %r. A stage with "
                    "nothing to say still needs a body -- `%s: {}`."
                    % (path, stage, spec, stage))
            # The machine dispatches a fixed role per stage. A manifest may
            # point that role at a different card; it may not rename the role,
            # because nothing downstream would follow the new name.
            want = STAGE_ROLES.get(stage)
            got = spec.get("role")
            if want and got is not None and got != want:
                raise WorkflowError(
                    "%s: stage %r declares role %r, but the machine dispatches "
                    "it as %r and asks for that role's card. Renaming it here "
                    "moves nothing and would leave the agent with no card. Use "
                    "`role: %s` and point `card:` wherever you like."
                    % (path, stage, got, want, want))
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

        Two stages MAY name the same role, and a first-match lookup then made
        the second declaration silently dead: repointing the later stage's
        `card` changed nothing, because whichever stage came first answered for
        the role. Rather than pick one quietly, disagreement is refused. A
        manifest that means two different cards for one role is asking for
        something this lookup cannot express.
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
        # No `extra_roles` key. It let a manifest declare a card for a role that
        # was not a stage's own -- `test-author`, which ran inside `plan` -- and
        # both are gone. It could not be revived by a manifest either: the
        # machine dispatches exactly the roles its two stages name, so a card
        # for anything else would be read by nobody.
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
        declared ones -- two answers to one question, which surfaces the first
        time somebody writes a manifest that omits a stage and gets it anyway.
        """
        spec = self.stages.get(stage)
        if spec is None:
            return True
        return spec.get("enabled", True) is not False

    def next_enabled(self, after, terminal="done", order=None):
        """The next stage that will actually run, skipping disabled ones.

        A stage is skipped, never removed: a handler names its own successor, so
        the machine still decides where a run goes and this only says which of
        those destinations is switched on. Falling off the end is `done` rather
        than an error, because disabling the tail of a workflow is a legitimate
        thing to declare.

        `order` is the caller's own stage sequence, and `machine.run` passes
        `STAGES`. Walking the manifest's declared stages alone contradicted
        `enabled()`, which treats an undeclared stage as on: a manifest that
        disabled one stage and never mentioned `integrate` skipped straight to
        `done`, so the run ended without the stage that writes the patch. The
        machine owns the graph, so the machine's order is the one to walk.
        """
        order = list(order or self.order())
        if after not in order:
            # An id this sequence does not contain has no position in it, so it
            # has no "next". Falling back to the whole list returned the FIRST
            # stage, which sends a run back to the beginning and around again.
            return terminal
        rest = order[order.index(after) + 1:]
        for stage in rest:
            if self.enabled(stage):
                return stage
        return terminal

    # No `discipline()`, `criteria()` or `wants_skill()`. A manifest could
    # declare method text for a stage -- borrowed from an installed companion
    # skill, with a fallback when it was absent -- and the machine never asked
    # for any of it. That is the right outcome rather than a missing call:
    # choosing HOW to work is the caller's, and a manifest key that injects
    # method into an agent's prompt is this program having an opinion about the
    # caller's craft. What a manifest still moves is which protocol and which
    # card an agent reads, which is location, not method.
