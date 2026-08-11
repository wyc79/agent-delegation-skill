# Task <task-id>: <short title>

## Request (verbatim)

> <the user's request, unedited — never paraphrase this section>

## What this means

<One paragraph restating the request in terms of this codebase. Name the systems
involved. This is where an outsider's confusion gets resolved, so write it for
someone who has not seen the repo.>

## Acceptance criteria

Numbered, checkable, one claim each. These ids are referenced by subtasks, tests,
and the review verdict, so they must stay stable once assigned.

- **AC-1** — <observable statement, e.g. "Poison deals damage once per turn for its duration">
- **AC-2** — <e.g. "Reapplying poison refreshes duration rather than stacking damage">
- **AC-3** — <e.g. "Affected enemies show a poison icon above their health bar">
- **AC-4** — <e.g. "Save files written before this change still load">

## Non-goals

What this task explicitly does **not** cover. Downstream agents treat this list as
a boundary, and the reviewer uses it against scope creep.

- <e.g. "No other status effects — poison only">
- <e.g. "No balance tuning of existing damage numbers">

## Constraints

<Anything that limits how this may be done: performance budgets, compatibility
requirements, APIs that must not change, platforms that must keep working.>

## Context worth knowing

<Prior attempts, related tickets, links, or gotchas the requester already knows
about. Optional — omit the section rather than padding it.>
