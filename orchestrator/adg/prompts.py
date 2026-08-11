"""Prompt composition (DESIGN.md §3.2).

The orchestrator injects *only* dynamic facts: which role, which task
directory, which scope, which budget. The protocol itself is the skill, which
the agent loads from disk -- so protocol growth costs disk, not per-agent
context, and prompts stay short.

Deliberately absent: any model name, any routing logic, any provider detail.
"""

import os

from . import workflow

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

# Roles whose whole answer is a line of text this program parses. They have no
# role card, write no report, and are handed no task id -- so they are not
# playing a role in this protocol, and `env_for` must not tell them they are.
TEXT_REPLY_ROLES = frozenset({"classifier", "intake", "reporter"})


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
    skill = wf.path
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
        # strictest. Nothing in this program reverts a hunk:
        # `verify.scope_violations` records the files and `_skip_review` turns a
        # simple task's skipped review back on. A prompt that threatens an
        # automatic revert is asking to be found out by the one agent that
        # tests it.
        #
        # `planned_scope` first, and it is the only key a stored subtask has.
        # The planner writes `file_scope` in plan.md and `_read_plan_subtasks`
        # renames it on the way into task.json (machine.py:516), so reading
        # `file_scope` here printed the header above with an EMPTY list under
        # it, on every implementer prompt ever composed. Meanwhile
        # `verify.scope_violations` checked against `planned_scope` and recorded
        # violations of a boundary the agent was never shown. `file_scope`
        # stays as a fallback for a raw plan dict that has not been through the
        # rename.
        scope = subtask.get("planned_scope") or subtask.get("file_scope") or []
        if scope:
            lines += [
                "Write scope (a hard boundary — every file you touch outside it "
                "is recorded and sent to a reviewer):",
            ]
            lines += ["  %s" % g for g in scope]
        else:
            # What `scope_violations` actually does with an empty scope: it
            # defaults to `**`. Saying nothing would read as "unspecified"; this
            # says "unrestricted", which is the truth.
            lines.append("Write scope: not restricted for this subtask — but "
                         "every file you touch is still recorded and reviewed.")
        if subtask.get("reads"):
            lines.append("May read (do not modify): %s" % ", ".join(subtask["reads"]))
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
        # Only the implementer loop counts attempts, and only against a subtask.
        # Telling a reviewer it has "8 attempts on this subtask" names a budget
        # it cannot spend for a subtask it was not given.
        lines += [
            "",
            "Budget for this session: at most %s attempts on this subtask. "
            "Escalate rather than exceeding it." % lims.get("max_attempts_per_subtask", "?"),
        ]
    lines += [
        "",
        "Finish by writing your report to:",
        "  %s/reports/" % task.path,
        "It must validate against %s/schemas/report.schema.json." % skill,
    ]
    found = {k: v for k, v in (state.get("companions") or {}).items() if v}
    if found:
        lines += ["", "Companion skills installed here: %s. See %s/references/"
                      "companions.md for which apply to your role and their limits."
                  % (", ".join(sorted(found)), skill)]
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

    Which is why the text-reply roles do not get it either. `classifier`,
    `intake` and `reporter` are this program's own names for one-shot questions;
    none has a card in `roles/`, none is in the report schema's role enum, and
    none is handed a task id. Setting the mandate for them conscripted an agent
    that then read the protocol, found no row for itself in the Step 2 table and no
    task id in its prompt, and was told by Step 1 to write a `blocked` report
    rather than answer the question -- degrading silently into "classifier gave
    no usable verdict" and a task planned as COMPLEX for no reason. The rule
    stated in orchestrator/winnow-passes.md for foreign passes binds this
    dispatcher's own roles first.
    """
    env = {"AGENT_DELEGATION_TASK_DIR": task.path,
           "AGENT_DELEGATION_SKILL_DIR": skill_path()}
    if role not in TEXT_REPLY_ROLES:
        env["AGENT_DELEGATION_ROLE"] = role
    return env


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
