"""Enough YAML to read a Docker Compose file, with the line each value came from.

Homestead runs on the standard library alone, and a Compose file uses a small,
well-worn corner of YAML: block mappings and sequences, plain and quoted
scalars, flow lists and maps, block text, anchors, aliases and merge keys.
That corner is what this reads. Anything outside it - multi-document streams,
complex keys, tags beyond ignoring them - is an error that names the line,
rather than a silent guess.

Mappings and sequences come back as Node dicts and lists: ordinary containers
that also remember the line of each key or item, so a Compose error can point
at the line it is about.
"""
import re


TABS = "indent with spaces, not tabs"


class YamlError(ValueError):
    def __init__(self, message, line=0):
        super().__init__(f"line {line}: {message}" if line else message)
        self.message, self.line = message, line


class NodeDict(dict):
    """A mapping that remembers where it started and where each key was."""

    def __init__(self, line=0):
        super().__init__()
        self.line = line
        self.lines = {}


class NodeList(list):
    def __init__(self, line=0):
        super().__init__()
        self.line = line
        self.lines = []


def line_of(node, key=None, default=0):
    """The line a mapping key or list index came from, or the node's own line."""
    if key is not None and isinstance(node, NodeDict):
        return node.lines.get(key, node.line or default)
    if key is not None and isinstance(node, NodeList) and isinstance(key, int) and key < len(node.lines):
        return node.lines[key]
    return getattr(node, "line", default) or default


_INT = re.compile(r"[-+]?(0|[1-9][0-9]*)")
_FLOAT = re.compile(r"[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?")


def _plain(text):
    """Type a plain scalar the way YAML 1.2 does for the values Compose uses."""
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/",
            " ": " ", "e": "\x1b", "a": "\a", "b": "\b", "v": "\v", "f": "\f"}


