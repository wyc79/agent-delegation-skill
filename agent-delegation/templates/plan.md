# Plan — Task <task-id>

## Approach

<Two or three paragraphs: the shape of the solution and why this one. Name the
alternative you rejected and the reason — that is what stops a later agent from
"helpfully" switching to it.>

## What I looked at

<The files and systems you actually read, and what you learned. This is how a
downstream agent knows whether your plan rests on evidence or assumption.>

## Risks

- <What could make this plan wrong, and the earliest signal that it has.>
- <Coupling or hidden dependency an implementer will hit.>

## Open questions

Each with a recommended default, so silence is a safe answer.

- **Q1:** <question> — *default:* <what happens if nobody answers> — *changes if
  answered differently:* <impact>

## Needs human approval

<Dependency changes, public API or save-format changes, deletions, hotspot edits,
anything irreversible. Omit the section if there are none — do not write "N/A".>

## Subtasks

Machine-readable; validated against `schemas/subtask.schema.json`. Every `AC-n`
in `task.md` must be claimed by at least one subtask.

```yaml
- id: st-1-<slug>
  goal: <one actionable sentence>
  file_scope: ["src/<area>/**"]
  reads: ["src/<other>/**"]
  depends_on: []
  parallel_group: A
  hotspots: []
  frozen_interfaces:
    - "signal status_applied(effect: StatusEffect)"
  capability_hint: {reasoning: medium, coding: high}
  estimated_loc: 250
  acceptance: [AC-1, AC-2]
  test_notes: Pure logic, engine-free — fast tests apply.

- id: st-2-<slug>
  goal: <one actionable sentence>
  file_scope: ["src/<area>/<file>"]
  depends_on: [st-1-<slug>]
  parallel_group: B
  hotspots: ["src/<area>/<file>"]
  estimated_loc: 60
  acceptance: [AC-3]
  test_notes: Needs an engine boot — stage-boundary check only.
```

## Test plan

<How the whole thing gets verified: which criteria are covered by fast tests,
which need the engine or a long build, and which cannot be automated at all.
Name the manual checks explicitly — an unlisted manual check does not happen.>

## Integration order

<The order subtasks land, and why. Usually topological by `depends_on`; call out
anything that must be sequential for a reason the dependencies do not express.>
