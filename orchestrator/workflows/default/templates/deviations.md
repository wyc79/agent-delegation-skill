# Deviations — Task <task-id>

Append-only. Never edit or delete an existing entry, including your own.
One entry per departure, newest last. All five fields are required.

Severity rules, worked examples, and what is never an acceptable deviation:
`references/deviations.md`.

The id is namespaced by **subtask**, not by agent — `dev-st-2-1`, `dev-st-2-2`.
A bare `dev-3` collides with a concurrent writer's, and the report schema rejects
it outright. If entries for your subtask are already here, keep numbering from
the highest one: a second agent may be continuing the first one's work.

```text
dev-<your-subtask-or-role>-<n> | <role>:<subtask> | <what the plan said, with a line reference>
        | did instead: <what you actually did>
        | why: <the reason reality differed>
        | severity: minor|major
```

---
