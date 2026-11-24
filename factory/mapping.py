"""Mapping language of a provider profile (docs/design/provider-agnostic.md section 4). Python >= 3.10, stdlib only.

A mapping is a JSON object whose leaves are expressions `{"op": args}`. The evaluator is pure and deterministic: no
I/O, no time, no random, no loops except `each` and `tree`. Any value that is not an operator object is a literal in
argument position (strings, numbers, booleans, null, lists of those).

Conventions (documented choices where the spec is silent):

* Unary operators propagate null: null in, null out. `exists`, `coalesce`, `any`, `all`, `not`, `eq`, `in`,
  `startswith`, `regex` are total and return booleans/values for null input.
* Truthiness (`any`, `all`, `not`, `each.where`): bool as is; null false; str/list/dict true when non-empty;
  numbers true when non-zero.
* `object` omits keys whose value is null and gives null when every value is null; `array` and `each` drop null items.
* `each` over null is null (an unknown), over an array is an array. Path wildcards over null are `[]` (section 4.2).
* A path projection (`a[*].b`) drops null results; `a[*]` alone returns the array as is. An index right after a
  projection picks from the projected array (`a[?k==v][0].x` = field x of the first match); deeper indexes apply
  per element.
* `coalesce` returns "" when every value is null or empty and at least one was an empty string.
* `concat` returns null when any part is null; numbers are written as decimal text.
* Regex guard: Python's `re` cannot be interrupted portably (it holds the interpreter lock, so a watchdog thread
  never gets to run), so the 1 s guard is: pattern length cap, input length cap (REGEX_MAX_INPUT), static rejection of
  patterns that nest an unbounded repeat inside another repeat (the classic catastrophic shape) and a post-hoc
  1 s wall-clock check per call that turns a slow match into an error.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
import time
import warnings
from html.parser import HTMLParser
from typing import Any

TREE_MAX_DEPTH = 64
TREE_MAX_NODES = 5000
REGEX_MAX_INPUT = 200_000
REGEX_MAX_PATTERN = 2_000
REGEX_BUDGET_S = 1.0

OPS = {
    "path", "lit", "coalesce", "any", "all", "not", "eq", "in", "exists",
    "startswith", "regex", "regex_group", "regex_sub", "lower", "upper", "trim", "join", "split", "concat",
    "to_int", "to_float", "to_bool", "date", "enum", "object", "array", "each",
    "html_to_md", "text_from_tree", "tree", "rgb_hex", "if", "type_is",
}
# op -> ("list", min, max) positional list, "expr" single expression, "dict" operator-specific object
ARITY: dict[str, tuple] = {
    "path": ("str",), "lit": ("any",), "coalesce": ("list", 1, None), "any": ("list", 1, None), "all": ("list", 1, None),
    "not": ("expr",), "eq": ("list", 2, 2), "in": ("list", 2, 2), "exists": ("expr",),
    "startswith": ("list", 2, 2), "regex": ("list", 2, 2), "regex_group": ("list", 3, 3), "regex_sub": ("list", 3, 3),
    "lower": ("expr",), "upper": ("expr",), "trim": ("expr",), "join": ("list", 2, 2), "split": ("list", 2, 2),
    "concat": ("list", 1, None), "to_int": ("expr",), "to_float": ("expr",), "to_bool": ("expr",),
    "date": ("dict", {"of"}, {"formats"}), "enum": ("dict", {"of", "map"}, {"default"}),
    "object": ("dict", None, None), "array": ("list", 0, None),
    "each": ("dict", {"over", "map"}, {"where", "flatten"}),
    "html_to_md": ("expr",), "text_from_tree": ("dict", {"of", "config"}, set()),
    "tree": ("dict", {"root", "children", "map"}, set()),
    "rgb_hex": ("dict", {"r", "g", "b"}, {"scale"}), "if": ("dict", {"cond", "then"}, {"else"}),
    "type_is": ("list", 2, 2),
}
TYPE_NAMES = ("array", "object", "string", "number", "bool", "null")
DATE_NAMED = {"iso", "epoch_s", "epoch_ms"}
TFT_KEYS = {"text_node", "text_key", "children_key", "block", "marks_key", "marks", "link", "mention", "unknown_node",
            "type_key", "level_key", "hard_break"}
TFT_BLOCKS = {"paragraph", "heading", "bullet_list", "ordered_list", "list_item", "code_block", "blockquote"}


class MappingError(Exception):
    """An expression failed (operator type error, unmapped enum, cap exceeded, bad path)."""

    def __init__(self, message: str, pointer: str = "", op: str = ""):
        super().__init__(message)
        self.message, self.pointer, self.op = message, pointer, op


class MappingFailure(Exception):
    """Several fields failed; `errors` has one line per failure."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------- paths (4.2) ----------

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _split_scope(path: str) -> tuple[str, str]:
    """(scope, rest). Scopes: main, ctx, name:<n>, item, parent."""
    if path.startswith("@@") and (len(path) == 2 or path[2] in ".["):
        return "parent", path[2:]
    if path.startswith("@") and (len(path) == 1 or path[1] in ".["):
        return "item", path[1:]
    if path.startswith("$"):
        m = _IDENT.match(path, 1)
        if m and (m.end() == len(path) or path[m.end()] in ".["):
            return ("ctx" if m.group(0) == "ctx" else f"name:{m.group(0)}"), path[m.end():]
        if len(path) == 1 or path[1] in ".[":
            return "main", path[1:]
    return "main", path


