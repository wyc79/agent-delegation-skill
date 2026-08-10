"""Minimal YAML reader for the subset this project writes.

PyYAML is not in the stdlib and the orchestrator must run with no install step,
so this parses the restricted subset used by registry.default.yaml and the
`subtasks` blocks in plan.md: nested maps, block lists, flow maps/lists,
comments, quoted strings, and folded (`>`) scalars.

It is deliberately not a YAML implementation. Anything outside the subset
raises YamlError rather than guessing -- a config that silently parses to the
wrong shape is worse than one that refuses to load.
"""

import re

__all__ = ["load", "YamlError"]


class YamlError(ValueError):
    pass


_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _scalar(text):
    t = text.strip()
    if not t:
        return None
    if t[0] in "\"'":
        if len(t) < 2 or t[-1] != t[0]:
            raise YamlError("unterminated quoted string: %s" % text)
        return t[1:-1]
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT.match(t):
        return int(t)
    if _FLOAT.match(t):
        return float(t)
    return t


def _split_top(text, close):
    """Split a flow collection body on commas not nested in brackets."""
    parts, depth, buf, quote = [], 0, [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if quote:
        raise YamlError("unterminated string in flow collection")
    if depth:
        raise YamlError("unbalanced %s in flow collection" % close)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _value(text):
    """Parse a scalar or an inline flow collection."""
    t = text.strip()
    if t.startswith("["):
        if not t.endswith("]"):
            raise YamlError("unterminated flow list: %s" % text)
        return [_value(p) for p in _split_top(t[1:-1], "]")]
    if t.startswith("{"):
        if not t.endswith("}"):
            raise YamlError("unterminated flow map: %s" % text)
        out = {}
        for part in _split_top(t[1:-1], "}"):
            k, sep, v = part.partition(":")
            if not sep:
                raise YamlError("flow map entry without ':': %s" % part)
            out[str(_scalar(k))] = _value(v)
        return out
    return _scalar(t)


def _key_colon(text):
    """Index of the colon separating a key from its value, or -1 if the line
    is a plain scalar. Quotes and brackets are skipped: a naive partition(":")
    splits `- "def f(a, b)  # see calc.py; note"` in the middle of a string and
    then fails on the unterminated quote."""
    depth, quote = 0, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            # A key colon is followed by whitespace or ends the line;
            # "a:b" inside a URL or time is part of the scalar.
            if i + 1 >= len(text) or text[i + 1] in " \t":
                return i
    return -1


def _strip_comment(line):
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


class _Lines:
    """Significant lines as (indent, text, lineno), with a one-item lookahead."""

    def __init__(self, src):
        self.items = []
        for n, raw in enumerate(src.splitlines(), 1):
            if raw.strip() in ("---", "..."):
                continue
            text = _strip_comment(raw)
            if not text.strip():
                continue
            self.items.append((len(text) - len(text.lstrip(" ")), text.strip(), n))
        self.i = 0

    def peek(self):
        return self.items[self.i] if self.i < len(self.items) else None

    def next(self):
        item = self.items[self.i]
        self.i += 1
        return item


# Block scalar indicators, with optional chomping (- strip, + keep) and an
# explicit indent digit. Models emit `>-` and `|` freely, so all of these have
# to work or a perfectly good plan fails to parse.
_BLOCK = re.compile(r"^([>|])([+-]?)(\d?)$")


def _block_scalar(lines, parent_indent, style):
    """Collect a block scalar: folded (`>`) joins lines with spaces, literal
    (`|`) keeps newlines. Chomping only affects trailing blank lines, which
    this reader has already dropped, so it needs no special handling."""
    parts = []
    while True:
        nxt = lines.peek()
        if nxt is None or nxt[0] <= parent_indent:
            break
        parts.append(lines.next()[1])
    return ("\n" if style == "|" else " ").join(parts)


def _parse_block(lines, indent):
    nxt = lines.peek()
    if nxt is None:
        return None
    return _parse_list(lines, indent) if nxt[1].startswith("- ") or nxt[1] == "-" \
        else _parse_map(lines, indent)


def _parse_list(lines, indent):
    out = []
    while True:
        nxt = lines.peek()
        if nxt is None or nxt[0] < indent or not (nxt[1].startswith("- ") or nxt[1] == "-"):
            break
        cur_indent, text, lineno = lines.next()
        body = text[2:].strip() if text.startswith("- ") else ""
        if not body:
            out.append(_parse_block(lines, cur_indent + 1))
            continue
        ci = _key_colon(body)
        key, rest = (body[:ci], body[ci + 1:]) if ci >= 0 else ("", "")
        if ci >= 0 and not key.strip().startswith(("[", "{", '"', "'")):
            # "- key: value" starts a map whose remaining keys are indented to
            # the column where `key` began.
            item = {str(_scalar(key)): _inline_or_block(lines, rest, cur_indent + 2)}
            item.update(_parse_map(lines, cur_indent + 2, initial=True) or {})
            out.append(item)
        else:
            out.append(_value(body))
    return out


def _inline_or_block(lines, rest, child_indent):
    marker = _BLOCK.match(rest.strip())
    if marker:
        return _block_scalar(lines, child_indent - 1, marker.group(1))
    if rest.strip():
        return _value(rest)
    nxt = lines.peek()
    if nxt is None or nxt[0] < child_indent:
        return None
    return _parse_block(lines, nxt[0])


def _parse_map(lines, indent, initial=False):
    out = {}
    while True:
        nxt = lines.peek()
        if nxt is None or nxt[0] < indent:
            break
        if nxt[1].startswith("- ") or nxt[1] == "-":
            if initial:
                break
            raise YamlError("line %d: list item where a mapping key was expected" % nxt[2])
        cur_indent, text, lineno = lines.next()
        ci = _key_colon(text)
        if ci < 0:
            raise YamlError("line %d: expected 'key: value', got %r" % (lineno, text))
        key, rest = text[:ci], text[ci + 1:]
        out[str(_scalar(key))] = _inline_or_block(lines, rest, cur_indent + 1)
    return out


def load(src):
    """Parse a YAML subset document into Python data."""
    lines = _Lines(src)
    if lines.peek() is None:
        return None
    return _parse_block(lines, lines.peek()[0])
