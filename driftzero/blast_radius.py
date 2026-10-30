"""Deterministic blast-radius analysis: which code does a DriftSpec break?

No LLM. Walks every Python file in a repo with `ast` and:

1. Finds *direct* hits: code that touches a removed / renamed / retyped field
   (`payload["noise"]`, `.get("noise")`, `frame.noise`, `Pose(roll=...)`,
   dataclass field `roll: float`, dict literal keys), string literals that
   only exist in the old endpoint paths (`"/v1"`), changed constant values
   (`"v1"`), and test data files (JSON under tests/) carrying old fields.
2. Builds edges between symbols: imports, calls, name references, pytest
   fixture injection, data-file loads, and class membership.
3. Propagates impact backwards along those edges (a caller of an impacted
   function is impacted), recording depth and which dependency caused it.

Type annotations are deliberately not edges: mentioning `TelemetryFrame` in a
signature does not break when one of its fields goes away; reading the field does.

Smoke test:  python -m driftzero.blast_radius [repo] [driftspec.json]
"""
from __future__ import annotations

import ast
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal

from pydantic import BaseModel

from driftzero.spec import DriftSpec

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".driftzero", "build", "dist",
             "node_modules", ".pytest_cache", ".mypy_cache", ".tox"}
DATA_DIRS = {"tests", "test", "fixtures", "testdata"}
GETTERS = {"get", "pop", "setdefault"}
PROPAGATING = {"call", "ref", "fixture", "data", "member"}
MATCH_EDGE = {"endpoint": "endpoint", "literal": "literal"}  # every other match kind -> "field_access"

SymbolKind = Literal["function", "method", "class", "constant", "fixture", "test"]


class Match(BaseModel):
    probe: str  # e.g. "removed:TelemetryFrame.noise"
    kind: str   # subscript | get | attribute | keyword | field_def | dict_key | endpoint | literal
    line: int
    text: str   # the key / attribute / literal that matched


class Edge(BaseModel):
    src: str  # symbol id ("path.py::Qual.name"), module path for imports
    dst: str  # symbol id, data-file path, or probe id
    kind: str  # import | call | ref | fixture | data | member | field_access | endpoint | literal
    line: int
    detail: str = ""


class ImpactedSymbol(BaseModel):
    id: str
    file: str
    name: str
    kind: SymbolKind
    line: int
    end_line: int
    direct: bool
    depth: int  # 0 = touches the drift itself
    matches: list[Match] = []
    via: list[str] = []  # impacted symbols / data files this one depends on


class DataHit(BaseModel):
    file: str
    probes: list[str]


class BlastRadius(BaseModel):
    repo: str
    api: str
    from_version: str
    to_version: str
    files: list[str]
    symbols: list[ImpactedSymbol]
    tests: list[str]  # pytest node ids
    data_files: list[DataHit]
    edges: list[Edge]
    added_fields: list[str]  # new in the spec; nothing reads them yet by construction

    def symbol(self, sid: str) -> ImpactedSymbol | None:
        return next((s for s in self.symbols if s.id == sid), None)


# --------------------------------------------------------------------------- probes

def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _template_re(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile("".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts))


class _Probes:
    """What to look for in code, derived from the DriftSpec."""

    def __init__(self, spec: DriftSpec):
        self.by_leaf: dict[str, list[str]] = defaultdict(list)
        self.by_literal: dict[str, list[str]] = defaultdict(list)
        self.value_old: dict[str, str] = {}
        self.endpoints: list[tuple[str, re.Pattern[str], str, re.Pattern[str], str]] = []
        for f in spec.fields_removed:
            self.by_leaf[_leaf(f.path)].append(f"removed:{f.schema_name}.{f.path}")
        for f in spec.fields_renamed:
            self.by_leaf[_leaf(f.old_path)].append(f"renamed:{f.schema_name}.{f.old_path}")
        for f in spec.type_changes:
            self.by_leaf[_leaf(f.path)].append(f"type:{f.schema_name}.{f.path}")
        for v in spec.value_changes:
            pid = f"value:{v.schema_name}.{v.path}={v.old}"
            self.by_leaf[_leaf(v.path)].append(pid)
            self.by_literal[v.old].append(pid)
            self.value_old[pid] = v.old
        for e in spec.endpoints_renamed:
            self.endpoints.append((f"endpoint:{e.method} {e.old}", _template_re(e.old), e.old,
                                   _template_re(e.new), e.new))

    def endpoint_hits(self, s: str) -> list[str]:
        """Path literals that belong to an old endpoint but not to its replacement."""
        if len(s) < 2 or not s.startswith("/"):
            return []
        hits = []
        for pid, old_re, old, new_re, new in self.endpoints:
            in_old = bool(old_re.fullmatch(s)) or s in old
            in_new = bool(new_re.fullmatch(s)) or s in new
            if in_old and not in_new:
                hits.append(pid)
        return hits