def _parse_lit(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        body = text[1:-1]
        if text[0] == '"':
            try:
                return json.loads(text)
            except ValueError:
                pass
        return body
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text in ("true", "false"):
        return text == "true"
    if text == "null":
        return None
    return text


def parse_path(path: str) -> tuple[str, list[tuple]]:
    """Parse a path into (scope, tokens). Tokens: ("key", name), ("idx", n), ("wild",), ("filter", key, "==|!=", lit)."""
    if not isinstance(path, str):
        raise MappingError(f"path must be a string, got {type(path).__name__}", op="path")
    scope, rest = _split_scope(path)
    toks: list[tuple] = []
    i, n = 0, len(rest)
    first = True
    while i < n:
        c = rest[i]
        if c == ".":
            i += 1
            if i >= n or rest[i] in ".[":
                raise MappingError(f"bad path {path!r}: empty key at {i}", op="path")
            continue
        if c == "[":
            j = i + 1
            if j < n and rest[j] in "\"'":
                q = rest[j]
                k = j + 1
                while k < n and rest[k] != q:
                    k += 2 if rest[k] == "\\" else 1
                if k >= n or k + 1 >= n or rest[k + 1] != "]":
                    raise MappingError(f"bad path {path!r}: unterminated quoted key", op="path")
                raw = rest[j:k + 1]
                try:
                    name = json.loads(raw) if q == '"' else raw[1:-1]
                except ValueError as err:
                    raise MappingError(f"bad path {path!r}: {err}", op="path") from err
                toks.append(("key", name))
                i = k + 2
                continue
            if j < n and rest[j] == "?":
                k = j + 1
                quote = ""
                while k < n and (rest[k] != "]" or quote):
                    if quote:
                        if rest[k] == quote:
                            quote = ""
                    elif rest[k] in "\"'":
                        quote = rest[k]
                    k += 1
                if k >= n:
                    raise MappingError(f"bad path {path!r}: unterminated filter", op="path")
                body = rest[j + 1:k]
                m = re.match(r"\s*([^=!\s]+)\s*(==|!=)(.*)$", body, re.S)
                if not m:
                    raise MappingError(f"bad path {path!r}: filter must be [?key==lit] or [?key!=lit]", op="path")
                toks.append(("filter", m.group(1), m.group(2), _parse_lit(m.group(3))))
                i = k + 1
                continue
            k = rest.find("]", j)
            if k < 0:
                raise MappingError(f"bad path {path!r}: missing ]", op="path")
            body = rest[j:k].strip()
            if body == "*":
                toks.append(("wild",))
            elif re.fullmatch(r"-?\d+", body):
                toks.append(("idx", int(body)))
            else:
                raise MappingError(f"bad path {path!r}: unsupported index [{body}]", op="path")
            i = k + 1
            continue
        k = i
        while k < n and rest[k] not in ".[":
            k += 1
        toks.append(("key", rest[i:k]))
        i = k
        first = False
    del first
    return scope, toks


def _kind(v: Any) -> str:
    return "null" if v is None else "bool" if isinstance(v, bool) else type(v).__name__


def _dotted(obj: Any, key: str) -> Any:
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _lit_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def apply_tokens(val: Any, toks: list[tuple], start: int = 0) -> Any:
    """Walk `toks[start:]` from `val`. Raises MappingError(type errors) without a pointer (callers add it)."""
    i = start
    while i < len(toks):
        t = toks[i]
        tag = t[0]
        if tag == "key":
            if val is None:
                return None
            if not isinstance(val, dict):
                raise MappingError(f"cannot read key {t[1]!r} of a {_kind(val)}", op="path")
            val = val.get(t[1])
        elif tag == "idx":
            if val is None:
                return None
            if not isinstance(val, list):
                raise MappingError(f"cannot index [{t[1]}] a {_kind(val)}", op="path")
            val = val[t[1]] if -len(val) <= t[1] < len(val) else None
        else:  # wild / filter
            if val is None:
                return []
            if not isinstance(val, list):
                raise MappingError(f"cannot apply [{'*' if tag == 'wild' else '?...'}] to a {_kind(val)}", op="path")
            if tag == "filter":
                keep = []
                for el in val:
                    if not isinstance(el, dict):
                        raise MappingError(f"filter over an array with a non-object element ({_kind(el)})", op="path")
                    hit = _lit_equal(_dotted(el, t[1]), t[3])
                    if hit == (t[2] == "=="):
                        keep.append(el)
                val = keep
            if i + 1 >= len(toks):
                return list(val)
            if toks[i + 1][0] == "idx":  # `[?k==v][0]`, `[*][-1]`: an index right after a projection picks from it
                j = toks[i + 1][1]
                picked = val[j] if -len(val) <= j < len(val) else None
                return apply_tokens(picked, toks, i + 2)
            out = [apply_tokens(el, toks, i + 1) for el in val]
            return [x for x in out if x is not None]
        i += 1
    return val


# ---------- regex guard ----------

try:  # the regex parser moved in 3.11
    import re._parser as _sre_parse  # type: ignore[import-not-found]
    _MAXREPEAT = _sre_parse.MAXREPEAT
except ImportError:  # pragma: no cover (3.10)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import sre_parse as _sre_parse  # type: ignore[no-redef]
    _MAXREPEAT = _sre_parse.MAXREPEAT


def _subs(name: str, av) -> list:
    """Sub-patterns (as lists of (op, av)) held by one parsed regex item."""
    if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
        return [av[2]]
    if name == "SUBPATTERN":
        return [av[-1]]
    if name == "BRANCH":
        return list(av[1])
    if name in ("ASSERT", "ASSERT_NOT"):
        return [av[-1]]
    if name == "ATOMIC_GROUP":
        return [av]
    if name == "GROUPREF_EXISTS":
        return [x for x in av[1:] if x is not None]
    return []


def _big_repeat(items) -> bool:
    """True when the items contain a repeat whose upper bound is above 1."""
    for op, av in items:
        name = str(op)
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") and av[1] > 1:
            return True
        if any(_big_repeat(s) for s in _subs(name, av)):
            return True
    return False


def _nested_repeat(items) -> bool:
    """True when a repeat with an upper bound > 1 contains another such repeat (e.g. `(a+)+`)."""
    for op, av in items:
        name = str(op)
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") and av[1] > 1 and _big_repeat(av[2]):
            return True
        if any(_nested_repeat(s) for s in _subs(name, av)):
            return True
    return False


def compile_regex(pattern: str, pointer: str = "", op: str = "regex") -> "re.Pattern[str]":
    if not isinstance(pattern, str):
        raise MappingError(f"regex pattern must be a string, got {_kind(pattern)}", pointer, op)
    if len(pattern) > REGEX_MAX_PATTERN:
        raise MappingError(f"regex pattern longer than {REGEX_MAX_PATTERN} characters", pointer, op)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = _sre_parse.parse(pattern)
    except re.error as err:
        raise MappingError(f"invalid regex {pattern[:80]!r}: {err}", pointer, op) from err
    if _nested_repeat(list(parsed)):
        raise MappingError(f"regex {pattern[:80]!r} nests an unbounded repeat inside a repeat (catastrophic backtracking "
                           "risk); bound the inner repeat or restructure", pointer, op)
    try:
        return re.compile(pattern)
    except re.error as err:
        raise MappingError(f"invalid regex {pattern[:80]!r}: {err}", pointer, op) from err


# ---------- rich text ----------

class _MdParser(HTMLParser):
    """HTML subset -> Markdown. Tags: p br h1-6 ul ol li a strong/b em/i code pre img blockquote table(-as-text)."""

    SKIP = {"script", "style", "head", "title"}
    TRANSPARENT = {"html", "body", "div", "span", "section", "article", "thead", "tbody", "tfoot", "figure",
                   "figcaption", "u", "small", "sub", "sup", "font", "center", "header", "footer", "main", "nav"}
    KNOWN = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a", "strong", "b", "em", "i", "code",
             "pre", "img", "blockquote", "table", "tr", "td", "th", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[list[str]] = [[]]
        self.lists: list[list] = []
        self.links: list[str] = []
        self.skip = 0
        self.pre = 0
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.unknown: set[str] = set()

    # buffer helpers
    def _w(self, text: str) -> None:
        if self.cell is not None:
            self.cell.append(text)
        else:
            self.stack[-1].append(text)

    def _block(self) -> None:
        self._w("\n\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag not in self.KNOWN and tag not in self.TRANSPARENT:
            self.unknown.add(tag)
        if tag in ("p",):
            self._block()
        elif tag == "br":
            self._w("\n")
        elif tag == "hr":
            self._w("\n\n---\n\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self._block()
            self._w("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._w("\n")
        elif tag == "li":
            depth = max(len(self.lists) - 1, 0)
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            self._w("\n" + "  " * depth + ("- " if kind[0] == "ul" else f"{kind[1]}. "))
        elif tag == "a":
            self.links.append(a.get("href") or "")
            self._w("[")
        elif tag in ("strong", "b"):
            self._w("**")
        elif tag in ("em", "i"):
            self._w("_")
        elif tag == "code":
            if not self.pre:
                self._w("`")
        elif tag == "pre":
            self.pre += 1
            self._w("\n\n```\n")
        elif tag == "img":
            self._w(f"![{a.get('alt') or ''}]({a.get('src') or ''})")
        elif tag == "blockquote":
            self.stack.append([])
        elif tag == "table":
            self.rows = []
        elif tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self.cell = []
            if not self.rows:
                self.rows.append([])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(self.skip - 1, 0)
            return
        if tag == "p" or re.fullmatch(r"h[1-6]", tag):
            self._block()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._w("\n")
        elif tag == "a":
            href = self.links.pop() if self.links else ""
            self._w(f"]({href})")
        elif tag in ("strong", "b"):
            self._w("**")
        elif tag in ("em", "i"):
            self._w("_")
        elif tag == "code":
            if not self.pre:
                self._w("`")
        elif tag == "pre":
            self.pre = max(self.pre - 1, 0)
            self._w("\n```\n\n")
        elif tag == "blockquote":
            if len(self.stack) > 1:
                body = "".join(self.stack.pop()).strip("\n")
                quoted = "\n".join(("> " + ln) if ln.strip() else ">" for ln in body.split("\n"))
                self._w("\n\n" + quoted + "\n\n")
        elif tag in ("td", "th"):
            if self.cell is not None:
                text = re.sub(r"\s+", " ", "".join(self.cell)).strip()
                self.cell = None
                self.rows[-1].append(text)
        elif tag == "table":
            lines = [" | ".join(r) for r in self.rows if r]
            self.rows = []
            self._w("\n\n" + "\n".join(lines) + "\n\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self._w(data)
        else:
            self._w(re.sub(r"\s+", " ", data))


def html_to_md(source: str) -> tuple[str, list[str]]:
    """Markdown text and the sorted list of tag names outside the supported set."""
    parser = _MdParser()
    parser.feed(source)
    parser.close()
    while len(parser.stack) > 1:  # unterminated blockquote
        parser.stack[-2].extend(parser.stack.pop())
    text = "".join(parser.stack[0])
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), sorted(parser.unknown)


# ---------- evaluator ----------

class Evaluator:
    """Evaluates expressions against named raw documents and an ingest context."""

    def __init__(self, docs: dict[str, Any], ctx: dict[str, Any] | None = None):
        self.docs = docs
        self.ctx = ctx or {}
        self.items: list[Any] = []
        self.unknowns: list[dict] = []
        self.field = ""
        self.nodes = 0

    # ----- errors -----
    def fail(self, op: str, msg: str, ptr: str, value: Any = None, show: bool = True) -> MappingError:
        text = f"{op}: {msg}"
        if show:
            shown = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
            text += f" (input {shown[:80]!r})"
        return MappingError(f"{text} at {ptr or '/'}", ptr, op)

    def unknown(self, reason: str) -> None:
        rec = {"field": self.field, "reason": reason}
        if rec not in self.unknowns:
            self.unknowns.append(rec)

    # ----- core -----
    def ev(self, e: Any, ptr: str) -> Any:
        if isinstance(e, dict):
            if len(e) != 1:
                raise self.fail("expression", f"an expression is an object with exactly one operator, got keys {sorted(e)}", ptr, show=False)
            op, arg = next(iter(e.items()))
            if op not in OPS:
                raise self.fail("expression", f"unknown operator {op!r}", ptr, show=False)
            return getattr(self, "op_if_" if op == "if" else "op_" + op)(arg, f"{ptr}/{op}")
        if isinstance(e, list):
            return [self.ev(x, f"{ptr}/{i}") for i, x in enumerate(e)]
        return e

    def lst(self, op: str, arg: Any, ptr: str, lo: int, hi: int | None) -> list:
        if not isinstance(arg, list) or len(arg) < lo or (hi is not None and len(arg) > hi):
            want = f"{lo}" if hi == lo else f"{lo}+" if hi is None else f"{lo}-{hi}"
            raise self.fail(op, f"expects a list of {want} arguments", ptr, show=False)
        return arg

    def s(self, op: str, v: Any, ptr: str) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise self.fail(op, f"expected a string, got {_kind(v)}", ptr, v)
        return v

    @staticmethod
    def truthy(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, (int, float)):
            return v != 0
        return len(v) > 0

    # ----- value -----
    def op_path(self, arg, ptr):
        return self.resolve(arg, ptr)

    def resolve(self, path: str, ptr: str, base: Any = None, use_base: bool = False) -> Any:
        try:
            scope, toks = parse_path(path)
            if use_base and scope == "main":
                root = base
            elif scope == "main":
                root = self.docs.get("main")
            elif scope == "ctx":
                root = self.ctx
            elif scope.startswith("name:"):
                name = scope[5:]
                if name not in self.docs:
                    raise MappingError(f"no additional raw named {name!r} (pass --raw {name}=path)", op="path")
                root = self.docs[name]
            elif scope == "item":
                if not self.items:
                    raise MappingError("@ used outside each/tree", op="path")
                root = self.items[-1]
            else:
                if len(self.items) < 2:
                    raise MappingError("@@ used outside a nested each/tree", op="path")
                root = self.items[-2]
            return apply_tokens(root, toks)
        except MappingError as err:
            if err.pointer:
                raise
            raise self.fail("path", err.message, ptr, path) from None

    def op_lit(self, arg, ptr):
        return arg

    def op_coalesce(self, arg, ptr):
        empty = False
        for i, x in enumerate(self.lst("coalesce", arg, ptr, 1, None)):
            v = self.ev(x, f"{ptr}/{i}")
            if v is not None and v != "":
                return v
            empty = empty or v == ""
        return "" if empty else None  # `[x, {"lit": ""}]` is the way to default a text field to empty

    # ----- boolean -----
    def op_any(self, arg, ptr):
        for i, x in enumerate(self.lst("any", arg, ptr, 1, None)):
            if self.truthy(self.ev(x, f"{ptr}/{i}")):
                return True
        return False

    def op_all(self, arg, ptr):
        for i, x in enumerate(self.lst("all", arg, ptr, 1, None)):
            if not self.truthy(self.ev(x, f"{ptr}/{i}")):
                return False
        return True

    def op_not(self, arg, ptr):
        return not self.truthy(self.ev(arg, ptr))

    def op_eq(self, arg, ptr):
        a, b = (self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst("eq", arg, ptr, 2, 2)))
        return _lit_equal(a, b)

    def op_in(self, arg, ptr):
        a, b = (self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst("in", arg, ptr, 2, 2)))
        if b is None:
            return False
        if not isinstance(b, list):
            raise self.fail("in", f"second argument must be a list, got {_kind(b)}", ptr, b)
        return any(_lit_equal(a, x) for x in b)

    def op_exists(self, arg, ptr):
        return self.ev(arg, ptr) is not None

    # ----- string -----
    def op_startswith(self, arg, ptr):
        s, p = (self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst("startswith", arg, ptr, 2, 2)))
        s, p = self.s("startswith", s, ptr), self.s("startswith", p, ptr)
        return bool(s is not None and p is not None and s.startswith(p))

    def _rx(self, op: str, arg: Any, ptr: str, n: int):
        parts = [self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst(op, arg, ptr, n, n))]
        s = self.s(op, parts[0], ptr)
        pat = parts[1]
        if pat is None:
            raise self.fail(op, "pattern is null", ptr, show=False)
        try:
            rx = compile_regex(pat, ptr, op)
        except MappingError as err:
            raise self.fail(op, err.message, ptr, show=False) from None
        if s is not None and len(s) > REGEX_MAX_INPUT:
            raise self.fail(op, f"input longer than {REGEX_MAX_INPUT} characters", ptr, s)
        return s, rx, parts

    def _timed(self, op: str, ptr: str, started: float) -> None:
        if time.monotonic() - started > REGEX_BUDGET_S:
            raise self.fail(op, f"regex exceeded the {REGEX_BUDGET_S:g}s guard", ptr, show=False)

    def op_regex(self, arg, ptr):
        s, rx, _ = self._rx("regex", arg, ptr, 2)
        if s is None:
            return False
        t0 = time.monotonic()
        hit = rx.search(s) is not None
        self._timed("regex", ptr, t0)
        return hit

    def op_regex_group(self, arg, ptr):
        s, rx, parts = self._rx("regex_group", arg, ptr, 3)
        if s is None:
            return None
        t0 = time.monotonic()
        m = rx.search(s)
        self._timed("regex_group", ptr, t0)
        if not m:
            return None
        g = parts[2]
        try:
            return m.group(g)
        except (IndexError, re.error) as err:
            raise self.fail("regex_group", f"no group {g!r}: {err}", ptr, show=False) from None

    def op_regex_sub(self, arg, ptr):
        s, rx, parts = self._rx("regex_sub", arg, ptr, 3)
        if s is None:
            return None
        repl = self.s("regex_sub", parts[2], ptr) or ""
        t0 = time.monotonic()
        try:
            out = rx.sub(repl, s)
        except (re.error, IndexError) as err:
            raise self.fail("regex_sub", f"bad replacement {repl!r}: {err}", ptr, show=False) from None
        self._timed("regex_sub", ptr, t0)
        return out

    def _unary_str(self, op: str, arg, ptr, fn):
        s = self.s(op, self.ev(arg, ptr), ptr)
        return None if s is None else fn(s)

    def op_lower(self, arg, ptr):
        return self._unary_str("lower", arg, ptr, str.lower)

    def op_upper(self, arg, ptr):
        return self._unary_str("upper", arg, ptr, str.upper)

    def op_trim(self, arg, ptr):
        return self._unary_str("trim", arg, ptr, str.strip)

    def op_join(self, arg, ptr):
        a, sep = (self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst("join", arg, ptr, 2, 2)))
        if a is None:
            return None
        if not isinstance(a, list):
            raise self.fail("join", f"first argument must be an array, got {_kind(a)}", ptr, a)
        sep = self.s("join", sep, ptr)
        if sep is None:
            raise self.fail("join", "separator is null", ptr, show=False)
        parts = []
        for x in a:
            if x is None:
                continue
            if isinstance(x, bool) or not isinstance(x, (str, int, float)):
                raise self.fail("join", f"array element must be text or number, got {_kind(x)}", ptr, x)
            parts.append(str(x))
        return sep.join(parts)

    def op_split(self, arg, ptr):
        s, sep = (self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst("split", arg, ptr, 2, 2)))
        s, sep = self.s("split", s, ptr), self.s("split", sep, ptr)
        if s is None:
            return None
        if not sep:
            raise self.fail("split", "separator must be a non-empty string", ptr, show=False)
        return s.split(sep)

    def op_concat(self, arg, ptr):
        out = []
        for i, x in enumerate(self.lst("concat", arg, ptr, 1, None)):
            v = self.ev(x, f"{ptr}/{i}")
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, (str, int, float)):
                raise self.fail("concat", f"part must be text or number, got {_kind(v)}", ptr, v)
            out.append(str(v))
        return "".join(out)

    # ----- convert -----
    def op_to_int(self, arg, ptr):
        v = self.ev(arg, ptr)
        if v is None:
            return None
        if isinstance(v, bool):
            raise self.fail("to_int", "bool is not a number", ptr, v)
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            if v == int(v):
                return int(v)
            raise self.fail("to_int", "not an integer", ptr, v)
        if isinstance(v, str) and re.fullmatch(r"\s*[-+]?\d+\s*", v):
            return int(v)
        raise self.fail("to_int", "not convertible", ptr, v)

    def op_to_float(self, arg, ptr):
        v = self.ev(arg, ptr)
        if v is None:
            return None
        if isinstance(v, bool):
            raise self.fail("to_float", "bool is not a number", ptr, v)
        if isinstance(v, (int, float)):
            return float(v) if isinstance(v, float) else v
        if isinstance(v, str):
            try:
                return float(v.strip())
            except ValueError:
                pass
        raise self.fail("to_float", "not convertible", ptr, v)

    def op_to_bool(self, arg, ptr):
        v = self.ev(arg, ptr)
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, int) and v in (0, 1):
            return bool(v)
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        raise self.fail("to_bool", "expected a bool, 0/1 or \"true\"/\"false\"", ptr, v)

    def op_date(self, arg, ptr):
        d = self.obj("date", arg, ptr)
        v = self.ev(d["of"], f"{ptr}/of") if "of" in d else None
        if v is None:
            return None
        formats = d.get("formats") or ["iso"]
        if not isinstance(formats, list) or not formats or not all(isinstance(f, str) for f in formats):
            raise self.fail("date", "formats must be a non-empty list of strings", ptr, show=False)
        for f in formats:
            got = _parse_date(v, f)
            if got is not None:
                return got.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raise self.fail("date", f"matches none of {formats}", ptr, v)

    def obj(self, op: str, arg: Any, ptr: str) -> dict:
        if not isinstance(arg, dict):
            raise self.fail(op, "expects an object argument", ptr, show=False)
        return arg

    # ----- enum -----
    def op_enum(self, arg, ptr):
        d = self.obj("enum", arg, ptr)
        if "of" not in d or not isinstance(d.get("map"), dict):
            raise self.fail("enum", "needs `of` and a `map` object", ptr, show=False)
        v = self.ev(d["of"], f"{ptr}/of")
        if v is None:
            return None
        key = ("true" if v else "false") if isinstance(v, bool) else str(v) if isinstance(v, (str, int, float)) else None
        if key is not None and key in d["map"]:
            return d["map"][key]
        if "default" in d:
            return d["default"]
        raise self.fail("enum", f"unmapped value (known: {sorted(d['map'])[:8]})", ptr, v)

    # ----- structure -----
    def op_object(self, arg, ptr):
        d = self.obj("object", arg, ptr)
        out = {}
        for k, e in d.items():
            v = self.ev(e, f"{ptr}/{k}")
            if v is not None:
                out[k] = v
        return out or None

    def op_array(self, arg, ptr):
        out = []
        for i, x in enumerate(self.lst("array", arg, ptr, 0, None)):
            v = self.ev(x, f"{ptr}/{i}")
            if v is not None:
                out.append(v)
        return out

    def op_each(self, arg, ptr):
        d = self.obj("each", arg, ptr)
        if "over" not in d or "map" not in d:
            raise self.fail("each", "needs `over` and `map`", ptr, show=False)
        over = self.ev(d["over"], f"{ptr}/over")
        if over is None:
            return None
        if not isinstance(over, list):
            raise self.fail("each", f"`over` must be an array, got {_kind(over)}", ptr, over)
        flat = bool(d.get("flatten"))
        out: list = []
        for i, item in enumerate(over):
            self.items.append(item)
            try:
                if "where" in d and not self.truthy(self.ev(d["where"], f"{ptr}/where")):
                    continue
                v = self.ev(d["map"], f"{ptr}/map")
            finally:
                self.items.pop()
            if v is None:
                continue
            if flat:
                if not isinstance(v, list):
                    raise self.fail("each", f"flatten needs array results, item {i} gave {_kind(v)}", ptr, v)
                out.extend(x for x in v if x is not None)
            else:
                out.append(v)
        return out

    # ----- conditional, type, color -----
    def op_if_(self, arg, ptr):
        d = self.obj("if", arg, ptr)
        if "cond" not in d or "then" not in d:
            raise self.fail("if", "needs `cond` and `then`", ptr, show=False)
        if self.truthy(self.ev(d["cond"], f"{ptr}/cond")):
            return self.ev(d["then"], f"{ptr}/then")
        return self.ev(d["else"], f"{ptr}/else") if "else" in d else None

    def op_type_is(self, arg, ptr):
        v, t = (self.ev(x, f"{ptr}/{i}") for i, x in enumerate(self.lst("type_is", arg, ptr, 2, 2)))
        if t not in TYPE_NAMES:
            raise self.fail("type_is", f"type must be one of {list(TYPE_NAMES)}", ptr, t)
        got = ("null" if v is None else "bool" if isinstance(v, bool) else "number" if isinstance(v, (int, float))
               else "string" if isinstance(v, str) else "array" if isinstance(v, list) else "object")
        return got == t

    def op_rgb_hex(self, arg, ptr):
        d = self.obj("rgb_hex", arg, ptr)
        if not {"r", "g", "b"} <= set(d):
            raise self.fail("rgb_hex", "needs `r`, `g` and `b`", ptr, show=False)
        scale = self.ev(d["scale"], f"{ptr}/scale") if "scale" in d else 1
        if scale not in (1, 255) or isinstance(scale, bool):
            raise self.fail("rgb_hex", "scale must be 1 or 255", ptr, scale)
        chans = []
        for k in ("r", "g", "b"):
            v = self.ev(d[k], f"{ptr}/{k}")
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise self.fail("rgb_hex", f"channel {k} must be a number, got {_kind(v)}", ptr, v)
            chans.append(max(0, min(255, int(round(v * 255 if scale == 1 else v)))))
        return "#%02x%02x%02x" % tuple(chans)

    # ----- rich text -----
    def op_html_to_md(self, arg, ptr):
        v = self.s("html_to_md", self.ev(arg, ptr), ptr)
        if v is None:
            return None
        if len(v) > 2_000_000:
            raise self.fail("html_to_md", "input too large", ptr, show=False)
        text, unknown = html_to_md(v)
        for tag in unknown:
            self.unknown(f"html tag <{tag}> rendered as text")
        return text

    def op_text_from_tree(self, arg, ptr):
        d = self.obj("text_from_tree", arg, ptr)
        if "of" not in d or not isinstance(d.get("config"), dict):
            raise self.fail("text_from_tree", "needs `of` and a `config` object", ptr, show=False)
        cfg = d["config"]
        bad = sorted(set(cfg) - TFT_KEYS)
        if bad:
            raise self.fail("text_from_tree", f"unknown config keys {bad}", ptr, show=False)
        root = self.ev(d["of"], f"{ptr}/of")
        if root is None:
            return None
        return _TreeText(self, cfg, ptr).render_root(root)

    def op_tree(self, arg, ptr):
        d = self.obj("tree", arg, ptr)
        if not all(k in d for k in ("root", "children", "map")) or not isinstance(d["children"], str):
            raise self.fail("tree", "needs `root`, `children` (a path string) and `map`", ptr, show=False)
        root = self.ev(d["root"], f"{ptr}/root")
        if root is None:
            return None
        count = [0]
        toks = parse_path(d["children"])[1]

        def build(raw: Any, depth: int) -> Any:
            if depth > TREE_MAX_DEPTH:
                raise self.fail("tree", f"depth cap {TREE_MAX_DEPTH} exceeded", ptr, show=False)
            count[0] += 1
            if count[0] > TREE_MAX_NODES:
                raise self.fail("tree", f"node cap {TREE_MAX_NODES} exceeded", ptr, show=False)
            self.items.append(raw)
            try:
                node = self.ev(d["map"], f"{ptr}/map")
                if not isinstance(node, dict):
                    raise self.fail("tree", f"`map` must produce an object, got {_kind(node)}", ptr, node)
                try:
                    kids = apply_tokens(raw, toks)
                except MappingError as err:
                    raise self.fail("tree", err.message, ptr, show=False) from None
                if kids is not None and not isinstance(kids, list):
                    raise self.fail("tree", f"children must be an array, got {_kind(kids)}", ptr, kids)
                built = [build(k, depth + 1) for k in kids or []]
            finally:
                self.items.pop()
            if built:
                node = dict(node)
                node["children"] = built
            return node

        return build(root, 1)


