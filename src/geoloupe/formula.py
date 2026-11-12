"""A small, safe formula language for error maps (``abs(P - T)``, ``where(T >= h, 1, 0)``).

The same language is implemented in the web client (frontend/src/lib/formula.ts); both pass the
shared cases in tests/formula_cases.json, so a map computed in the browser and on the server agree.

Grammar (lowest precedence first)::

    or      := and ("or" and)*
    and     := not ("and" not)*
    not     := "not" not | compare
    compare := sum (("<" | "<=" | ">" | ">=" | "==" | "!=") sum)?     (no chains)
    sum     := product (("+" | "-") product)*
    product := unary (("*" | "/") unary)*
    unary   := ("-" | "+") unary | power
    power   := atom (("^" | "**") unary)?                             (right-associative)
    atom    := number | name | name "(" args ")" | "(" or ")"

Rules that keep missing data missing: a comparison, ``and``, ``or``, ``not`` or ``where`` with a
missing (NaN) input gives NaN, never 0 or 1; any result that is not finite (division by zero,
``log(0)``) becomes NaN. Nothing is evaluated with Python's ``eval``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

MAX_LENGTH = 500
MAX_DEPTH = 40
CONSTANTS = {"nan": float("nan"), "pi": float(np.pi)}
# name: (min arguments, max arguments or None for any number)
FUNCTIONS: dict[str, tuple[int, int | None]] = {
    "abs": (1, 1),
    "sqrt": (1, 1),
    "log": (1, 1),
    "log10": (1, 1),
    "exp": (1, 1),
    "sign": (1, 1),
    "isnan": (1, 1),
    "where": (3, 3),
    "min": (2, None),
    "max": (2, None),
}
COMPARE = ("<=", ">=", "==", "!=", "<", ">")
KEYWORDS = ("and", "or", "not")


class FormulaError(ValueError):
    """A formula that cannot be read; ``pos`` is the 0-based character position (or None)."""

    def __init__(self, message: str, pos: int | None = None):
        super().__init__(message if pos is None else f"{message} (at character {pos + 1})")
        self.message = message
        self.pos = pos


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Name:
    name: str
    pos: int


@dataclass(frozen=True)
class Unary:
    op: str  # "-", "+", "not"
    arg: Node


@dataclass(frozen=True)
class Binary:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple[Node, ...]
    pos: int


Node = Num | Name | Unary | Binary | Call

_TOKEN = re.compile(
    r"\s*(?:(?P<num>(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>\*\*|<=|>=|==|!=|[-+*/^(),<>]))"
)


def tokenize(text: str) -> list[tuple[str, str, int]]:
    """[(kind, text, position)], kind = num | name | op | end."""
    out: list[tuple[str, str, int]] = []
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        m = _TOKEN.match(text, i)
        if not m or m.lastgroup is None:
            raise FormulaError(f"unexpected character '{text[i]}'", i)
        kind = m.lastgroup
        start = m.start(kind)
        out.append((kind, m.group(kind), start))
        i = m.end()
    out.append(("end", "", len(text)))
    return out


class _Parser:
    def __init__(self, text: str):
        self.tokens = tokenize(text)
        self.i = 0
        self.depth = 0

    def peek(self) -> tuple[str, str, int]:
        return self.tokens[self.i]

    def take(self) -> tuple[str, str, int]:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def kw(self, word: str) -> bool:
        kind, text, _ = self.peek()
        return kind == "name" and text == word

    def op(self, *texts: str) -> bool:
        kind, text, _ = self.peek()
        return kind == "op" and text in texts

    def expect(self, text: str) -> None:
        if not self.op(text):
            raise self.unexpected(f"expected '{text}'")
        self.take()

    def unexpected(self, hint: str = "") -> FormulaError:
        kind, text, pos = self.peek()
        if kind == "end":
            return FormulaError("unexpected end of formula" + (f": {hint}" if hint else ""), pos)
        return FormulaError(f"unexpected '{text}'" + (f": {hint}" if hint else ""), pos)

    def nest(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FormulaError("formula is nested too deeply", self.peek()[2])

    def parse(self) -> Node:
        node = self.or_()
        if self.peek()[0] != "end":
            raise self.unexpected()
        return node

    def or_(self) -> Node:
        node = self.and_()
        while self.kw("or"):
            self.take()
            node = Binary("or", node, self.and_())
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.kw("and"):
            self.take()
            node = Binary("and", node, self.not_())
        return node

    def not_(self) -> Node:
        if self.kw("not"):
            self.take()
            self.nest()
            node = Unary("not", self.not_())
            self.depth -= 1
            return node
        return self.compare()

    def compare(self) -> Node:
        node = self.sum_()
        if self.op(*COMPARE):
            op = self.take()[1]
            node = Binary(op, node, self.sum_())
            if self.op(*COMPARE):
                raise FormulaError("comparisons cannot be chained; use 'and'", self.peek()[2])
        return node

    def sum_(self) -> Node:
        node = self.product()
        while self.op("+", "-"):
            op = self.take()[1]
            node = Binary(op, node, self.product())
        return node

    def product(self) -> Node:
        node = self.unary()
        while self.op("*", "/"):
            op = self.take()[1]
            node = Binary(op, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.op("-", "+"):
            op = self.take()[1]
            self.nest()
            node = Unary(op, self.unary())
            self.depth -= 1
            return node
        return self.power()

    def power(self) -> Node:
        node = self.atom()
        if self.op("^", "**"):
            self.take()
            self.nest()
            node = Binary("^", node, self.unary())
            self.depth -= 1
        return node

    def atom(self) -> Node:
        kind, text, pos = self.peek()
        if kind == "num":
            self.take()
            return Num(float(text))
        if kind == "name":
            if text in KEYWORDS:
                raise self.unexpected()
            self.take()
            if self.op("("):
                return self.call(text, pos)
            return Name(text, pos)
        if self.op("("):
            self.take()
            self.nest()
            node = self.or_()
            self.depth -= 1
            self.expect(")")
            return node
        raise self.unexpected()

    def call(self, fn: str, pos: int) -> Node:
        if fn not in FUNCTIONS:
            raise FormulaError(f"unknown function '{fn}'", pos)
        self.take()  # "("
        self.nest()
        args: list[Node] = []
        if not self.op(")"):
            args.append(self.or_())
            while self.op(","):
                self.take()
                args.append(self.or_())
        self.expect(")")
        self.depth -= 1
        lo, hi = FUNCTIONS[fn]
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = f"{lo}" if lo == hi else f"at least {lo}"
            noun = "argument" if want == "1" else "arguments"
            raise FormulaError(f"{fn}() takes {want} {noun}, not {len(args)}", pos)
        return Call(fn, tuple(args), pos)


def parse(text: str) -> Node:
    """Parse a formula; raises FormulaError with a position."""
    if len(text) > MAX_LENGTH:
        raise FormulaError(f"formula is too long (more than {MAX_LENGTH} characters)")
    if not text.strip():
        raise FormulaError("empty formula")
    return _Parser(text).parse()


def names(node: Node) -> list[tuple[str, int]]:
    """Variable names a formula uses, with positions, in order of appearance."""
    if isinstance(node, Name):
        return [(node.name, node.pos)]
    if isinstance(node, Unary):
        return names(node.arg)
    if isinstance(node, Binary):
        return names(node.left) + names(node.right)
    if isinstance(node, Call):
        return [n for a in node.args for n in names(a)]
    return []


def check(node: Node, allowed: set[str] | frozenset[str]) -> None:
    """Raise FormulaError for the first name that is neither allowed nor a constant."""
    for name, pos in names(node):
        if name not in allowed and name not in CONSTANTS:
            raise FormulaError(f"unknown name '{name}'", pos)


Value = np.ndarray | float


def _nan_any(*xs: np.ndarray) -> np.ndarray:
    out = np.isnan(xs[0])
    for x in xs[1:]:
        out = out | np.isnan(x)
    return out


def _bool(mask: np.ndarray, missing: np.ndarray) -> np.ndarray:
    return np.where(missing, np.nan, mask.astype(np.float64))


def _eval(node: Node, env: Mapping[str, Value]) -> np.ndarray:
    if isinstance(node, Num):
        return np.asarray(node.value, dtype=np.float64)
    if isinstance(node, Name):
        if node.name in env:
            return np.asarray(env[node.name], dtype=np.float64)
        if node.name in CONSTANTS:
            return np.asarray(CONSTANTS[node.name])
        raise FormulaError(f"unknown name '{node.name}'", node.pos)
    if isinstance(node, Unary):
        a = _eval(node.arg, env)
        if node.op == "-":
            return -a
        if node.op == "+":
            return a
        return _bool(a == 0, np.isnan(a))  # not
    if isinstance(node, Binary):
        a, b = _eval(node.left, env), _eval(node.right, env)
        op = node.op
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b
        if op == "^":
            return np.power(a, b)
        missing = _nan_any(a, b)
        if op == "and":
            return _bool((a != 0) & (b != 0), missing)
        if op == "or":
            return _bool((a != 0) | (b != 0), missing)
        cmp = {
            "<": np.less,
            "<=": np.less_equal,
            ">": np.greater,
            ">=": np.greater_equal,
            "==": np.equal,
            "!=": np.not_equal,
        }[op]
        return _bool(cmp(a, b), missing)
    # Call
    args = [_eval(a, env) for a in node.args]
    fn = node.fn
    if fn == "where":
        c, x, y = args
        return np.where(np.isnan(c), np.nan, np.where(c != 0, x, y))
    if fn == "min":
        out = args[0]
        for x in args[1:]:
            out = np.minimum(out, x)
        return out
    if fn == "max":
        out = args[0]
        for x in args[1:]:
            out = np.maximum(out, x)
        return out
    (x,) = args
    if fn == "isnan":
        return np.isnan(x).astype(np.float64)
    ufunc = {
        "abs": np.abs,
        "sqrt": np.sqrt,
        "log": np.log,
        "log10": np.log10,
        "exp": np.exp,
        "sign": np.sign,
    }[fn]
    return ufunc(x)


def evaluate(node: Node, env: Mapping[str, Value]) -> np.ndarray:
    """Evaluate on numpy arrays (broadcast) or numbers; non-finite results become NaN."""
    with np.errstate(all="ignore"):
        out = np.asarray(_eval(node, env), dtype=np.float64)
    return np.where(np.isfinite(out), out, np.nan)