# --------------------------------------------------------------------------- index

@dataclass
class _Symbol:
    id: str
    file: str
    name: str
    kind: str
    node: ast.AST
    line: int
    end_line: int
    cls: str | None = None
    params: list[str] = field(default_factory=list)


@dataclass
class _Module:
    rel: str
    dotted: str
    is_init: bool
    is_test: bool
    tree: ast.Module
    top: dict[str, str] = field(default_factory=dict)  # top-level name -> symbol id
    imports: dict[str, tuple[str, str | None, int]] = field(default_factory=dict)  # alias -> (module, attr, line)


def _discover(repo: Path) -> Iterator[Path]:
    for p in sorted(repo.rglob("*.py")):
        parts = p.relative_to(repo).parts
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parts):
            continue
        yield p


def _dotted(rel: str) -> tuple[str, bool]:
    parts = rel[:-3].split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    return ".".join(parts), is_init


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _is_fixture(fn: ast.AST) -> bool:
    for d in getattr(fn, "decorator_list", []):
        d = d.func if isinstance(d, ast.Call) else d
        if (isinstance(d, ast.Name) and d.id == "fixture") or (isinstance(d, ast.Attribute) and d.attr == "fixture"):
            return True
    return False


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs) if x.arg not in ("self", "cls", "request")]


def _walk(node: ast.AST) -> Iterator[ast.AST]:
    """ast.walk in source order, skipping type annotations."""
    yield node
    for name, value in ast.iter_fields(node):
        if name in ("annotation", "returns", "type_comment"):
            continue
        if isinstance(value, list):
            for v in value:
                if isinstance(v, ast.AST):
                    yield from _walk(v)
        elif isinstance(value, ast.AST):
            yield from _walk(value)


def _target_names(t: ast.AST) -> Iterator[ast.Name]:
    if isinstance(t, ast.Name):
        yield t
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from _target_names(e)


