"""Passive capture of real provider walls.

Every quota classification a live run makes is a test case somebody would
otherwise have to sit down and write. `fixtures/provider-messages.json` is the
corpus detection is tested against, and its own `_about` block says the cases in
it are representative shapes rather than recordings -- because collecting real
ones was a chore nobody was going to do.

So this does it as exhaust. When a run classifies a provider message as a quota
wall, one JSON line goes to `<state_root>/corpus-capture.jsonl` with what was
seen and how it was read. Nothing reads that file back: promotion into the
corpus is a human editing JSON, deliberately, because a case is only worth
having once somebody has decided what it proves.

**Nothing here may affect a run.** Capture is exhaust, not a feature the run
depends on -- an unwritable state directory, a full disk, a path that is
somehow a directory, all end the same way: the line is lost, one debug record
is emitted, and the run carries on knowing nothing about it.

Redaction is best-effort, and saying so is part of the contract. It is stdlib
regex over text this program did not write and cannot parse: home-directory
paths, email addresses and API-key-shaped strings are stripped by pattern, and a
provider that embeds a secret in some shape not listed below will have it
written to this file. The file lives under the user's own state directory and is
never uploaded by anything here, but read a line before you paste it into a
repository.
"""

import json
import logging
import os
import re
import time

from .store import state_root

_LOG = logging.getLogger("adg.corpus")

# Long enough to hold the whole of any provider message seen so far, short
# enough that a runaway transcript cannot fill a home directory one wall at a
# time.
MAX_TEXT = 4000

_HOME = os.path.expanduser("~")

# Applied in order. Home paths first, so a path containing an address or a
# key-shaped segment is gone before the later patterns have to be right about
# it; the generic long-token rule last, because it is the blunt one.
_REDACTIONS = [
    # This machine's actual home, whatever it is called. `_HOME` can be "/" in
    # a container with no HOME set, which would redact the entire message, so
    # anything implausibly short is skipped rather than trusted.
    (re.compile(re.escape(_HOME) + r"[^\s\"'`,;)\]]*")
     if len(_HOME) > 4 else None, "<path>"),
    # And the shapes a home path takes on a machine that is not this one --
    # captured text can arrive from a worktree, a container, or a quoted log.
    (re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:Users|home)[\\/][^\s\"'`,;)\]]*"), "<path>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[abprs]-|AIza|ya29\.)"
                r"[A-Za-z0-9_\-]{8,}"), "<key>"),
    (re.compile(r"\b(?:sk|pk|rk|ak|api|key|token|secret)[-_]"
                r"[A-Za-z0-9_\-]{12,}", re.I), "<key>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{12,}", re.I), "Bearer <key>"),
    # Anything else that is one unbroken run of key characters. Deliberately
    # blunt and deliberately last: provider prose does not contain 40-character
    # words, so what this catches is opaque -- a token, a session id, a base64
    # blob -- and over-redacting a request id costs the corpus nothing while
    # under-redacting a credential costs the user something real.
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "<key>"),
]
_REDACTIONS = [(rx, sub) for rx, sub in _REDACTIONS if rx is not None]


# One debug record per process, not one per wall. This is the lowest-value
# write the program makes, and a complaint per wall trains the reader to skim
# the ones that matter.
_WARNED = False


def path():
    return os.path.join(state_root(), "corpus-capture.jsonl")


def redact(text):
    """Best-effort scrub. See the module docstring for what that is worth."""
    out = str(text or "")
    for rx, sub in _REDACTIONS:
        out = rx.sub(sub, out)
    return out


def capture(verdict, kind, text, reset_at=None, now=None, adapter=None,
            channel=None, model=None):
    """Append one record. Returns True when a line was written.

    Never raises. A caller that changes behaviour on the return value has
    misunderstood what this is for -- it is returned so a test can assert the
    write happened, not so a run can react to it not happening.
    """
    try:
        now = time.time() if now is None else float(now)
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "adapter": adapter,
            "kind": kind,
            "channel": channel,
            "model": model,
            "verdict": verdict,
            "reset_at": float(reset_at) if reset_at else None,
            # Seconds from the classification, which is the form the corpus
            # writes ({T0+N}): an absolute epoch in a fixture pins it to
            # whatever clock the suite happened to run on.
            "reset_in_s": int(float(reset_at) - now) if reset_at else None,
            # What the CLASSIFIER saw, not what the log kept. On the local path
            # those differ -- the agent's own prose is held back from the
            # pattern table -- and a corpus built from the log would be a corpus
            # of a different question.
            "text": redact(text)[:MAX_TEXT],
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        with open(path(), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
        return True
    except Exception as e:                     # noqa: BLE001 -- exhaust, not a feature
        global _WARNED
        if not _WARNED:
            _WARNED = True
            _LOG.debug("corpus capture is not being written (%s): %s", path(), e)
        return False
