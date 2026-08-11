"""Detect installed companion skills (DESIGN.md §3.2, references/companions.md).

Declared, not discovered: the orchestrator looks once and writes the result into
task.json, so agents never spend a turn hunting the filesystem. Absent
companions are recorded as absent rather than omitted -- "we did not check" and
"it is not installed" are different facts, and only one of them is an excuse.
"""

import os

# name -> the path fragment that proves it is installed
KNOWN = {
    "karpathy-guidelines": os.path.join("skills", "karpathy-guidelines", "SKILL.md"),
    "superpowers": os.path.join("skills", "brainstorming", "SKILL.md"),
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


def _present(fragment, repo=None):
    roots = ROOTS
    if repo:
        roots = tuple(os.path.join(repo, r) for r in PROJECT_ROOTS) + ROOTS
    for root in roots:
        base = os.path.expanduser(root)
        if not os.path.isdir(base):
            continue
        if os.path.exists(os.path.join(base, fragment)):
            return True
        # plugin caches nest as <marketplace>/<plugin>/<version>/...
        for depth1 in os.listdir(base):
            d1 = os.path.join(base, depth1)
            if not os.path.isdir(d1):
                continue
            if os.path.exists(os.path.join(d1, fragment)):
                return True
            for depth2 in os.listdir(d1):
                d2 = os.path.join(d1, depth2)
                if os.path.isdir(d2) and os.path.exists(os.path.join(d2, fragment)):
                    return True
                if os.path.isdir(d2):
                    for depth3 in os.listdir(d2):
                        if os.path.exists(os.path.join(d2, depth3, fragment)):
                            return True
    return False


def detect(repo=None):
    """What is installed. `repo` enables the project-local search, which is
    where a deliberate per-project override lives."""
    return {name: _present(frag, repo) for name, frag in KNOWN.items()}
