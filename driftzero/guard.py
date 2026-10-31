"""Stage 6: test-integrity guard. Runs on the repair diff before (and instead of wasting) a Modal verify.

AST-compares every changed test file between the base commit and the worktree.

Hard rejections (the repair is sent back, verify is skipped):
  - library code that stubs the DriftSpec's target dependency: try/except ImportError around its import, or
    sys.modules injection under its name (only when the repair introduced it)
  - a test function removed (or renamed)
  - a test's assertion count decreased (assert statements, assert*() calls, pytest.raises/warns)
  - pytest.mark.skip / skipif / xfail added (decorator, module `pytestmark`, or pytest.skip()/xfail() call)
  - an assert replaced by a bare expression (a comparison statement with no `assert`)
  - a new conftest.py anywhere in the project
  - a pytest configuration change (pytest.ini, tox.ini [pytest], setup.cfg [tool:pytest],
    pyproject.toml [tool.pytest.ini_options])

Allowed but reported for human review:
  - literal changes in tests (strings, numbers, expected set/dict/list values)
  - fixture data edits (non-Python files under tests/), and edits to existing conftest.py
  - comparison loosening (new pytest.approx, added abs=/rel=/atol=/rtol= tolerances)
  - added tests, and other structural edits to existing tests

Smoke test:  python -m driftzero.guard .driftzero/wt-<id>
"""
from __future__ import annotations

import ast
import configparser
import difflib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SKIP_MARKS = {"skip", "skipif", "xfail"}
TOLERANCE_KWARGS = {"abs", "rel", "atol", "rtol"}
PYTEST_CONFIG_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}


@dataclass
class Finding:
    rule: str
    file: str
    message: str
    test: str | None = None
    old: str | None = None
    new: str | None = None

    def __str__(self) -> str:
        where = f"{self.file}::{self.test}" if self.test else self.file
        return f"[{self.rule}] {where}: {self.message}"


@dataclass
class GuardResult:
    ok: bool
    rejections: list[Finding] = field(default_factory=list)
    review_items: list[Finding] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [str(f) for f in self.rejections]

    def retry_message(self) -> str:
        lines = ["The test-integrity guard rejected your change. Undo these test edits and fix the library code instead:"]
        for f in self.rejections:
            lines.append(f"- {f.message}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "rejections": [vars(f) for f in self.rejections],
                "review_items": [vars(f) for f in self.review_items]}


@dataclass
class FileChange:
    path: str              # repo-relative
    old: str | None        # None = file added
    new: str | None        # None = file deleted


# --------------------------------------------------------------------------- classification

def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    return "tests" in parts or "test" in parts or Path(path).name.startswith("test_") or path.endswith("_test.py")


def _is_test_module(path: str) -> bool:
    name = Path(path).name
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


# --------------------------------------------------------------------------- AST helpers

