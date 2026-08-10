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

ROOTS = (
    os.path.join("~", ".claude", "skills"),
    os.path.join("~", ".claude", "plugins", "cache"),
    os.path.join("~", ".cursor", "skills"),
    os.path.join("~", ".agents", "skills"),
)


def _present(fragment):
    for root in ROOTS:
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


def detect():
    return {name: _present(frag) for name, frag in KNOWN.items()}