def _parse_date(v: Any, fmt: str) -> _dt.datetime | None:
    try:
        if fmt == "iso":
            if not isinstance(v, str):
                return None
            text = v.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            got = _dt.datetime.fromisoformat(text)
        elif fmt in ("epoch_s", "epoch_ms"):
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                return None
            num = float(v)
            got = _dt.datetime.fromtimestamp(num / (1000 if fmt == "epoch_ms" else 1), _dt.timezone.utc)
        else:
            if not isinstance(v, str):
                return None
            got = _dt.datetime.strptime(v.strip(), fmt)
    except (ValueError, OverflowError, OSError):
        return None
    return got if got.tzinfo else got.replace(tzinfo=_dt.timezone.utc)


class _TreeText:
    """`text_from_tree`: rich-text tree -> Markdown with node-type names taken from the profile config."""

    def __init__(self, ev: Evaluator, cfg: dict, ptr: str):
        self.ev, self.cfg, self.ptr = ev, cfg, ptr
        self.type_key = cfg.get("type_key", "type")
        self.children_key = cfg.get("children_key", "content")
        self.text_node = cfg.get("text_node", "text")
        self.text_key = cfg.get("text_key", "text")
        self.marks_key = cfg.get("marks_key", "marks")
        self.marks = cfg.get("marks") or {}
        self.link = cfg.get("link") or {}
        self.mention = cfg.get("mention") or {}
        self.hard_break = cfg.get("hard_break")
        blocks = cfg.get("block") or {}
        self.block = {v: k for k, v in blocks.items() if k in TFT_BLOCKS}
        self.level_key = cfg.get("level_key", "attrs.level")
        self.mode = cfg.get("unknown_node", "descend")
        self.count = 0

    def err(self, msg: str, val: Any = None) -> MappingError:
        return self.ev.fail("text_from_tree", msg, self.ptr, val, show=val is not None)

    def kids(self, node: dict) -> list:
        k = _dotted(node, self.children_key)
        if k is None:
            return []
        if not isinstance(k, list):
            raise self.err(f"`{self.children_key}` must be an array", k)
        return k

    def render_root(self, root: Any) -> str:
        nodes = root if isinstance(root, list) else [root]
        parts: list[tuple[bool, str]] = []
        for n in nodes:
            if isinstance(n, dict) and n.get(self.type_key) not in self.block and not self.is_inline(n):
                parts += self.pieces(self.kids(n))  # the document root: descended without an unknown entry
            else:
                parts += self.pieces([n])
        return self.join(parts)

    def is_inline(self, n: dict) -> bool:
        t = n.get(self.type_key)
        return t == self.text_node or (bool(self.link) and t == self.link.get("type")) or \
            (bool(self.mention) and t == self.mention.get("type")) or (bool(self.hard_break) and t == self.hard_break)

    def pieces(self, nodes: list) -> list[tuple[bool, str]]:
        out: list[tuple[bool, str]] = []
        for n in nodes:
            self.count += 1
            if self.count > TREE_MAX_NODES:
                raise self.err(f"node cap {TREE_MAX_NODES} exceeded")
            if not isinstance(n, dict):
                raise self.err("tree node must be an object", n)
            out.append(self.one(n))
        return out

    @staticmethod
    def join(parts: list[tuple[bool, str]], sep: str = "\n\n") -> str:
        groups: list[str] = []
        cur: list[str] = []
        for inline, text in parts:
            if inline:
                cur.append(text)
            else:
                if cur:
                    groups.append("".join(cur))
                    cur = []
                groups.append(text)
        if cur:
            groups.append("".join(cur))
        return sep.join(g for g in groups if g.strip() != "")

    def inline_text(self, nodes: list) -> str:
        return self.join(self.pieces(nodes))

    def one(self, n: dict) -> tuple[bool, str]:
        t = n.get(self.type_key)
        if t == self.text_node:
            text = _dotted(n, self.text_key)
            text = "" if text is None else str(text)
            for mark in n.get(self.marks_key) or []:
                mt = mark.get(self.type_key) if isinstance(mark, dict) else mark
                tok = self.marks.get(mt)
                if tok is None:
                    self.ev.unknown(f"rich-text mark {mt!r} ignored")
                elif text.strip():
                    text = f"{tok}{text}{tok}" if tok else text
            return True, text
        if self.link and t == self.link.get("type"):
            label = self.inline_text(self.kids(n))
            url = _dotted(n, self.link.get("url_key", "attrs.href"))
            return True, f"[{label}]({url})" if url else label
        if self.mention and t == self.mention.get("type"):
            text = _dotted(n, self.mention.get("text_key", "attrs.text"))
            return True, "" if text is None else str(text)
        if self.hard_break and t == self.hard_break:
            return True, "\n"
        sem = self.block.get(t)
        if sem == "paragraph":
            return False, self.inline_text(self.kids(n))
        if sem == "heading":
            lvl = _dotted(n, self.level_key)
            lvl = lvl if isinstance(lvl, int) and not isinstance(lvl, bool) and 1 <= lvl <= 6 else 1
            return False, "#" * lvl + " " + self.inline_text(self.kids(n))
        if sem in ("bullet_list", "ordered_list"):
            return False, self.list_text(n, sem == "ordered_list")
        if sem == "list_item":
            return False, self.join(self.pieces(self.kids(n)), "\n")
        if sem == "code_block":
            body = "".join(str(_dotted(k, self.text_key) or "") for k in self.kids(n) if isinstance(k, dict))
            return False, f"```\n{body}\n```"
        if sem == "blockquote":
            body = self.join(self.pieces(self.kids(n)))
            return False, "\n".join(("> " + ln) if ln.strip() else ">" for ln in body.split("\n"))
        # not in the config
        if self.mode == "error":
            raise self.err(f"node type {t!r} is not in the config", t)
        self.ev.unknown(f"rich-text node type {t!r} descended without config")
        return False, self.join(self.pieces(self.kids(n)))

    def list_text(self, n: dict, ordered: bool) -> str:
        lines: list[str] = []
        for i, item in enumerate(self.kids(n), start=1):
            if not isinstance(item, dict):
                raise self.err("list item must be an object", item)
            self.count += 1
            body = self.one(item)[1] if self.block.get(item.get(self.type_key)) == "list_item" else self.inline_text([item])
            body_lines = body.split("\n")
            mark = f"{i}. " if ordered else "- "
            lines.append(mark + body_lines[0])
            lines.extend(("  " * (1 if not ordered else 1) + ln) if ln else ln for ln in body_lines[1:])
        return "\n".join(lines)


