// The formula language of error panes, e.g. "abs(P - T)" or "where(T >= h, 1, 0)".
//
// Same grammar and rules as the server (src/geoloupe/formula.py); both pass the shared cases in
// tests/formula_cases.json. A formula is parsed once into a tree and compiled into a function of
// (P, T) that runs per pixel. Nothing is evaluated with eval() or new Function().
//
// Rules that keep missing data missing: a comparison, "and", "or", "not" or "where" with a missing
// (NaN) input gives NaN, never 0 or 1; any result that is not finite becomes NaN.

export const MAX_LENGTH = 500;
export const MAX_DEPTH = 40;
export const CONSTANTS: Record<string, number> = { nan: NaN, pi: Math.PI };
/** name → [min arguments, max arguments (Infinity: any number)] */
export const FUNCTIONS: Record<string, [number, number]> = {
  abs: [1, 1],
  sqrt: [1, 1],
  log: [1, 1],
  log10: [1, 1],
  exp: [1, 1],
  sign: [1, 1],
  isnan: [1, 1],
  where: [3, 3],
  min: [2, Infinity],
  max: [2, Infinity],
};
const COMPARE = ["<=", ">=", "==", "!=", "<", ">"];
const KEYWORDS = ["and", "or", "not"];

export class FormulaError extends Error {
  readonly pos: number | null;
  readonly reason: string;
  constructor(reason: string, pos: number | null = null) {
    super(pos === null ? reason : `${reason} (at character ${pos + 1})`);
    this.reason = reason;
    this.pos = pos;
  }
}

export type Node =
  | { k: "num"; value: number }
  | { k: "name"; name: string; pos: number }
  | { k: "unary"; op: "-" | "+" | "not"; arg: Node }
  | { k: "binary"; op: string; left: Node; right: Node }
  | { k: "call"; fn: string; args: Node[]; pos: number };

type Token = { kind: "num" | "name" | "op" | "end"; text: string; pos: number };

