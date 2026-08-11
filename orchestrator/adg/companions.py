"""Detect installed companion skills (DESIGN.md §3.2, references/companions.md).

Declared, not discovered: the orchestrator looks once and writes the result into
task.json, so agents never spend a turn hunting the filesystem. Absent
companions are recorded as absent rather than omitted -- "we did not check" and
"it is not installed" are different facts, and only one of them is an excuse.
"""

import os

# name -> the path fragment that proves it is installed, relative to a *skills
# directory*. `_present` also probes it one level down under a `skills/` of its
# own, which is the layout a plugin uses.
#
# These fragments used to start with `skills/` themselves, and every root below
# already ends in one -- so `~/.claude/skills` was probed for
# `~/.claude/skills/skills/brainstorming/SKILL.md` and could never match. Only
# the nested plugin layouts were ever detected, while `winnow.find()` found the
# exactly-parallel `.claude/skills/code-winnow/scripts/scan.py`. That is the
# same one-directional divergence the note below says was closed.
KNOWN = {
    "karpathy-guidelines": os.path.join("karpathy-guidelines", "SKILL.md"),
    "superpowers": os.path.join("brainstorming", "SKILL.md"),
}

# Home-directory installs. Kept deliberately in step with `winnow.SEARCH` and
# `winnow.PLUGIN_ROOTS`, which searched two places this list did not:
# `plugins/marketplaces` and the project-local trees below. The divergence was
# silent and one-directional -- a superpowers install that `winnow.find()` could
# see reported "not installed" here, so `_stage_brainstorm` quietly dropped to
# the generic prompt and `compose` dropped the companions paragraph. Two lists
# of the same thing; `test_the_two_skill_searches_do_not_diverge` now fails if
# they drift apart again.
ROOTS = (
    os.path.join("~", ".claude", "skills"),
    os.path.join("~", ".claude", "plugins", "cache"),
    os.path.join("~", ".claude", "plugins", "marketplaces"),
    os.path.join("~", ".cursor", "skills"),
    os.path.join("~", ".agents", "skills"),
)

# Searched first, because a project-local install is a deliberate override.
PROJECT_ROOTS = (
    os.path.join(".claude", "skills"),
    os.path.join(".agents", "skills"),
)


def _at(base, fragment):
    """Is the skill here, either directly or under this level's own `skills/`?

    Both are real installs: a plain skill sits at `<skills dir>/<name>/SKILL.md`,
    and a plugin carries `<plugin>/skills/<name>/SKILL.md`. Probing only one
    shape is what made a flat install invisible.
    """
    return (os.path.exists(os.path.join(base, fragment))
            or os.path.exists(os.path.join(base, "skills", fragment)))


def _present(fragment, repo=None):
    roots = ROOTS
    if repo:
        roots = tuple(os.path.join(repo, r) for r in PROJECT_ROOTS) + ROOTS
    for root in roots:
        base = os.path.expanduser(root)
        if not os.path.isdir(base):
            continue
        if _at(base, fragment):
            return True
        # plugin caches nest as <marketplace>/<plugin>/<version>/...
        for depth1 in os.listdir(base):
            d1 = os.path.join(base, depth1)
            if not os.path.isdir(d1):
                continue
            if _at(d1, fragment):
                return True
            for depth2 in os.listdir(d1):
                d2 = os.path.join(d1, depth2)
                if not os.path.isdir(d2):
                    continue
                if _at(d2, fragment):
                    return True
                for depth3 in os.listdir(d2):
                    if _at(os.path.join(d2, depth3), fragment):
                        return True
    return False


def detect(repo=None):
    """What is installed. `repo` enables the project-local search, which is
    where a deliberate per-project override lives."""
    return {name: _present(frag, repo) for name, frag in KNOWN.items()}