# ---------- public API ----------

def evaluate(expr: Any, docs: dict[str, Any], ctx: dict[str, Any] | None = None, pointer: str = "") -> tuple[Any, list[dict]]:
    """Evaluate one expression. Returns (value, unknowns). Raises MappingError."""
    ev = Evaluator(docs, ctx)
    return ev.ev(expr, pointer), ev.unknowns


def evaluate_map(mapping: dict, docs: dict[str, Any], ctx: dict[str, Any] | None = None,
                 pointer: str = "/map") -> tuple[dict, list[dict], list[str]]:
    """Evaluate every top-level field of a `map` object. Returns (values, unknowns, errors); a field that errors is
    absent from `values` and reported in `errors` (one line each, with its JSON pointer). `unknowns` holds rich-text
    notes only; null fields are the caller's business (it knows which are required)."""
    ev = Evaluator(docs, ctx)
    values: dict[str, Any] = {}
    errors: list[str] = []
    for field, expr in mapping.items():
        ev.field = field
        try:
            values[field] = ev.ev(expr, f"{pointer}/{field}")
        except MappingError as err:
            errors.append(err.message)
    return values, ev.unknowns, errors


def source_expression(expr: Any, limit: int = 160) -> str:
    text = json.dumps(expr, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---------- lint ----------

def lint_expr(expr: Any, ptr: str = "") -> list[str]:
    """Static problems of an expression: unknown operators, wrong argument shapes, regex that do not compile or are
    unsafe, bad paths, bad enum/date/text_from_tree config."""
    out: list[str] = []

    def walk(e: Any, p: str) -> None:
        if isinstance(e, list):
            for i, x in enumerate(e):
                walk(x, f"{p}/{i}")
            return
        if not isinstance(e, dict):
            return
        if len(e) != 1:
            out.append(f"{p or '/'}: an expression is an object with exactly one operator, got keys {sorted(e)}")
            return
        op, arg = next(iter(e.items()))
        q = f"{p}/{op}"
        if op not in OPS:
            out.append(f"{q}: unknown operator {op!r}")
            return
        shape = ARITY[op]
        if shape[0] == "str":
            try:
                parse_path(arg)
            except MappingError as err:
                out.append(f"{q}: {err.message}")
        elif shape[0] == "list":
            if not isinstance(arg, list) or len(arg) < shape[1] or (shape[2] is not None and len(arg) > shape[2]):
                out.append(f"{q}: expects a list of {shape[1]}{'+' if shape[2] is None else '' if shape[1] == shape[2] else '-' + str(shape[2])} arguments")
                return
            if op in ("regex", "regex_group", "regex_sub"):
                pat = arg[1]
                if isinstance(pat, str):
                    try:
                        compile_regex(pat, q, op)
                    except MappingError as err:
                        out.append(f"{q}: {err.message}")
                elif not isinstance(pat, dict):
                    out.append(f"{q}: pattern must be a string")
            if op == "type_is" and isinstance(arg[1], str) and arg[1] not in TYPE_NAMES:
                out.append(f"{q}: type must be one of {list(TYPE_NAMES)}")
            for i, x in enumerate(arg):
                walk(x, f"{q}/{i}")
        elif shape[0] == "expr":
            walk(arg, q)
        elif shape[0] == "dict":
            if not isinstance(arg, dict):
                out.append(f"{q}: expects an object")
                return
            req, opt = shape[1], shape[2]
            if req is not None:
                miss = sorted(req - set(arg))
                extra = sorted(set(arg) - req - (opt or set()))
                if miss:
                    out.append(f"{q}: missing {miss}")
                if extra:
                    out.append(f"{q}: unexpected keys {extra}")
            if op == "rgb_hex" and "scale" in arg and arg["scale"] not in (1, 255):
                out.append(f"{q}: scale must be 1 or 255")
            if op == "enum" and not isinstance(arg.get("map"), dict):
                out.append(f"{q}: map must be an object")
            if op == "date":
                for f in arg.get("formats") or []:
                    if not isinstance(f, str):
                        out.append(f"{q}: formats must be strings")
            if op == "text_from_tree" and isinstance(arg.get("config"), dict):
                bad = sorted(set(arg["config"]) - TFT_KEYS)
                if bad:
                    out.append(f"{q}: unknown config keys {bad}")
                if arg["config"].get("unknown_node", "descend") not in ("descend", "error"):
                    out.append(f"{q}: unknown_node must be descend or error")
            if op == "tree":
                if not isinstance(arg.get("children"), str):
                    out.append(f"{q}: children must be a path string")
                else:
                    try:
                        parse_path(arg["children"])
                    except MappingError as err:
                        out.append(f"{q}/children: {err.message}")
            for k, v in arg.items():
                if op in ("rgb_hex", "if") or op == "object" or k in ("of", "over", "map", "where", "root") and op in ("each", "tree", "date", "enum", "text_from_tree"):
                    if op == "enum" and k == "map":
                        continue
                    walk(v, f"{q}/{k}")
        return

    walk(expr, ptr)
    return out
