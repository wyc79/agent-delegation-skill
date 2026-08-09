# Unity projects

Read when working in a Unity repo. The asset database, not the C# code, is where
multi-agent work goes wrong.

## Files you must handle carefully

| Path | Rule |
|---|---|
| `*.unity` (scenes), `*.prefab` | YAML, but **hotspots**. Merge badly and reference each other by GUID+fileID. One agent at a time, always. |
| `*.meta` | Contains the asset's GUID. **Must travel with its asset** — moving, renaming, or deleting an asset without its meta breaks every reference to it, project-wide. |
| `ProjectSettings/`, `Packages/manifest.json` | Global — hotspots, and dependency changes need human approval. |
| `Library/`, `Temp/`, `obj/`, `*.csproj`, `*.sln` | Generated. Never edit, never commit, excluded from scope accounting. |

**The GUID rule is the one to remember:** references are stored by GUID, not by
path. Renaming a file in git without its `.meta` silently detaches every prefab,
scene, and serialized field that pointed at it, and the failure appears as a
missing script or null reference far from your change. Treat asset moves and
deletions as human decisions.

## Editing scenes and prefabs

- Prefer changes in **C#** or via a prefab variant over hand-editing YAML.
- If you must edit YAML, change values in place; do not reorder or reindent
  blocks, and never renumber `fileID`s.
- Confirm the project has *Force Text* serialization and *Visible Meta Files*
  before assuming a scene is diffable at all.
- A structural change to a large scene is a whole subtask, never a side effect of
  one.

## Serialization traps that break requirements quietly

- Renaming a serialized field **loses its inspector-set value** unless you add
  `[FormerlySerializedAs("oldName")]`. This shows up as a designer's tuning
  silently reverting to defaults.
- Changing a field's type, or public→private without `[SerializeField]`, has the
  same effect.
- `ScriptableObject` assets carry data your code change may invalidate.

If a plan step involves renaming or retyping serialized state, say so in your
report even when everything compiles.

## Verification

Compile-only (fast enough to iterate on):

```bash
Unity -batchmode -quit -projectPath . -logFile -
```

Edit-mode tests are moderately fast; play-mode tests boot the engine and are
slow. Use the project's actual commands from your prompt.

```bash
Unity -batchmode -runTests -testPlatform EditMode -projectPath . -logFile -
```

Two practical constraints: a Unity licence must be activated in the environment,
and **only one Unity process may open a project directory at a time** — worktrees
give each agent its own copy, so never point two agents at one project path.

**Design for fast tests:** plain C# classes with no `MonoBehaviour` dependency
run in EditMode in seconds. Logic reachable only through a scene needs play mode.
Push the rules into POCOs and let components call them.

## Windows

Unity development is mostly Windows. Long paths, case-insensitive scope
matching, CRLF diffs, and the editor holding file locks all bite here — see
`DESIGN.md` §4.4b. Practical rule: keep worktrees at a short path, and expect
`git worktree remove` to fail while the editor is open.

## Before you report

Say which test platform you ran (EditMode / PlayMode / compile only) and what you
skipped. A green compile is not a passing test, and a reviewer will check.