class _Index:
    def __init__(self, repo: Path):
        self.repo = repo
        self.modules: dict[str, _Module] = {}
        self.by_dotted: dict[str, _Module] = {}
        self.symbols: dict[str, _Symbol] = {}
        for path in _discover(repo):
            rel = path.relative_to(repo).as_posix()
            try:
                tree = ast.parse(path.read_text(), filename=rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
            dotted, is_init = _dotted(rel)
            mod = _Module(rel, dotted, is_init, _is_test_file(rel), tree)
            self.modules[rel] = mod
            self.by_dotted[dotted] = mod
        for mod in self.modules.values():
            self._collect_symbols(mod)
            self._collect_imports(mod)

        methods: dict[str, list[str]] = defaultdict(list)
        self.fixtures: dict[str, list[_Symbol]] = defaultdict(list)
        for s in self.symbols.values():
            if s.cls and not s.name.split(".")[-1].startswith("__"):
                methods[s.name.split(".")[-1]].append(s.id)
            if s.kind == "fixture":
                self.fixtures[s.name.split(".")[-1]].append(s)
        self.unique_methods = {name: ids[0] for name, ids in methods.items() if len(ids) == 1}

    def _collect_symbols(self, mod: _Module) -> None:
        def add(qual: str, kind: str, node: ast.AST, cls: str | None = None) -> str:
            sid = f"{mod.rel}::{qual}"
            params = _params(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
            self.symbols[sid] = _Symbol(sid, mod.rel, qual, kind, node, node.lineno,
                                        node.end_lineno or node.lineno, cls, params)
            return sid

        for stmt in mod.tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = ("fixture" if _is_fixture(stmt)
                        else "test" if mod.is_test and stmt.name.startswith("test") else "function")
                mod.top[stmt.name] = add(stmt.name, kind, stmt)
            elif isinstance(stmt, ast.ClassDef):
                cid = mod.top[stmt.name] = add(stmt.name, "class", stmt)
                test_cls = mod.is_test and stmt.name.startswith("Test")
                for sub in stmt.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = ("fixture" if _is_fixture(sub)
                                else "test" if test_cls and sub.name.startswith("test") else "method")
                        add(f"{stmt.name}.{sub.name}", kind, sub, cls=cid)
            elif isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    for n in _target_names(t):
                        mod.top[n.id] = add(n.id, "constant", stmt)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                mod.top[stmt.target.id] = add(stmt.target.id, "constant", stmt)

    def _collect_imports(self, mod: _Module) -> None:
        # Function-local imports are folded into the module table (good enough for resolution).
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.imports[a.asname] = (a.name, None, node.lineno)
                    else:
                        head = a.name.split(".")[0]
                        mod.imports[head] = (head, None, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg = mod.dotted.split(".") if mod.is_init else mod.dotted.split(".")[:-1]
                    pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    base = ".".join([*pkg, *([node.module] if node.module else [])])
                for a in node.names:
                    if a.name != "*":
                        mod.imports[a.asname or a.name] = (base, a.name, node.lineno)

    # -- name resolution: returns ("sym", id) | ("mod", dotted) | None

    def resolve_name(self, mod: _Module, name: str, depth: int = 0) -> tuple[str, str] | None:
        if depth > 6:
            return None
        if name in mod.top:
            return ("sym", mod.top[name])
        if name in mod.imports:
            target, attr, _ = mod.imports[name]
            if attr is None:
                return ("mod", target) if target in self.by_dotted else None
            return self.resolve_attr(target, attr, depth + 1)
        return None

    def resolve_attr(self, dotted: str, attr: str, depth: int = 0) -> tuple[str, str] | None:
        if f"{dotted}.{attr}" in self.by_dotted:
            return ("mod", f"{dotted}.{attr}")
        target = self.by_dotted.get(dotted)
        return self.resolve_name(target, attr, depth) if target else None

    def resolve_chain(self, mod: _Module, node: ast.Attribute, local: set[str]) -> tuple[str, str] | None:
        attrs = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name) or cur.id in local or cur.id == "self":
            return None
        res = self.resolve_name(mod, cur.id)
        if not res or res[0] != "mod":
            return None
        for attr in reversed(attrs):
            res = self.resolve_attr(res[1], attr)
            if res is None or res[0] == "sym":
                return res
        return res

    def fixture_for(self, file: str, name: str) -> _Symbol | None:
        candidates = self.fixtures.get(name, [])
        for s in candidates:
            if s.file == file:
                return s
        best, best_len = None, -1
        for s in candidates:
            if s.file.endswith("conftest.py"):
                d = s.file.rsplit("/", 1)[0] if "/" in s.file else ""
                if (d == "" or file.startswith(d + "/")) and len(d) > best_len:
                    best, best_len = s, len(d)
        return best


# --------------------------------------------------------------------------- analysis

def _scan_data(repo: Path, probes: _Probes) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for p in sorted(repo.rglob("*.json")):
        parts = p.relative_to(repo).parts
        if any(x in SKIP_DIRS for x in parts) or not DATA_DIRS & set(parts[:-1]):
            continue
        try:
            doc = json.loads(p.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        found: set[str] = set()

        def visit(o: object) -> None:
            if isinstance(o, dict):
                for k, v in o.items():
                    for pid in probes.by_leaf.get(k, []):
                        if pid not in probes.value_old or v == probes.value_old[pid]:
                            found.add(pid)
                    visit(v)
            elif isinstance(o, list):
                for v in o:
                    visit(v)

        visit(doc)
        if found:
            hits[p.relative_to(repo).as_posix()] = sorted(found)
    return hits


def _symbol_roots(sym: _Symbol) -> tuple[list[ast.AST], list[ast.Name]]:
    """AST subtrees owned by a symbol (methods excluded from their class), plus dataclass-style field names."""
    n = sym.node
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [*n.decorator_list, *n.args.defaults, *[d for d in n.args.kw_defaults if d], *n.body], []
    if isinstance(n, ast.ClassDef):
        roots: list[ast.AST] = [*n.decorator_list, *n.bases, *n.keywords]
        fields: list[ast.Name] = []
        for s in n.body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                fields.append(s.target)
                if s.value:
                    roots.append(s.value)
            else:
                roots.append(s)
        return roots, fields
    value = getattr(n, "value", None)
    return ([value] if value is not None else []), []


def _scan_symbol(idx: _Index, sym: _Symbol, probes: _Probes, data_files: dict[str, list[str]]
                 ) -> tuple[list[Match], list[Edge]]:
    mod = idx.modules[sym.file]
    roots, fields = _symbol_roots(sym)
    nodes = [x for r in roots for x in _walk(r)]
    callees = {id(x.func) for x in nodes if isinstance(x, ast.Call)}
    local = set(sym.params) | {x.id for x in nodes if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
    if isinstance(sym.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        local |= {x.arg for x in sym.node.args.args}  # includes self/cls

    matches: list[Match] = []
    edges: list[Edge] = []

    def leaf_hit(key: str, kind: str, line: int) -> None:
        for pid in probes.by_leaf.get(key, []):
            matches.append(Match(probe=pid, kind=kind, line=line, text=key))

    def link(dst: str, kind: str, line: int, detail: str = "") -> None:
        if dst != sym.id and dst != sym.cls:
            edges.append(Edge(src=sym.id, dst=dst, kind=kind, line=line, detail=detail))

    for t in fields:
        leaf_hit(t.id, "field_def", t.lineno)

    for x in nodes:
        line = getattr(x, "lineno", sym.line)
        if isinstance(x, ast.Subscript) and isinstance(x.slice, ast.Constant) and isinstance(x.slice.value, str):
            leaf_hit(x.slice.value, "subscript", line)
        if (isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr in GETTERS
                and x.args and isinstance(x.args[0], ast.Constant) and isinstance(x.args[0].value, str)):
            leaf_hit(x.args[0].value, "get", line)
        if isinstance(x, ast.Attribute):
            res = idx.resolve_chain(mod, x, local)
            if res and res[0] == "sym":
                link(res[1], "call" if id(x) in callees else "ref", line)
            elif res is None:
                leaf_hit(x.attr, "attribute", line)
                if id(x) in callees and x.attr in idx.unique_methods:
                    link(idx.unique_methods[x.attr], "call", line, "method-name")
        elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in local:
            res = idx.resolve_name(mod, x.id)
            if res and res[0] == "sym":
                link(res[1], "call" if id(x) in callees else "ref", line)
        elif isinstance(x, ast.keyword) and x.arg:
            leaf_hit(x.arg, "keyword", line)
        elif isinstance(x, ast.Dict):
            for k in x.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    leaf_hit(k.value, "dict_key", line)
        elif isinstance(x, ast.Constant) and isinstance(x.value, str):
            s = x.value
            for pid in probes.by_literal.get(s, []):
                matches.append(Match(probe=pid, kind="literal", line=line, text=s))
            for pid in probes.endpoint_hits(s):
                matches.append(Match(probe=pid, kind="endpoint", line=line, text=s))
            for rel in data_files:
                if s and (s == rel or rel == s.lstrip("./") or rel.endswith("/" + s.lstrip("./"))):
                    link(rel, "data", line)

    if sym.kind in ("test", "fixture") or idx.modules[sym.file].is_test:
        for p in sym.params:
            fx = idx.fixture_for(sym.file, p)
            if fx:
                link(fx.id, "fixture", sym.line, p)
    return matches, edges


def _dedupe(edges: Iterable[Edge]) -> list[Edge]:
    seen: dict[tuple[str, str, str], Edge] = {}
    for e in edges:
        key = (e.src, e.dst, e.kind)
        if key not in seen or e.line < seen[key].line:
            seen[key] = e
    return sorted(seen.values(), key=lambda e: (e.src, e.dst, e.kind))


def analyze(repo: str | Path, spec: DriftSpec) -> BlastRadius:
    repo = Path(repo).resolve()
    probes = _Probes(spec)
    idx = _Index(repo)
    data_files = _scan_data(repo, probes)

    matches: dict[str, list[Match]] = {}
    edges: list[Edge] = []
    for sym in idx.symbols.values():
        m, e = _scan_symbol(idx, sym, probes, data_files)
        matches[sym.id] = m
        edges.extend(e)
        if sym.cls:
            edges.append(Edge(src=sym.cls, dst=sym.id, kind="member", line=sym.line))
    for mod in idx.modules.values():
        for alias, (target, attr, line) in mod.imports.items():
            if attr is not None:
                res = idx.resolve_attr(target, attr)
                if res and res[0] == "sym":
                    edges.append(Edge(src=mod.rel, dst=res[1], kind="import", line=line, detail=alias))
    edges = _dedupe(edges)

    # Propagate impact from direct hits backwards along dependency edges.
    seeds = [sid for sid, m in matches.items() if m] + list(data_files)
    depth: dict[str, int] = {s: 0 for s in seeds}
    via: dict[str, set[str]] = defaultdict(set)
    rdeps: dict[str, list[Edge]] = defaultdict(list)
    for e in edges:
        if e.kind in PROPAGATING:
            rdeps[e.dst].append(e)
    queue = deque(seeds)
    while queue:
        cur = queue.popleft()
        for e in rdeps[cur]:
            via[e.src].add(cur)
            if e.src not in depth:
                depth[e.src] = depth[cur] + 1
                queue.append(e.src)

    impacted: list[ImpactedSymbol] = []
    for sid in depth:
        sym = idx.symbols.get(sid)
        if sym is None:
            continue  # data file node
        ms = sorted(matches[sid], key=lambda m: (m.line, m.probe, m.kind))
        impacted.append(ImpactedSymbol(
            id=sid, file=sym.file, name=sym.name, kind=sym.kind,  # type: ignore[arg-type]
            line=sym.line, end_line=sym.end_line, direct=bool(ms), depth=depth[sid],
            matches=ms, via=sorted(via[sid]),
        ))
    impacted.sort(key=lambda s: (s.file, s.line, s.name))

    out_edges = [e for e in edges if e.dst in depth and (e.kind == "import" or e.src in depth)]
    for s in impacted:
        for m in s.matches:
            out_edges.append(Edge(src=s.id, dst=m.probe, kind=MATCH_EDGE.get(m.kind, "field_access"),
                                  line=m.line, detail=f"{m.kind}:{m.text}"))

    return BlastRadius(
        repo=str(repo),
        api=spec.api,
        from_version=spec.from_version,
        to_version=spec.to_version,
        files=sorted({s.file for s in impacted}),
        symbols=impacted,
        tests=[f"{s.file}::{s.name.replace('.', '::')}" for s in impacted if s.kind == "test"],
        data_files=[DataHit(file=f, probes=p) for f, p in sorted(data_files.items())],
        edges=_dedupe(out_edges),
        added_fields=[f"{f.schema_name}.{f.path}" for f in spec.fields_added],
    )


# --------------------------------------------------------------------------- rendering

_EVIDENCE = {
    "subscript": '["{}"]', "get": '.get("{}")', "attribute": ".{}", "keyword": "{}=",
    "field_def": "{}: …", "dict_key": '{{"{}": …}}', "endpoint": '"{}"', "literal": '"{}"',
}


def evidence(s: ImpactedSymbol) -> str:
    if s.matches:
        seen: list[str] = []
        for m in s.matches:
            ev = _EVIDENCE[m.kind].format(m.text)
            if ev not in seen:
                seen.append(ev)
        return " ".join(seen)
    return "via " + ", ".join(v.split("::")[-1] for v in s.via)


def render(br: BlastRadius, console=None) -> None:
    from rich.console import Console
    from rich.table import Table

    console = console or Console()
    table = Table(title=f"Blast radius: {br.api} {br.from_version} → {br.to_version}", show_lines=False)
    table.add_column("file", style="cyan", no_wrap=True)
    table.add_column("symbol", style="bold")
    table.add_column("kind")
    table.add_column("impact")
    table.add_column("evidence", overflow="fold")
    prev = None
    for s in br.symbols:
        impact = "[red]direct[/red]" if s.direct else f"[yellow]+{s.depth}[/yellow]"
        table.add_row(s.file if s.file != prev else "", s.name, s.kind, impact, evidence(s))
        prev = s.file
    console.print(table)
    n_direct = sum(s.direct for s in br.symbols)
    console.print(f"{len(br.files)} files, {len(br.symbols)} symbols ({n_direct} direct), {len(br.tests)} tests")
    for d in br.data_files:
        console.print(f"data: {d.file}  [dim]{', '.join(d.probes)}[/dim]")
    if br.added_fields:
        console.print(f"new fields to wire in: {', '.join(br.added_fields)}")


if __name__ == "__main__":
    import sys

    bench = Path(__file__).resolve().parents[1] / "benchmarks" / "robotics_telemetry_drift"
    repo_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else bench
    spec_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else bench / "driftspec_v2.json"
    render(analyze(repo_arg, DriftSpec.load(spec_arg)))
