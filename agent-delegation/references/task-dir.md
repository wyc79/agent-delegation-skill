# Finding the task directory

Read when `$AGENT_DELEGATION_TASK_DIR` is not set and you must locate the task
directory yourself.

Derive it exactly as written here. Every agent in the pipeline must compute the
**same** path, so an improvised variation means you read a directory nobody else
writes to — and the failure looks like "the plan is missing," not like a bug.

## The two parts

```text
<state root>/agent-delegation/projects/<project-key>/tasks/<task-id>/
```

**State root**, in this order:

| Condition | Root |
|---|---|
| `XDG_STATE_HOME` is set (any OS) | `$XDG_STATE_HOME` |
| Windows | `%LOCALAPPDATA%` |
| Linux, macOS | `$HOME/.local/state` |

**Project key** = `<repo folder name>-<first 8 hex of sha256(canonical common dir)>`.

The common directory is the same for the main checkout and every worktree, which
is exactly why it identifies the project:

```bash
git rev-parse --path-format=absolute --git-common-dir
```

Canonicalize that string before hashing:

1. Use forward slashes.
2. Remove any trailing slash.
3. **On Windows only**, lowercase the whole path (the filesystem is
   case-insensitive, so `C:/Repo/.git` and `c:/repo/.git` are one directory and
   must not produce two keys).

## Recipes

POSIX shell (Linux, macOS, Git Bash / WSL on Windows):

```bash
CD=$(git rev-parse --path-format=absolute --git-common-dir)
CD=${CD%/}
NAME=$(basename "$(dirname "$CD")")
HASH=$(printf %s "$CD" | { command -v sha256sum >/dev/null && sha256sum || shasum -a 256; } | cut -c1-8)
ROOT=${XDG_STATE_HOME:-$HOME/.local/state}
echo "$ROOT/agent-delegation/projects/$NAME-$HASH/tasks/"
```

PowerShell (native Windows):

```powershell
$cd = (git rev-parse --path-format=absolute --git-common-dir).Trim().TrimEnd('/').ToLower()
$name = Split-Path (Split-Path $cd -Parent) -Leaf
$sha = [System.Security.Cryptography.SHA256]::Create()
$hash = ([BitConverter]::ToString(
  $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($cd))) -replace '-','').ToLower().Substring(0,8)
$root = if ($env:XDG_STATE_HOME) { $env:XDG_STATE_HOME } else { $env:LOCALAPPDATA }
"$root/agent-delegation/projects/$name-$hash/tasks/"
```

Both hash the **UTF-8 bytes of the canonical path string** — not the file
contents, and not a normalized real path with symlinks resolved.

## Then find your task

List that directory and match the task id from your prompt. If exactly one task
exists and your prompt named none, you may use it — but say so in your report.

## If it is not there

**Stop.** Do not create it, and do not fall back to writing inside the repo.
A missing task directory means one of:

- you are in the wrong repository or a stale worktree,
- the orchestrator never created the task, or
- the state root differs from the one the orchestrator used (most often
  `XDG_STATE_HOME` set for one process and not the other).

All three are worth surfacing rather than working around. Report `blocked` with
the path you computed and the value of `AGENT_DELEGATION_TASK_DIR`,
`XDG_STATE_HOME`, and the common dir you derived it from — that is enough for
whoever debugs it to see the mismatch immediately.
