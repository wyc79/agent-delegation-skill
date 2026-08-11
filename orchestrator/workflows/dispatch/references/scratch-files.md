# Writing files inside the repo

Read when something forces you to put a file in the working tree that is not
source, not a test, and not durable documentation — a tool that only accepts a
relative output path, an engine test runner that writes reports next to the
project, a profiler dump, a generated fixture.

The default remains: **it goes in `$TASK_DIR`.** This page is the narrow
exception, and it exists because "the tool made me" is otherwise a hole big
enough to drive the whole convention through.

## The rule

If a file must land inside the repository, it must go somewhere the repo
**already ignores**. Verify it — do not assume from the path:

```bash
git check-ignore -v <path>     # prints the rule that ignores it, exit 0
```

Exit 0 with a printed rule means the location is safe. **Exit 1 means stop** —
that path would show up in `git status`, in the diff, and in the PR.

## What you may not do to satisfy that rule

- **Do not edit `.gitignore`.** Adding an entry is a change to the project, it
  lands in the diff, and it is exactly the pollution this convention prevents.
  If the repo has no suitable ignored location, raise it as a `blocked_command`
  signal and stop. That signal stops the run for a human, who can add an entry
  to `.git/info/exclude` — uncommitted, and shared by every worktree. Nothing
  in the orchestrator does it for you, so do not stop expecting it to be fixed
  underneath you.
- **Do not use `git add -f`** on anything under an ignored path, ever.
- **Do not repurpose a meaningful ignored directory.** `Library/`, `.godot/`,
  `node_modules/`, and `target/` belong to their tools; dropping files in them
  can confuse a rebuild or be wiped mid-run without warning.

## Choosing a location

Prefer, in order:

1. A scratch subdirectory the project already ignores for this purpose (many
   repos ignore `tmp/`, `scratch/`, `.cache/`, or `*.local.*`).
2. A path the orchestrator gave you explicitly in your prompt.
3. Nothing — escalate instead.

Whatever you choose, treat it as **ephemeral**: assume it can vanish between
sessions. `git clean -xdf`, a fresh worktree, or a tool's own cleanup will
remove it, and none of those are unusual.

## Anything worth keeping gets copied out

An ignored in-repo file is a staging area, never a destination. Before you
finish, copy anything of lasting value into `$TASK_DIR` — test output into
`$TASK_DIR/verify/`, notes and analysis into your report or `decisions.md`.

The test: if your report **cites** it as evidence, it must exist in `$TASK_DIR`,
because the next agent may be running in a different worktree that never had
your scratch file.

## Before you exit

The working tree must contain only your intended source changes:

```bash
git status --porcelain      # nothing unexpected, ignored or not
```

If a scratch file shows up here, it is not actually ignored — remove it and fix
the location. Note in your report where you wrote scratch files and whether you
cleaned them up; an unexplained artifact in someone's checkout is a small mystery
that costs real time to run down.

## Durable documentation is different

A design document, an ADR, or a README update that will matter after this task is
finished is **project documentation, not scratch**. It belongs in the repo's
normal docs location, committed like any other change — but only if the plan
called for it or you log it as a deviation. Do not smuggle documentation into an
ignored directory to avoid review, and do not commit run bookkeeping as if it
were documentation.
