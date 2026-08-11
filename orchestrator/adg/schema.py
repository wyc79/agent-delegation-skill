"""Just enough JSON Schema to validate agent reports.

Covers the keywords the skill's schemas actually use: type, required,
properties, additionalProperties, enum, items, pattern, minLength, minItems.
An unknown keyword is ignored rather than silently passing everything -- but
the schemas here are ours, so the subset is a closed set, not a guess.

Validation exists so a malformed report is caught as a protocol failure at the
boundary instead of surfacing as a confusing KeyError three stages later.
"""

import json
import os
import re

from . import workflow


def schemas_dir():
    """Where the report and verdict schemas live.

    The BUNDLED directory, deliberately, and not the workflow in force. The
    report envelope is how this program reads an agent's result -- it is the
    runtime's contract, not the workflow's, and a hosted workflow should not
    have to ship a copy of it to be loadable.

    Resolving it against `--workflow` was worse than wrong, it was invisible:
    this module froze the path at import time, so a workflow directory with no
    `schemas/` still validated against the bundled copy and only failed under
    the real CLI, where `_collect_report` would raise FileNotFoundError into the
    generic handler and report CRASHED. Computed on each call now, so there is
    no import-order dependency left to be lucky about.
    """
    return os.path.join(workflow.default_dir(), "schemas")

_TYPES = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool,
}


class Invalid(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors[:6]) + ("" if len(errors) <= 6 else " ..."))


def load(name):
    with open(os.path.join(schemas_dir(), name), encoding="utf-8") as fh:
        return json.load(fh)


def _check(data, schema, path, errs):
    t = schema.get("type")
    if t:
        py = _TYPES.get(t)
        if py and (isinstance(data, bool) and py is not bool or not isinstance(data, py)):
            errs.append("%s: expected %s, got %s" % (path or "<root>", t, type(data).__name__))
            return
    if "enum" in schema and data not in schema["enum"]:
        errs.append("%s: %r not one of %s" % (path, data, schema["enum"]))
    if isinstance(data, str):
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errs.append("%s: %r does not match %s" % (path, data, schema["pattern"]))
        if "minLength" in schema and len(data) < schema["minLength"]:
            errs.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errs.append("%s: needs at least %d item(s)" % (path, schema["minItems"]))
        if "items" in schema:
            for i, item in enumerate(data):
                _check(item, schema["items"], "%s[%d]" % (path, i), errs)
    if isinstance(data, dict):
        if "minProperties" in schema and len(data) < schema["minProperties"]:
            errs.append("%s: needs at least %d field(s)"
                        % (path or "<root>", schema["minProperties"]))
        for key in schema.get("required", []):
            if key not in data:
                errs.append("%s: missing required field %r" % (path or "<root>", key))
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props:
                    errs.append("%s: unexpected field %r" % (path or "<root>", key))
        for key, sub in props.items():
            if key in data:
                _check(data[key], sub, "%s.%s" % (path, key) if path else key, errs)


def validate(data, schema):
    errs = []
    _check(data, schema, "", errs)
    if errs:
        raise Invalid(errs)
    return True


def validate_report(data):
    return validate(data, load("report.schema.json"))


def validate_verdict(data):
    return validate(data, load("verdict.schema.json"))