def _double(body, line):
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 1
            if i >= len(body):
                raise YamlError("a double-quoted string ends in a backslash", line)
            code = body[i]
            if code in _ESCAPES:
                out.append(_ESCAPES[code])
            elif code in "xuU":
                size = {"x": 2, "u": 4, "U": 8}[code]
                digits = body[i + 1:i + 1 + size]
                if len(digits) != size or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise YamlError(f"bad \\{code} escape in a double-quoted string", line)
                out.append(chr(int(digits, 16)))
                i += size
            else:
                raise YamlError(f"unknown escape \\{code} in a double-quoted string", line)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _strip_comment(text):
    """Drop a trailing # comment, leaving any # inside quotes alone."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    continue
                quote = None
            elif ch == "\\" and quote == '"':
                continue
        elif ch in "'\"" and (i == 0 or text[i - 1] in " \t[{,:-"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _split_key(text):
    """Split "key: value" at the first ": " (or trailing ":") outside quotes.

    Returns (key, rest) or None when the text is not a mapping entry, which is
    how "8080:80" and "nginx:1.2" stay scalars.
    """
    quote = None
    depth = 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"" and i == 0:
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth <= 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].rstrip(), text[i + 1:].strip()
    return None


class _Lines:
    """The document as (line number, indent, text) with blanks and comments gone."""

    def __init__(self, source):
        self.rows = []
        self.raw = source.splitlines()
        for number, raw in enumerate(self.raw, 1):
            if "\t" in raw[:len(raw) - len(raw.lstrip())]:
                raise YamlError(TABS, number)
            text = _strip_comment(raw)
            stripped = text.strip()
            if not stripped:
                continue
            if stripped in ("---", "...") and not raw.startswith(" "):
                if stripped == "---" and self.rows:
                    raise YamlError("only one document is read; this starts a second", number)
                continue
            self.rows.append((number, len(text) - len(text.lstrip(" ")), stripped))
        self.i = 0

    def peek(self):
        return self.rows[self.i] if self.i < len(self.rows) else None

    def next(self):
        row = self.rows[self.i]
        self.i += 1
        return row


class _Parser:
    def __init__(self, source):
        self.lines = _Lines(source)
        self.anchors = {}

    # ------------------------------------------------------------ blocks
    def document(self):
        first = self.lines.peek()
        if not first:
            return None
        value = self.block(first[1])
        left = self.lines.peek()
        if left:
            raise YamlError("this line is indented less than the one it follows belongs to", left[0])
        return value

    def block(self, indent):
        number, at, text = self.lines.peek()
        if at != indent:
            raise YamlError("unexpected indentation", number)
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def mapping(self, indent):
        node = NodeDict(self.lines.peek()[0])
        merged = set()
        while True:
            row = self.lines.peek()
            if not row or row[1] < indent:
                return node
            number, at, text = row
            if at > indent:
                raise YamlError("unexpected indentation", number)
            if text == "-" or text.startswith("- "):
                raise YamlError("a list item where a key was expected", number)
            self.lines.next()
            parts = _split_key(text)
            if parts is None:
                raise YamlError(f"expected \"key: value\", found \"{text[:40]}\"", number)
            key_text, rest = parts
            key = self.key(key_text, number)
            value = self.value_after_key(rest, indent, number)
            if key == "<<":
                merged.update(self.merge(node, value, number))
                continue
            if key in node and key not in merged:
                raise YamlError(f"\"{key}\" is set twice in the same mapping", number)
            # A key written out beats the same key brought in by a merge.
            merged.discard(key)
            node[key] = value
            node.lines[key] = number

    def merge(self, node, value, number):
        sources = value if isinstance(value, list) else [value]
        added = set()
        for source in sources:
            if not isinstance(source, dict):
                raise YamlError("<< merges a mapping, usually an alias like *defaults", number)
            for key, item in source.items():
                if key not in node:
                    node[key] = item
                    node.lines[key] = number
                    added.add(key)
        return added

    def key(self, text, number):
        if text.startswith(("'", '"')):
            return str(self.quoted(text, number)[0])
        if text.startswith(("?", "[", "{", "&", "*", "!")):
            raise YamlError("complex keys are not supported in a Compose file", number)
        return text

    def value_after_key(self, rest, indent, number):
        anchor, rest = self.take_anchor(rest, number)
        if not rest:
            value = self.nested(indent, number, allow_same_indent_list=True)
        else:
            value = self.inline(rest, indent, number)
        if anchor:
            self.anchors[anchor] = value
        return value

    def nested(self, indent, number, allow_same_indent_list=False):
        row = self.lines.peek()
        if not row:
            return None
        if row[1] > indent:
            return self.block(row[1])
        # YAML lets a list sit at the same indent as the key that owns it.
        if allow_same_indent_list and row[1] == indent and (row[2] == "-" or row[2].startswith("- ")):
            return self.sequence(indent)
        return None

    def sequence(self, indent):
        node = NodeList(self.lines.peek()[0])
        while True:
            row = self.lines.peek()
            if not row or row[1] < indent:
                return node
            number, at, text = row
            if at > indent:
                raise YamlError("unexpected indentation", number)
            if not (text == "-" or text.startswith("- ")):
                return node
            self.lines.next()
            rest = text[1:].strip()
            anchor, rest = self.take_anchor(rest, number)
            if not rest:
                value = self.nested(indent, number)
            elif self.mapping_entry(rest):
                # "- key: value" opens a mapping whose further keys line up
                # with that first key, so this row is re-read as that key.
                inner = at + text.find(rest)
                self.lines.i -= 1
                self.lines.rows[self.lines.i] = (number, inner, rest)
                value = self.mapping(inner)
            else:
                value = self.inline(rest, indent, number)
            if anchor:
                self.anchors[anchor] = value
            node.append(value)
            node.lines.append(number)

    @staticmethod
    def mapping_entry(text):
        if text.startswith(("[", "{")):
            return False
        if text.startswith(("'", '"')):
            end = text.find(text[0], 1)
            return end > 0 and text[end + 1:end + 2] == ":" and text[end + 2:end + 3] in ("", " ")
        return _split_key(text) is not None

    def take_anchor(self, text, number):
        if text.startswith("&"):
            match = re.match(r"&([^\s,\[\]{}]+)\s*", text)
            if not match:
                raise YamlError("an anchor needs a name", number)
            return match.group(1), text[match.end():]
        if text.startswith("!"):
            # A tag: read the value as though it were not there.
            match = re.match(r"!\S*\s*", text)
            return None, text[match.end():]
        return None, text

    # ----------------------------------------------------------- scalars
    def inline(self, text, indent, number):
        if text.startswith("*"):
            name = text[1:].strip()
            if name not in self.anchors:
                raise YamlError(f"*{name} refers to an anchor that is not defined above it", number)
            return self.anchors[name]
        if text[0] in "|>":
            return self.block_text(text, indent, number)
        if text[0] in "[{":
            value, end = self.flow(text, 0, number)
            if text[end:].strip():
                raise YamlError(f"unexpected text after the closing bracket: \"{text[end:].strip()[:30]}\"", number)
            return value
        if text[0] in "'\"":
            value, end = self.quoted(text, number)
            if text[end:].strip():
                raise YamlError("unexpected text after a quoted string", number)
            return value
        # A plain scalar may run on over more-indented lines.
        parts = [text]
        while True:
            row = self.lines.peek()
            if not row or row[1] <= indent or _split_key(row[2]) is not None or row[2].startswith("- "):
                break
            parts.append(self.lines.next()[2])
        return _plain(" ".join(parts))

    def quoted(self, text, number):
        quote = text[0]
        i = 1
        while True:
            end = text.find(quote, i)
            if end < 0:
                raise YamlError(f"a {'single' if quote == chr(39) else 'double'}-quoted string is not closed", number)
            if quote == "'" and text[end + 1:end + 2] == "'":
                i = end + 2
                continue
            if quote == '"':
                slashes = len(text[:end]) - len(text[:end].rstrip("\\"))
                if slashes % 2:
                    i = end + 1
                    continue
            break
        body = text[1:end]
        value = body.replace("''", "'") if quote == "'" else _double(body, number)
        return value, end + 1

    def block_text(self, header, indent, number):
        match = re.fullmatch(r"([|>])([-+]?)([1-9]?)([-+]?)", header)
        if not match:
            raise YamlError(f"bad block scalar header \"{header}\"", number)
        style, chomp = match.group(1), match.group(2) or match.group(4)
        # Block text keeps its lines as written, so it is read from the source
        # rather than the comment-stripped rows: a # inside it is text.
        raw, lines, last, body_indent = self.lines.raw, [], number, None
        for j in range(number, len(raw)):          # raw is 0-based; number is the header's line
            line = raw[j]
            if not line.strip():
                lines.append("")
                continue
            width = len(line) - len(line.lstrip(" "))
            if width <= indent or (body_indent is not None and width < body_indent):
                break
            if body_indent is None:
                body_indent = width
            lines.append(line[body_indent:])
            last = j + 1
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        while self.lines.peek() and self.lines.peek()[0] <= last:
            self.lines.next()
        if style == "|":
            text = "\n".join(lines)
        else:
            out, paragraph = [], []
            for line in lines:
                if line and not line.startswith(" "):
                    paragraph.append(line)
                    continue
                if paragraph:
                    out.append(" ".join(paragraph))
                    paragraph = []
                out.append(line)
            if paragraph:
                out.append(" ".join(paragraph))
            text = "\n".join(out)
        if not text:
            return ""
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (1 + trailing)
        return text + "\n"

    def flow(self, text, i, number):
        opener = text[i]
        closer = "]" if opener == "[" else "}"
        node = NodeList(number) if opener == "[" else NodeDict(number)
        i += 1
        while True:
            i = self.skip_space(text, i)
            if i >= len(text):
                raise YamlError(f"a flow {'list' if opener == '[' else 'mapping'} is not closed on this line", number)
            if text[i] == closer:
                return node, i + 1
            if opener == "[":
                value, i = self.flow_item(text, i, number, "],")
                node.append(value)
                node.lines.append(number)
            else:
                key, i = self.flow_item(text, i, number, ":,}")
                i = self.skip_space(text, i)
                value = None
                if i < len(text) and text[i] == ":":
                    value, i = self.flow_item(text, self.skip_space(text, i + 1), number, ",}")
                node[str(key)] = value
                node.lines[str(key)] = number
            i = self.skip_space(text, i)
            if i < len(text) and text[i] == ",":
                i += 1
            elif i < len(text) and text[i] != closer:
                raise YamlError(f"expected \",\" or \"{closer}\" in a flow collection", number)

    def flow_item(self, text, i, number, stops):
        i = self.skip_space(text, i)
        if i < len(text) and text[i] in "[{":
            return self.flow(text, i, number)
        if i < len(text) and text[i] in "'\"":
            value, end = self.quoted(text[i:], number)
            return value, i + end
        if i < len(text) and text[i] == "*":
            match = re.match(r"\*([^\s,\[\]{}]+)", text[i:])
            name = match.group(1)
            if name not in self.anchors:
                raise YamlError(f"*{name} refers to an anchor that is not defined above it", number)
            return self.anchors[name], i + match.end()
        start = i
        while i < len(text):
            ch = text[i]
            if ch in stops and (ch != ":" or i + 1 >= len(text) or text[i + 1] in " ,}"):
                break
            i += 1
        return _plain(text[start:i].strip()), i

    @staticmethod
    def skip_space(text, i):
        while i < len(text) and text[i] in " \t":
            i += 1
        return i


def loads(source):
    """Parse one YAML document. Raises YamlError with the line it is about."""
    return _Parser(source or "").document()


def untab(source):
    """The text with tabs in its indentation turned into spaces.

    YAML forbids tabs there, but a file pasted from an editor or a web page
    often has them. Each tab becomes one step of the file's own indentation -
    the smallest indent a space-indented line uses, else two spaces. Returns
    the new text, the step and the numbers of the lines changed. Only the
    indentation is touched, never what follows it.
    """
    lines = (source or "").split("\n")
    steps = [len(line) - len(line.lstrip(" ")) for line in lines
             if line.strip() and not line.lstrip(" ").startswith("\t")]
    width = min([n for n in steps if n] or [2])
    out, changed = [], []
    for number, line in enumerate(lines, 1):
        body = line.lstrip(" \t")
        lead = line[:len(line) - len(body)]
        if "\t" in lead:
            line = lead.replace("\t", " " * width) + body
            changed.append(number)
        out.append(line)
    return "\n".join(out), width, changed


def loads_untabbed(source):
    """Like loads, but a document indented with tabs is read as untab reads it.

    Returns the document and, when tabs were turned into spaces, what untab
    returned; a document without tabs in its indentation is read unchanged.
    """
    try:
        return loads(source), None
    except YamlError as error:
        if error.message != TABS:
            raise
    fixed = untab(source)
    return loads(fixed[0]), fixed


# ------------------------------------------------------------------ writing
_PLAIN = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./@+-]*(?: [A-Za-z0-9_./@+-]+)*$")
_SPECIAL = {"true", "false", "null", "yes", "no", "on", "off", "~", "y", "n"}


def _dump_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if (_PLAIN.match(text) and text.lower() not in _SPECIAL
            and not re.fullmatch(r"[-+]?(\d[\d_]*\.?\d*|\.\d+)([eE][-+]?\d+)?|0x[0-9a-fA-F]+|0o[0-7]+", text)):
        return text
    import json
    return json.dumps(text, ensure_ascii=False)


def _dump_block(text, pad):
    """A multi-line string as a literal block, which reads as the text itself -
    or quoted, where a block could not say it exactly (a first line that
    starts with a space, blank lines at the end, carriage returns)."""
    import json
    if text[:1] in (" ", "\t") or text.endswith("\n\n") or "\r" in text:
        return json.dumps(text, ensure_ascii=False)
    chomp = "" if text.endswith("\n") else "-"
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    body = "\n".join((pad + line) if line else "" for line in lines)
    return f"|{chomp}\n{body}"


def _dump(value, indent):
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, item in value.items():
            name = _dump_scalar(key)
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{name}:\n{_dump(item, indent + 1)}")
            elif isinstance(item, list) and item:
                lines.append(f"{pad}{name}:\n{_dump(item, indent)}")
            elif isinstance(item, str) and "\n" in item:
                lines.append(f"{pad}{name}: {_dump_block(item, pad + '  ')}")
            else:
                lines.append(f"{pad}{name}: {_dump(item, indent + 1) if isinstance(item, (dict, list)) else _dump_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                inner = _dump(item, indent + 1)
                lines.append(f"{pad}- {inner[len(pad) + 2:]}")
            elif isinstance(item, list) and item:
                # A list in a list: in flow style, which reads back plainly.
                import json
                lines.append(f"{pad}- {json.dumps(item, ensure_ascii=False)}")
            elif isinstance(item, str) and "\n" in item:
                lines.append(f"{pad}- {_dump_block(item, pad + '  ')}")
            else:
                lines.append(f"{pad}- {_dump(item, indent + 1) if isinstance(item, (dict, list)) else _dump_scalar(item)}")
        return "\n".join(lines)
    return _dump_scalar(value)


def dump(value):
    """YAML for plain data - maps, lists, strings, numbers, booleans, null - in
    the block style kubectl prints, and that loads() reads back to the same
    data."""
    return _dump(value, 0) + "\n"