const TOKEN =
  /(?<num>(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)|(?<name>[A-Za-z_][A-Za-z0-9_]*)|(?<op>\*\*|<=|>=|==|!=|[-+*/^(),<>])/y;

export function tokenize(text: string): Token[] {
  const out: Token[] = [];
  let i = 0;
  while (i < text.length) {
    if (/\s/.test(text[i])) {
      i++;
      continue;
    }
    TOKEN.lastIndex = i;
    const m = TOKEN.exec(text);
    const g = m?.groups;
    if (!m || !g) throw new FormulaError(`unexpected character '${text[i]}'`, i);
    const kind = g.num !== undefined ? "num" : g.name !== undefined ? "name" : "op";
    out.push({ kind, text: m[0], pos: i });
    i += m[0].length;
  }
  out.push({ kind: "end", text: "", pos: text.length });
  return out;
}

class Parser {
  private i = 0;
  private depth = 0;
  constructor(private readonly tokens: Token[]) {}

  private peek(): Token {
    return this.tokens[this.i];
  }
  private take(): Token {
    return this.tokens[this.i++];
  }
  private kw(word: string): boolean {
    const t = this.peek();
    return t.kind === "name" && t.text === word;
  }
  private op(...texts: string[]): boolean {
    const t = this.peek();
    return t.kind === "op" && texts.includes(t.text);
  }
  private unexpected(hint = ""): FormulaError {
    const t = this.peek();
    const suffix = hint ? `: ${hint}` : "";
    if (t.kind === "end") return new FormulaError(`unexpected end of formula${suffix}`, t.pos);
    return new FormulaError(`unexpected '${t.text}'${suffix}`, t.pos);
  }
  private expect(text: string): void {
    if (!this.op(text)) throw this.unexpected(`expected '${text}'`);
    this.take();
  }
  private nested<T>(f: () => T): T {
    if (++this.depth > MAX_DEPTH) throw new FormulaError("formula is nested too deeply", this.peek().pos);
    const out = f();
    this.depth--;
    return out;
  }

  parse(): Node {
    const node = this.or();
    if (this.peek().kind !== "end") throw this.unexpected();
    return node;
  }
  private or(): Node {
    let node = this.and();
    while (this.kw("or")) {
      this.take();
      node = { k: "binary", op: "or", left: node, right: this.and() };
    }
    return node;
  }
  private and(): Node {
    let node = this.not();
    while (this.kw("and")) {
      this.take();
      node = { k: "binary", op: "and", left: node, right: this.not() };
    }
    return node;
  }
  private not(): Node {
    if (this.kw("not")) {
      this.take();
      return this.nested(() => ({ k: "unary", op: "not", arg: this.not() }) as Node);
    }
    return this.compare();
  }
  private compare(): Node {
    let node = this.sum();
    if (this.op(...COMPARE)) {
      const op = this.take().text;
      node = { k: "binary", op, left: node, right: this.sum() };
      if (this.op(...COMPARE))
        throw new FormulaError("comparisons cannot be chained; use 'and'", this.peek().pos);
    }
    return node;
  }
  private sum(): Node {
    let node = this.product();
    while (this.op("+", "-")) {
      const op = this.take().text;
      node = { k: "binary", op, left: node, right: this.product() };
    }
    return node;
  }
  private product(): Node {
    let node = this.unary();
    while (this.op("*", "/")) {
      const op = this.take().text;
      node = { k: "binary", op, left: node, right: this.unary() };
    }
    return node;
  }
  private unary(): Node {
    if (this.op("-", "+")) {
      const op = this.take().text as "-" | "+";
      return this.nested(() => ({ k: "unary", op, arg: this.unary() }) as Node);
    }
    return this.power();
  }
  private power(): Node {
    const node = this.atom();
    if (this.op("^", "**")) {
      this.take();
      return this.nested(() => ({ k: "binary", op: "^", left: node, right: this.unary() }) as Node);
    }
    return node;
  }
  private atom(): Node {
    const t = this.peek();
    if (t.kind === "num") {
      this.take();
      return { k: "num", value: parseFloat(t.text) };
    }
    if (t.kind === "name") {
      if (KEYWORDS.includes(t.text)) throw this.unexpected();
      this.take();
      if (this.op("(")) return this.call(t.text, t.pos);
      return { k: "name", name: t.text, pos: t.pos };
    }
    if (this.op("(")) {
      this.take();
      const node = this.nested(() => this.or());
      this.expect(")");
      return node;
    }
    throw this.unexpected();
  }
  private call(fn: string, pos: number): Node {
    if (!(fn in FUNCTIONS)) throw new FormulaError(`unknown function '${fn}'`, pos);
    this.take(); // "("
    const args = this.nested(() => {
      const out: Node[] = [];
      if (!this.op(")")) {
        out.push(this.or());
        while (this.op(",")) {
          this.take();
          out.push(this.or());
        }
      }
      this.expect(")");
      return out;
    });
    const [lo, hi] = FUNCTIONS[fn];
    if (args.length < lo || args.length > hi) {
      const want = lo === hi ? `${lo}` : `at least ${lo}`;
      const noun = want === "1" ? "argument" : "arguments";
      throw new FormulaError(`${fn}() takes ${want} ${noun}, not ${args.length}`, pos);
    }
    return { k: "call", fn, args, pos };
  }
}

/** Parse a formula; throws FormulaError with a position. */
export function parse(text: string): Node {
  if (text.length > MAX_LENGTH)
    throw new FormulaError(`formula is too long (more than ${MAX_LENGTH} characters)`);
  if (!text.trim()) throw new FormulaError("empty formula");
  return new Parser(tokenize(text)).parse();
}

/** Variable names used, with positions, in order of appearance. */
export function names(node: Node): { name: string; pos: number }[] {
  switch (node.k) {
    case "name":
      return [{ name: node.name, pos: node.pos }];
    case "unary":
      return names(node.arg);
    case "binary":
      return [...names(node.left), ...names(node.right)];
    case "call":
      return node.args.flatMap(names);
    default:
      return [];
  }
}

/** Throw FormulaError for the first name that is neither allowed nor a constant. */
export function check(node: Node, allowed: Iterable<string>): void {
  const ok = new Set(allowed);
  for (const { name, pos } of names(node))
    if (!ok.has(name) && !(name in CONSTANTS)) throw new FormulaError(`unknown name '${name}'`, pos);
}

export type PixelFn = (P: number, T: number) => number;

const bool = (b: boolean) => (b ? 1 : 0);
const UNARY: Record<string, (x: number) => number> = {
  abs: Math.abs,
  sqrt: Math.sqrt,
  log: Math.log,
  log10: Math.log10,
  exp: Math.exp,
  sign: Math.sign,
  isnan: (x) => bool(Number.isNaN(x)),
};
const BINARY: Record<string, (a: number, b: number) => number> = {
  "+": (a, b) => a + b,
  "-": (a, b) => a - b,
  "*": (a, b) => a * b,
  "/": (a, b) => a / b,
  "^": (a, b) => Math.pow(a, b),
};
// NaN in → NaN out; else 1 or 0.
const LOGIC: Record<string, (a: number, b: number) => boolean> = {
  "<": (a, b) => a < b,
  "<=": (a, b) => a <= b,
  ">": (a, b) => a > b,
  ">=": (a, b) => a >= b,
  "==": (a, b) => a === b,
  "!=": (a, b) => a !== b,
  and: (a, b) => a !== 0 && b !== 0,
  or: (a, b) => a !== 0 || b !== 0,
};

function build(node: Node, env: Record<string, number>): PixelFn {
  switch (node.k) {
    case "num": {
      const v = node.value;
      return () => v;
    }
    case "name": {
      if (node.name === "P") return (P) => P;
      if (node.name === "T") return (_P, T) => T;
      const v = node.name in env ? env[node.name] : CONSTANTS[node.name];
      if (v === undefined) throw new FormulaError(`unknown name '${node.name}'`, node.pos);
      return () => v;
    }
    case "unary": {
      const a = build(node.arg, env);
      if (node.op === "-") return (P, T) => -a(P, T);
      if (node.op === "+") return a;
      return (P, T) => {
        const x = a(P, T);
        return Number.isNaN(x) ? NaN : bool(x === 0);
      };
    }
    case "binary": {
      const a = build(node.left, env);
      const b = build(node.right, env);
      const arith = BINARY[node.op];
      if (arith) return (P, T) => arith(a(P, T), b(P, T));
      const logic = LOGIC[node.op];
      return (P, T) => {
        const x = a(P, T);
        const y = b(P, T);
        return Number.isNaN(x) || Number.isNaN(y) ? NaN : bool(logic(x, y));
      };
    }
    case "call": {
      const args = node.args.map((n) => build(n, env));
      if (node.fn === "where") {
        const [c, x, y] = args;
        return (P, T) => {
          const v = c(P, T);
          return Number.isNaN(v) ? NaN : v !== 0 ? x(P, T) : y(P, T);
        };
      }
      if (node.fn === "min" || node.fn === "max") {
        const pick = node.fn === "min" ? Math.min : Math.max;
        return (P, T) => {
          let out = args[0](P, T);
          for (let i = 1; i < args.length; i++) out = pick(out, args[i](P, T));
          return out;
        };
      }
      const f = UNARY[node.fn];
      const a = args[0];
      return (P, T) => f(a(P, T));
    }
  }
}

/**
 * Compile a parsed formula into a per-pixel function. `env` gives the parameters; P and T are the
 * pixel's values. A missing P or T, or a result that is not finite, gives NaN.
 */
export function compile(node: Node, env: Record<string, number> = {}): PixelFn {
  const f = build(node, env);
  return (P, T) => {
    if (Number.isNaN(P) || Number.isNaN(T)) return NaN;
    const v = f(P, T);
    return Number.isFinite(v) ? v : NaN;
  };
}

/** Evaluate without the missing-input rule (for the shared tests of the bare language). */
export function evaluateRaw(node: Node, P: number, T: number, env: Record<string, number> = {}) {
  const v = build(node, env)(P, T);
  return Number.isFinite(v) ? v : NaN;
}
