"""Prompt composition.

The orchestrator injects *only* dynamic facts: which role, which task
directory, which scope, which budget. The protocol itself is the skill, which
the agent loads from disk -- so protocol growth costs disk, not per-agent
context, and prompts stay short.

Deliberately absent: any model name, any routing logic, any provider detail.
"""

import os

from . import schema, workflow

# The protocol dispatched agents follow, relative to `orchestrator/`. It lives
# here rather than at the repo root because it is orchestrator-internal: it is
# the bundled DEFAULT WORKFLOW, one of several the runtime will eventually be
# able to host, not something a user installs.
#
# `agent-delegation/` at the repo root is now a different thing with a different
# audience -- the front-door skill for the agent a human is talking to, which
# teaches only when to call `delegate`. Two audiences were sharing one directory,
# and `compose` below points dispatched agents at `PROTOCOL.md`: leaving them
# merged would have handed every implementer a document telling it to call
# `delegate`, which is the wrong document and an invitation to recurse.
WORKFLOW_DIR = os.path.join("workflows", "default")


def skill_path():
    """Where the workflow in force lives.

    Still called `skill_path`, and still exported to agents as
    `AGENT_DELEGATION_SKILL_DIR`, because that name is the contract: the role
    cards and `references/task-dir.md` both name the variable, and agents on
    other providers read those files. Renaming the path is free; renaming the
    thing agents look for is not.

    It is no longer a fixed directory. `--workflow` and $AGENT_DELEGATION_WORKFLOW
    both move it, which is what lets a deployment host a workflow this repo did
    not write.
    """
    return workflow.current().path


def compose(role, task, subtask=None, extra=None, verify_cfg=None):
    wf = workflow.current()
    lines = ["You are the **%s** in a delegated development workflow." % role.upper(), ""]
    # Both are asked of the manifest rather than assembled from a naming
    # convention. A workflow whose stages are foreign skills has no protocol of
    # its own and no card for the role -- and pointing an agent at a file that
    # does not exist sends it hunting instead of working.
    protocol = wf.protocol()
    if protocol:
        lines += ["Read and follow this protocol before anything else:",
                  "  %s" % protocol]
    card = wf.card(role)
    if card:
        lines += ["Then read your role card:", "  %s" % card]
    lines += [
        "",
        "Task directory (already exists; all task artifacts go here, never in the repo):",
        "  %s" % task.path,
        "It is also in your environment as AGENT_DELEGATION_TASK_DIR, and your "
        "role as AGENT_DELEGATION_ROLE.",
        "",
        "Task id: %s" % task.state["id"],
    ]

    if subtask:
        lines += [
            "",
            "Your subtask: %s" % subtask.get("id"),
            "Goal: %s" % subtask.get("goal", ""),
        ]
        # What the scope line says is what actually happens, not what sounds
        # strictest. Nothing in this program reverts a hunk and nothing reviews
        # the result: `verify.scope_violations` records the files, and the merge
        # brief prints them for the caller. The line used to promise a reviewer
        # that was removed with the rest of the protocol -- a prompt that
        # threatens a consequence this program cannot deliver is asking to be
        # found out by the one agent that tests it.
        #
        # `planned_scope` first, and it is the only key a stored subtask has.
        # The caller writes `file_scope` in plan.md and `_read_plan_subtasks`
        # renames it on the way into task.json, so reading `file_scope` here
        # printed the header above with an EMPTY list under it, on every
        # implementer prompt ever composed. Meanwhile
        # `verify.scope_violations` checked against `planned_scope` and recorded
        # violations of a boundary the agent was never shown. `file_scope`
        # stays as a fallback for a raw plan dict that has not been through the
        # rename.
        scope = subtask.get("planned_scope") or subtask.get("file_scope") or []
        if scope:
            lines += [
                "Write scope (a hard boundary — every file you touch outside it "
                "is recorded and reported back to whoever dispatched you):",
            ]
            lines += ["  %s" % g for g in scope]
        else:
            # What `scope_violations` actually does with an empty scope: it
            # defaults to `**`. Saying nothing would read as "unspecified"; this
            # says "unrestricted", which is the truth.
            lines.append("Write scope: not restricted for this subtask — but "
                         "every file you touch is still recorded and reported.")
        if subtask.get("reads"):
            lines.append("May read (do not modify): %s" % ", ".join(subtask["reads"]))
        # The only thing an agent learns about the jobs running beside it.
        #
        # It was stored in the record and never composed into a prompt, while
        # `roles/worker.md` told the agent "if your prompt names a frozen
        # interface, treat it as a contract" and the front-door skill listed it
        # as carried into the prompt. Three places describing a channel that did
        # not exist -- and it is the channel isolation depends on: agents in
        # separate worktrees cannot see each other's code, so a signature both
        # sides code against has to arrive here or the merge is where the
        # disagreement is discovered.
        if subtask.get("frozen_interfaces"):
            lines.append("Frozen interfaces — other jobs are coding against these "
                         "right now, in worktrees you cannot see. Match them exactly. "
                         "Changing one breaks work you have no way to inspect, so it "
                         "is something to report rather than something to do:")
            lines += ["  %s" % f for f in subtask["frozen_interfaces"]]
        if subtask.get("hotspots"):
            lines.append("Unmergeable files held exclusively for this job (no other "
                         "job runs while you hold them): %s"
                         % ", ".join(subtask["hotspots"]))
        if subtask.get("acceptance"):
            lines.append("Acceptance criteria you own: %s" % ", ".join(subtask["acceptance"]))

    if verify_cfg and verify_cfg.get("fast"):
        lines += ["", "Verification commands for this project (run these, do not invent others):"]
        lines += ["  %s" % c for c in verify_cfg["fast"]]
        if verify_cfg.get("slow"):
            lines.append("Slow checks (stage boundaries only): %s" % "; ".join(verify_cfg["slow"]))

    state = task.state
    lims = state.get("limits", {})
    if subtask:
        # Only against a subtask. The integrator is dispatched without one, and
        # telling it "8 attempts on this subtask" names a budget it cannot spend
        # for a job it was not given.
        lines += [
            "",
            "Budget for this session: at most %s attempts on this subtask. "
            "Escalate rather than exceeding it." % lims.get("max_attempts_per_subtask", "?"),
        ]
    lines += [
        "",
        "Finish by writing your report to:",
        "  %s/reports/" % task.path,
        # The runtime's schemas directory, not the workflow's: the report
        # envelope is how this program reads a result, so it does not move
        # with --workflow and a hosted workflow need not ship a copy.
        "It must validate against %s." % os.path.join(
            schema.schemas_dir(), "report.schema.json"),
    ]
    # Nothing about METHOD is injected here. There was a paragraph naming the
    # companion skills detected on the box and pointing at a
    # `references/companions.md` that had already been deleted with the rest of
    # the protocol, so it sent every dispatched agent hunting for a file that
    # was not there. Picking how to work is the caller's, and an agent that
    # wants a skill it has installed can reach for it without being told.
    if extra:
        lines += ["", extra.strip()]
    return "\n".join(lines)


