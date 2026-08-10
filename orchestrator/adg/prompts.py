"""Prompt composition (DESIGN.md §3.2).

The orchestrator injects *only* dynamic facts: which role, which task
directory, which scope, which budget. The protocol itself is the skill, which
the agent loads from disk -- so protocol growth costs disk, not per-agent
context, and prompts stay short.

Deliberately absent: any model name, any routing logic, any provider detail.
"""

import os

SKILL_DIRNAME = "agent-delegation"


def skill_path():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, SKILL_DIRNAME)


def compose(role, task, subtask=None, extra=None, verify_cfg=None):
    skill = skill_path()
    lines = [
        "You are the **%s** in a delegated development workflow." % role.upper(),
        "",
        "Read and follow this protocol before anything else:",
        "  %s/SKILL.md" % skill,
        "Then read your role card:",
        "  %s/roles/%s.md" % (skill, role),
        "",
        "Task directory (already exists; all task artifacts go here, never in the repo):",
        "  %s" % task.path,
        "It is also in your environment as AGENT_DELEGATION_TASK_DIR.",
        "",
        "Task id: %s" % task.state["id"],
    ]

    if subtask:
        lines += [
            "",
            "Your subtask: %s" % subtask.get("id"),
            "Goal: %s" % subtask.get("goal", ""),
            "Write scope (a hard boundary — edits outside it are reverted):",
        ]
        lines += ["  %s" % g for g in subtask.get("file_scope", [])]
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
    lines += [
        "",
        "Budget for this session: at most %s attempts on this subtask. "
        "Escalate rather than exceeding it." % lims.get("max_attempts_per_subtask", "?"),
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


def env_for(task):
    return {"AGENT_DELEGATION_TASK_DIR": task.path,
            "AGENT_DELEGATION_SKILL_DIR": skill_path()}


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
