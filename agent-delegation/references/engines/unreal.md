# Unreal Engine projects

Read when working in an Unreal repo. The defining constraint: **the most
important files are binary and cannot be merged at all.**

## Files you must handle carefully

| Path | Rule |
|---|---|
| `*.uasset`, `*.umap` | **Binary.** No merge, no diff, no concurrent edits — ever. Always hotspots; conflicts are resolved by taking one file whole. |
| `Content/` | Mostly binary assets. Agents wire assets; they do not author them. |
| `*.generated.h`, `Intermediate/`, `Binaries/`, `Saved/`, `DerivedDataCache/` | Generated. Never edit. Excluded from scope accounting. |
| `*.uproject`, `*.Build.cs`, `*.Target.cs` | Module and dependency configuration — hotspot, and dependency changes need human approval. |
| `Config/Default*.ini` | Global settings — hotspot. |

If two subtasks both need the same `.uasset` or `.umap`, that is a planning
error: they must be sequential. Say so rather than attempting it.

## Blueprints

Blueprints are `.uasset` — binary. You cannot meaningfully read or edit them as
text. Practical consequences:

- Never attempt a textual Blueprint edit.
- A change described as "in the Blueprint" is either a human task or a C++ task,
  and the plan should have said which. Raise a signal if it did not.
- Changing a C++ `UFUNCTION`/`UPROPERTY` signature can break Blueprints that call
  it, with **no compile error** — the break surfaces at load or at runtime. Flag
  every such signature change in your report even when the build is green.

## C++ specifics that bite

- Reflection macros (`UCLASS`, `USTRUCT`, `UPROPERTY`, `UFUNCTION`) drive code
  generation; malformed macros produce errors in generated files, not yours.
- Adding or removing a header often needs a `.Build.cs` dependency change —
  that is a module boundary, so treat it as a plan-level concern, not a quick fix.
- Renaming a `UPROPERTY` loses serialized values in existing assets unless
  redirectors are added to config.
- Hot reload is unreliable; assume a full rebuild is needed to trust a result.

## Verification

Builds are slow — minutes to tens of minutes. Plan around that:

```bash
# compile (paths and target names vary by project — use the ones in your prompt)
<UE>/Engine/Build/BatchFiles/RunUAT.sh BuildEditor -project=<Project>.uproject

# automation tests, headless
<UE>/Engine/Binaries/<Platform>/UnrealEditor-Cmd <Project>.uproject \
  -ExecCmds="Automation RunTests <Suite>; Quit" -unattended -nullrhi -nosplash
```

Because a full build is expensive, **iterate on the smallest compilable unit you
can** and batch your verification. If the plan's iteration budget assumed
web-speed feedback, say so in your report — that assumption is wrong here, and
escalation thresholds should account for it.

**Design for fast tests:** logic in plain C++ classes, free of `UObject` and
engine subsystems, can be covered by low-level automation specs that run without
booting the full editor. Push rules there wherever the plan allows.

## Windows

Unreal development is mostly Windows, where deep `Intermediate/` paths routinely
approach the 260-character limit and the editor holds locks on `Binaries/`. Keep
worktrees at a short root path and expect cleanup to need a retry.

## Before you report

State exactly what you built and ran, and how long it took. "Compiled; automation
suite not run (25 min)" is a legitimate report — a fabricated green run is not,
and here it is especially expensive to discover later.
