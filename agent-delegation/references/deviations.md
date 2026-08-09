# Deviations

Read when reality does not match the plan and you must decide what to do about
it.

Plans are written before contact with the code, so deviating is normal and
expected. **Silently** deviating is the problem: it makes the plan a lie, makes
review meaningless, and leaves the next agent building on assumptions that no
longer hold.

## The severity test

A deviation is **major** if *any* of these is true:

- It touches files outside your subtask's `file_scope`.
- It changes an interface the plan named (a signature, a signal, a schema, a
  file format) — especially one the plan *froze* for another subtask.
- It contradicts an entry in `decisions.md`.
- It invalidates another subtask's plan.
- It changes observable behavior a requirement (`AC-n`) depends on.

Otherwise it is **minor**.

Minor: log it and keep going. Major: **stop, log it, and raise a signal** — see
`references/escalation.md`. Do not proceed on your own judgment through a major
deviation; another agent's work probably depends on the thing you are changing.

When you are genuinely unsure which it is, treat it as major. The cost of an
unnecessary escalation is a few minutes. The cost of an unnoticed interface
change is a broken integration nobody can explain.

## The log format

Append to `$TASK_DIR/deviations.md`. Append only — never edit or delete existing
entries, including your own.

```text
dev-<n> | <role>:<subtask> | <what the plan said, with a line reference>
        | did instead: <what you actually did>
        | why: <the reason reality differed>
        | severity: minor|major
```

Worked example:

```text
dev-3 | impl:st-2 | plan.md:L48 said extend BaseSystem
      | did instead: composition wrapper holding a BaseSystem instance
      | why: BaseSystem is final in engine 4.3; subclassing fails to compile
      | severity: major (plan.md:L52 has st-4 expecting a BaseSystem subclass)
```

The `why` matters more than it looks. A reviewer reading `deviations.md` months
later needs to know whether the plan was wrong or the implementer was confused,
and only the reason distinguishes them.

## What is *not* a deviation

- Implementation detail the plan left open. If the plan said "cache the result"
  and you chose a dictionary, that is you doing your job.
- Unrelated things you noticed and did **not** change (put those in
  `decisions.md` as observations, so they are not lost).
- Test additions beyond the plan's minimum, if they stay in your scope.

## What is *not* an acceptable deviation, at any severity

- Deleting or weakening a test to make a build pass. If a test is wrong, log the
  deviation, explain precisely why it is wrong, and raise a signal.
- Removing a requirement's functionality because it was hard.
- Reverting another agent's committed work.
- Widening your `file_scope` because the change was "small."

## For reviewers

Read `plan.md` **through** `deviations.md`: a logged deviation amends the plan and
should be judged on its merits, not treated as a violation. The thing to hunt for
is the inverse — a diff hunk with **no** corresponding scope or deviation entry.
That gap is where unrecorded decisions hide, and it is mechanically detectable,
so there is no excuse for missing it.