def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Top-level functions and class methods, keyed by qualified name."""
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = sub
    return out


def _is_test(qual: str) -> bool:
    parts = qual.split(".")
    return parts[-1].startswith("test") and (len(parts) == 1 or parts[0].startswith("Test"))


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _assert_count(fn: ast.AST) -> int:
    n = 0
    for x in ast.walk(fn):
        if isinstance(x, ast.Assert):
            n += 1
        elif isinstance(x, ast.Call):
            name = _call_name(x)
            if name.startswith("assert") or name in ("raises", "warns", "deprecated_call"):
                n += 1
    return n


def _bare_compares(fn: ast.AST) -> int:
    return sum(isinstance(x, ast.Expr) and isinstance(x.value, ast.Compare) for x in ast.walk(fn))


def _mark_name(dec: ast.AST) -> str | None:
    """'skip' for @pytest.mark.skip / @pytest.mark.skip(...) / @mark.skip."""
    d = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(d, ast.Attribute) and isinstance(d.value, ast.Attribute) and d.value.attr == "mark":
        return d.attr
    if isinstance(d, ast.Attribute) and isinstance(d.value, ast.Name) and d.value.id == "mark":
        return d.attr
    return None


def _skip_markers(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    found = [m for m in (_mark_name(d) for d in fn.decorator_list) if m in SKIP_MARKS]
    for x in ast.walk(fn):
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr in ("skip", "xfail") \
                and isinstance(x.func.value, ast.Name) and x.func.value.id == "pytest":
            found.append(f"pytest.{x.func.attr}()")
    return found


def _module_marks(tree: ast.Module) -> list[str]:
    out = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            out += [m for m in (_mark_name(x) for x in ast.walk(node.value)) if m in SKIP_MARKS]
    return out


def _approx_and_tolerances(node: ast.AST) -> int:
    n = 0
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            if _call_name(x) in ("approx", "isclose", "allclose", "assert_allclose", "assert_almost_equal"):
                n += 1
            n += sum(1 for k in x.keywords if k.arg in TOLERANCE_KWARGS)
    return n


class _Normalize(ast.NodeTransformer):
    """Replace literals (and all-literal collections) with a placeholder."""

    PLACEHOLDER = "<LIT>"

    def _lit(self, node: ast.AST) -> ast.AST:
        return ast.copy_location(ast.Constant(self.PLACEHOLDER), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        return self._lit(node)

    def _collection(self, node: ast.AST, elts: list) -> ast.AST:
        if all(isinstance(e, ast.Constant) or (isinstance(e, ast.UnaryOp) and isinstance(e.operand, ast.Constant))
               for e in elts if e is not None):
            return self._lit(node)
        return self.generic_visit(node)

    def visit_Set(self, node):  # noqa: N802
        return self._collection(node, node.elts)

    def visit_List(self, node):  # noqa: N802
        return self._collection(node, node.elts)

    def visit_Tuple(self, node):  # noqa: N802
        return self._collection(node, node.elts)

    def visit_Dict(self, node):  # noqa: N802
        return self._collection(node, [*node.keys, *node.values])

    def visit_UnaryOp(self, node):  # noqa: N802
        if isinstance(node.operand, ast.Constant):
            return self._lit(node)
        return self.generic_visit(node)


def _norm(stmt_src: str) -> str:
    return ast.dump(_Normalize().visit(ast.parse(stmt_src)))


def _literal_atoms(src: str) -> list[str]:
    """Literal values / all-literal collections in source order, as source text."""
    atoms: list[str] = []

    class V(ast.NodeVisitor):
        def generic_visit(self, node):
            if isinstance(node, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
                elts = [*node.keys, *node.values] if isinstance(node, ast.Dict) else node.elts
                if all(isinstance(e, ast.Constant) for e in elts if e is not None):
                    atoms.append(ast.unparse(node))
                    return
            if isinstance(node, ast.Constant):
                atoms.append(repr(node.value))
                return
            super().generic_visit(node)

    V().visit(ast.parse(src))
    return atoms


def _mentions(token: str, src: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", src, re.IGNORECASE) is not None


def _path_segments(src: str) -> set[str]:
    """Segments of path-like string literals ('/v1/fleet/telemetry' -> {'v1', 'fleet', 'telemetry'})."""
    segs: set[str] = set()
    for lit in _literal_atoms(src):
        if "/" in lit:
            segs |= {s.lower() for s in re.split(r"[/'\"\[\],\s{}]+", lit) if s}
    return segs


def _stale_name_tokens(qual: str, old_body: str, new_body: str) -> list[str]:
    """Version (v1, v2) or endpoint-path tokens in a test's name that its body mentioned before and no longer does."""
    tokens = [t for t in qual.split(".")[-1].split("_")[1:] if t]
    segments = _path_segments(old_body) if old_body.strip() else set()
    stale = []
    for tok in tokens:
        versionish = re.fullmatch(r"v\d+", tok, re.IGNORECASE) is not None
        if not (versionish or tok.lower() in segments):
            continue
        if _mentions(tok, old_body) and not _mentions(tok, new_body):
            stale.append(tok)
    return stale


