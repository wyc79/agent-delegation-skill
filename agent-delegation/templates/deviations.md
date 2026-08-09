# Deviations — Task <task-id>

Append-only. Never edit or delete an existing entry, including your own.
Severity rules and worked examples: `references/deviations.md`.

Format:

```text
dev-<n> | <role>:<subtask> | <what the plan said, with a line reference>
        | did instead: <what you actually did>
        | why: <the reason reality differed>
        | severity: minor|major
```

A **major** deviation (outside your file scope, changes a named interface,
contradicts a decision, invalidates another subtask, or changes behavior an
`AC-n` depends on) must also raise a signal in your report — logging it is not
enough on its own.

---

<!-- Entries below. Newest last. -->

dev-1 | impl:st-1-example | plan.md:L48 said extend BaseSystem
      | did instead: composition wrapper holding a BaseSystem instance
      | why: BaseSystem is final in this engine version; subclassing fails to compile
      | severity: major (plan.md:L52 has st-4 expecting a BaseSystem subclass)
