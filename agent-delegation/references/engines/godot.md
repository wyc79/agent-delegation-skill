# Godot projects

Read when working in a Godot repo. Web-development habits do not transfer
cleanly here.

## Files you must handle carefully

| Path | Rule |
|---|---|
| `*.tscn`, `*.tres` | Text, but **treat as hotspots**. Node paths and sub-resource ids break silently under naive edits. One agent at a time. |
| `project.godot` | Global config — hotspot. Input maps, autoloads, and layer names live here and everything references them. |
| `.godot/`, `.import/` | Generated. Never hand-edit. Excluded from scope accounting. |
| `*.import` sidecars | Belong to their asset — they travel together; do not edit alone. |
| `export_presets.cfg` | Contains credentials in some setups. Do not read or modify. |

## Editing scenes

Prefer changing scenes **from script** or editing the smallest possible region of
the `.tscn`. When you must hand-edit:

- Keep `[ext_resource]` / `[sub_resource]` ids stable. Renumbering them silently
  detaches references elsewhere in the file.
- Node paths in signal connections and `NodePath` exports are strings — renaming
  a node breaks them with no compile error. Grep for the old name before
  renaming, and add it to your report if you cannot check every reference.
- Adding a node is far safer than restructuring the tree. If the plan calls for
  restructuring a large scene, expect churn and escalate early rather than
  fighting the file repeatedly.

## Verification

Fast checks, safe to run every iteration:

```bash
godot --headless --check-only --script <file.gd>   # parse/type check
godot --headless --quit                            # project imports cleanly
```

Test suites (GUT, GdUnit) need a real engine boot — slower, so run them at
stage boundaries rather than after every edit. Your prompt names the project's
exact commands; prefer those over these defaults.

**Design for fast tests.** Pure GDScript logic with no `Node` dependency can be
unit-tested in seconds; the same logic reached only through a scene needs the
whole engine. When you have a choice, keep the rules in a plain class and let the
node call into it — this is the single biggest lever on iteration speed in a
Godot project.

## GDScript specifics worth remembering

- Static typing (`var x: int`) turns runtime surprises into parse-time errors —
  match the file's existing convention, but prefer typed in new code.
- `@onready` runs after `_ready()` ordering; a node fetched too early is `null`
  with no error until it is used.
- Signals are the idiomatic decoupling seam. If the plan froze an interface
  between systems, it is probably a signal — do not replace it with a direct call
  because that seemed simpler.
- `class_name` registers globally; adding one can collide with an existing name
  anywhere in the project.

## Before you report

State explicitly which checks you ran and which you skipped. "Scene loads
headless; GUT suite not run (slow)" is a useful, honest report. Claiming a scene
works when you only parsed the script is not.