def _body_lines(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # docstring
    return [ast.unparse(s) for s in body]


# --------------------------------------------------------------------------- library code: target dependency stubs

IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def _module_root(dep: str) -> str:
    return dep.strip().lower().replace("-", "_").split(".")[0]


def _imports_dep(node: ast.AST, root: str) -> bool:
    for x in ast.walk(node):
        if isinstance(x, ast.Import) and any(a.name.split(".")[0] == root for a in x.names):
            return True
        if isinstance(x, ast.ImportFrom) and (x.module or "").split(".")[0] == root and not x.level:
            return True
    return False


def _catches_import_error(h: ast.ExceptHandler) -> bool:
    names = []
    if isinstance(h.type, ast.Name):
        names = [h.type.id]
    elif isinstance(h.type, ast.Attribute):
        names = [h.type.attr]
    elif isinstance(h.type, ast.Tuple):
        names = [e.id if isinstance(e, ast.Name) else getattr(e, "attr", "") for e in h.type.elts]
    return h.type is None or bool(IMPORT_ERRORS & set(names))


def _key_names_dep(node: ast.AST, root: str) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.split(".")[0] == root


def _is_sys_modules(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "modules" and isinstance(node.value, ast.Name) \
        and node.value.id == "sys"


def dependency_stubs(source: str | None, dep: str) -> list[str]:
    """Places in library code that make the target dependency optional or fake it:
    try/except ImportError around its import, or sys.modules injection under its name."""
    if not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    root = _module_root(dep)
    found = []
    for x in ast.walk(tree):
        if isinstance(x, ast.Try) and any(_catches_import_error(h) for h in x.handlers) \
                and any(_imports_dep(b, root) for b in x.body):
            found.append(f"line {x.lineno}: try/except ImportError around `import {root}`")
        if isinstance(x, (ast.Assign, ast.AugAssign)):
            targets = x.targets if isinstance(x, ast.Assign) else [x.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and _is_sys_modules(t.value) and _key_names_dep(t.slice, root):
                    found.append(f"line {x.lineno}: sys.modules[{t.slice.value!r}] assignment")
        if isinstance(x, ast.Call):
            f = x.func
            if isinstance(f, ast.Attribute) and _is_sys_modules(f.value) and f.attr in ("setdefault", "update", "__setitem__"):
                keys = [x.args[0]] if x.args else []
                if f.attr == "update" and x.args and isinstance(x.args[0], ast.Dict):
                    keys = list(x.args[0].keys)
                if any(k is not None and _key_names_dep(k, root) for k in keys):
                    found.append(f"line {x.lineno}: sys.modules.{f.attr}({root!r} ...)")
            if isinstance(f, ast.Attribute) and f.attr == "dict" and x.args and _is_sys_modules(x.args[0]) \
                    and len(x.args) > 1 and isinstance(x.args[1], ast.Dict) \
                    and any(k is not None and _key_names_dep(k, root) for k in x.args[1].keys):
                found.append(f"line {x.lineno}: patch.dict(sys.modules, {{{root!r}: ...}})")
    return found


# --------------------------------------------------------------------------- per-file checks

def _check_python(path: str, old: str | None, new: str | None, res: GuardResult) -> None:
    is_test_module = _is_test_module(path)
    try:
        old_tree = ast.parse(old) if old is not None else ast.Module(body=[], type_ignores=[])
    except SyntaxError:
        old_tree = ast.Module(body=[], type_ignores=[])
    try:
        new_tree = ast.parse(new) if new is not None else ast.Module(body=[], type_ignores=[])
    except SyntaxError as exc:
        res.rejections.append(Finding("syntax_error", path, f"{path} no longer parses: {exc.msg} (line {exc.lineno})"))
        return
    old_fns, new_fns = _functions(old_tree), _functions(new_tree)

    if is_test_module:
        added_marks = sorted(set(_module_marks(new_tree)) - set(_module_marks(old_tree)))
        if added_marks:
            res.rejections.append(Finding("skip_added", path, f"module-level pytestmark {added_marks} added in {path}; remove it"))

    for qual, fn_old in old_fns.items():
        if not (is_test_module and _is_test(qual)):
            continue
        fn_new = new_fns.get(qual)
        if fn_new is None:
            res.rejections.append(Finding("test_removed", path, f"restore test {qual} in {path}; you removed it "
                                                                 f"({_assert_count(fn_old)} asserts)", test=qual))
            continue
        a_old, a_new = _assert_count(fn_old), _assert_count(fn_new)
        if a_new < a_old:
            res.rejections.append(Finding("asserts_removed", path, f"restore test {qual}; you removed {a_old - a_new} "
                                          f"assert{'s' if a_old - a_new > 1 else ''} ({a_old} -> {a_new})", test=qual,
                                          old=str(a_old), new=str(a_new)))
        new_skips = sorted(set(_skip_markers(fn_new)) - set(_skip_markers(fn_old)))
        if new_skips:
            res.rejections.append(Finding("skip_added", path, f"remove {', '.join(new_skips)} from test {qual}", test=qual))
        if _bare_compares(fn_new) > _bare_compares(fn_old):
            res.rejections.append(Finding("bare_expression", path, f"test {qual} has a comparison that is no longer "
                                          "asserted; put the `assert` back", test=qual))

    for qual, fn_new in new_fns.items():
        if qual not in old_fns:
            if is_test_module and _is_test(qual):
                n = _assert_count(fn_new)
                res.review_items.append(Finding("added_test", path, f"new test ({_plural(n, 'assert')})", test=qual,
                                                new=str(n)))
            continue
        _diff_function(path, qual, old_fns[qual], fn_new, res)


def _diff_function(path: str, qual: str, fn_old, fn_new, res: GuardResult) -> None:
    a, b = _body_lines(fn_old), _body_lines(fn_new)
    if a == b and ast.dump(fn_old.args) == ast.dump(fn_new.args):
        return
    literal_pairs: list[tuple[str, str]] = []
    other: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        olds, news = a[i1:i2], b[j1:j2]
        if tag == "replace" and len(olds) == len(news):
            for o, n in zip(olds, news):
                if _norm(o) == _norm(n):
                    literal_pairs += [(x, y) for x, y in zip(_literal_atoms(o), _literal_atoms(n)) if x != y]
                else:
                    other.append(f"`{o}` -> `{n}`")
                if _approx_and_tolerances(ast.parse(n)) > _approx_and_tolerances(ast.parse(o)):
                    res.review_items.append(Finding("loosened", path, "comparison loosened (approx/tolerance added)",
                                                    test=qual, old=o, new=n))
        else:
            other += [f"removed `{o}`" for o in olds] + [f"added `{n}`" for n in news]
            # pure insertions add new checks; only replacing existing lines can loosen one
            if olds and sum(_approx_and_tolerances(ast.parse(n)) for n in news) > \
                    sum(_approx_and_tolerances(ast.parse(o)) for o in olds):
                res.review_items.append(Finding("loosened", path, "comparison loosened (approx/tolerance added)",
                                                test=qual, old="\n".join(olds), new="\n".join(news)))
    kind = "test" if _is_test(qual) else "helper/fixture"
    if _is_test(qual):
        for tok in _stale_name_tokens(qual, "\n".join(a), "\n".join(b)):
            res.review_items.append(Finding(
                "stale_test_name", path, f"test name mentions '{tok}' but its body no longer does; rename it in a "
                f"follow-up (the guard rejects renames during a repair)", test=qual, old=tok))
    for o, n in literal_pairs:
        res.review_items.append(Finding("literal_change", path, f"literal changed in {kind} {qual}", test=qual, old=o, new=n))
    if other:
        res.review_items.append(Finding("modified_test", path, f"{kind} {qual} edited: " + "; ".join(other)[:400], test=qual))


def _pytest_config(name: str, text: str | None) -> object:
    if text is None:
        return None
    try:
        if name == "pyproject.toml":
            return tomllib.loads(text).get("tool", {}).get("pytest")
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(text)
        section = {"pytest.ini": "pytest", "tox.ini": "pytest", "setup.cfg": "tool:pytest"}[name]
        return dict(cp[section]) if cp.has_section(section) else None
    except Exception:  # noqa: BLE001 - unparseable config is itself a change worth rejecting
        return ("unparseable", text)


def _autouse_fixtures(source: str | None) -> set[str]:
    """Names of functions decorated @pytest.fixture(autouse=True) / @fixture(autouse=True)."""
    if source is None:
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if not isinstance(d, ast.Call):
                continue
            f = d.func
            if not ((isinstance(f, ast.Name) and f.id == "fixture") or (isinstance(f, ast.Attribute) and f.attr == "fixture")):
                continue
            if any(k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value is True for k in d.keywords):
                out.add(node.name)
    return out


def check_changes(changes: list[FileChange], target_dependency: str | None = None) -> GuardResult:
    res = GuardResult(ok=True)
    for ch in sorted(changes, key=lambda c: c.path):
        if target_dependency and ch.path.endswith(".py") and not _is_test_path(ch.path):
            new_stubs = dependency_stubs(ch.new, target_dependency)
            if len(new_stubs) > len(dependency_stubs(ch.old, target_dependency)):
                res.rejections.append(Finding(
                    "dependency_stubbed", ch.path,
                    f"remove the code in {ch.path} that makes `{target_dependency}` optional or fakes it "
                    f"({'; '.join(new_stubs)}): never stub, mock or make the target dependency optional in "
                    "library code"))
        name = Path(ch.path).name
        if name == "conftest.py":
            if ch.old is None:
                res.rejections.append(Finding("new_conftest", ch.path, f"remove the new {ch.path}; adding conftest.py "
                                              "files is not allowed"))
                continue
        if name in PYTEST_CONFIG_FILES:
            if _pytest_config(name, ch.old) != _pytest_config(name, ch.new):
                res.rejections.append(Finding("pytest_config", ch.path, f"revert the pytest configuration change in "
                                              f"{ch.path}"))
            continue
        if ch.path.endswith(".py"):
            if _is_test_path(ch.path):
                if name == "conftest.py" and ch.new is not None and ch.old != ch.new:
                    res.review_items.append(Finding("conftest_edited", ch.path, f"existing {ch.path} edited; it applies "
                                                    "to every test under it"))
                for fx in sorted(_autouse_fixtures(ch.new) - _autouse_fixtures(ch.old)):
                    res.review_items.append(Finding("autouse_fixture", ch.path, f"autouse fixture {fx} added: it runs "
                                                    "around every test in scope", test=fx))
                _check_python(ch.path, ch.old, ch.new, res)
            continue
        if _is_test_path(ch.path):
            verb = "added" if ch.old is None else "deleted" if ch.new is None else "edited"
            res.review_items.append(Finding("fixture_data", ch.path, f"test data {verb}"))
    res.ok = not res.rejections
    return res


# --------------------------------------------------------------------------- git glue

def _git_show(cwd: Path, rev: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True)
    return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else None


def changes_from_worktree(wt_path: Path, base: str, paths: list[str]) -> list[FileChange]:
    # Non-UTF-8 (binary) files are decoded with replacement: only .py files are parsed, and the rest must not crash.
    out = []
    for path in paths:
        f = wt_path / path
        after = f.read_bytes().decode("utf-8", errors="replace") if f.exists() else None
        out.append(FileChange(path, _git_show(wt_path, base, path), after))
    return out


def guard_check(wt, diff, target_dependency: str | None = None) -> GuardResult:
    """Guard a repair: `wt` is a repair.Worktree, `diff` its repair.RepairDiff; `target_dependency` the package
    name from the DriftSpec, checked for stubbing in library code."""
    return check_changes(changes_from_worktree(wt.path, wt.base, [p for p, _, _ in diff.files]), target_dependency)


if __name__ == "__main__":
    from driftzero.repair import RepairDiff, Worktree, collect_diff

    wt_path = Path(sys.argv[1]).resolve()
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=wt_path, capture_output=True,
                               text=True).stdout.strip())
    wt = Worktree(root=root, path=wt_path, branch="", base=base, project_rel=".")
    r = guard_check(wt, collect_diff(wt))
    print("OK" if r.ok else "REJECTED")
    for f in r.rejections:
        print("  REJECT", f)
    for f in r.review_items:
        print("  review", f, f"({f.old!r} -> {f.new!r})" if f.old or f.new else "")