def env_for(task, role):
    """Location and activation, split.

    `AGENT_DELEGATION_TASK_DIR` used to be both: it said where the artifacts
    are, and the protocol's frontmatter named it as a trigger, so *setting the
    variable* is what enlisted an agent into this protocol. That conflates a
    path with a mandate, and it collides with any other skill hosted on this
    runtime -- dispatch a foreign judgement pass through it and the agent loads
    agent-delegation anyway, then holds two protocols telling it to write
    different files in different places.

    `AGENT_DELEGATION_ROLE` is the mandate, and it is the honest carrier for
    one: an agent outside this protocol has no role in it. A task directory can
    now be handed to a foreign agent as a plain scratch location without
    conscripting it.

    **A dispatcher must not set the mandate for an agent that is not playing one
    of the workflow's roles.** That is the rule that lets this runtime host
    somebody else's pass: give it the scratch path, its own prompt, and no role,
    and it does its own job instead of loading a protocol that tells it to write
    a report it was never asked for.
    """
    return {"AGENT_DELEGATION_TASK_DIR": task.path,
            "AGENT_DELEGATION_SKILL_DIR": skill_path(),
            "AGENT_DELEGATION_ROLE": role}


def retry(failure):
    """Follow-up turn for an agent that is already loaded with the task. It has
    the protocol, the plan and the code in context, so this says only what is
    new -- what broke."""
    return (
        "The checks did not pass. Real output:\n\n%s\n\n"
        "Fix the cause, not the symptom, and stay inside your file scope. "
        "Re-run the checks yourself before finishing. If you now believe the "
        "plan or the test is wrong rather than the code, stop and say so in "
        "your report with the evidence instead of forcing a pass."
        % (failure or "(no output captured)")
    )
