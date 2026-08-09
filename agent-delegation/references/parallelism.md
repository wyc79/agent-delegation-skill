# Working alongside other agents

Read when other agents are working on the same task at the same time, when you
hit a merge conflict, or when planning work that several agents will execute.

## The model

Each running subtask gets its **own git worktree** on its own branch, cut from
the task's integration branch. You see a complete checkout that is yours alone.
Other agents' edits are invisible to you until integration — which is the point:
you cannot be broken mid-task by someone else's half-finished work.

The consequence you must internalize: **the code you are reading may already be
out of date**, and the fix is not to peek at other worktrees (you cannot) but to
stay inside your scope so the merge is mechanical.

## Rules while working

1. **Write only inside your `file_scope`.** This is what makes concurrent work
   safe. It is enforced — out-of-scope hunks get flagged at review and may be
   reverted automatically.
2. **Reading outside your scope is fine.** `reads:` in your subtask block lists
   what you depend on; reading more is allowed, but remember those files may be
   changing under you.
3. **Commit checkpoints often, inside your worktree.** They are free, and they
   are what lets a crashed or escalated task resume instead of restarting. Never
   push, never switch branches, never `git checkout` another agent's branch.
4. **Do not reformat, rename, or reorganize** files that are only incidentally in
   your path. A whole-file reformat turns a one-line merge into a manual one.
5. **Treat frozen interfaces as contracts.** If the plan froze a signature or
   signal for another subtask to call, changing it is a major deviation even if
   the file is inside your scope.

## Hotspots

Some files cannot be safely edited by two agents even with careful scoping:
engine scene and prefab files, project settings, dependency manifests, generated
files, and central "everything touches it" modules. The plan marks them
`hotspots:` and the orchestrator serializes anyone who needs them.

If you find yourself needing to edit a hotspot that is not in your subtask's
`hotspots` list, stop and raise a signal. Two agents in one scene file produces a
conflict no merge tool can resolve.

## For planners deciding what can run in parallel

Parallelize only when subtasks have **disjoint write scopes** and no dependency
between them — or when you can freeze the interface between them in the plan,
turning a dependency into a contract both sides code against.

Do not parallelize for its own sake. Two agents on tightly coupled work is slower
than one agent doing both, once you count the conflict, the integrator pass, and
the re-verification. Sequential is the safe default; parallel is an optimization
you justify.

Signs a split should stay sequential: the pieces share a hotspot, the interface
between them is exactly what the task is figuring out, or one piece's design
plausibly changes the other's.

## When integration conflicts

If you are asked to resolve one, you are acting as Integrator — read
`roles/integrator.md`. The short version: resolve by authority (the plan first,
then logged deviations), preserve both intents when they are compatible, never
silently drop a side, and verify the *combination* rather than the pieces.
