"""Stage 5: independent verification of a repair, in a Modal sandbox (or locally with --local).

Two pytest runs, as separate commands:
  1. the agent's suite: `python -m pytest -q --junitxml=agent.xml tests/`
  2. hold-out contract tests the agent never saw: `holdout_v2/`, uploaded from the *source*
     checkout (not the worktree), run with `--noconftest -c /dev/null` so nothing the
     agent wrote (a conftest.py, pytest addopts in pyproject) can influence them.
`passed` is true only if both exit 0.

Smoke test:  python -m driftzero.verify .driftzero/wt-<id> [--local]
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel

PY_VERSION = "3.11"
SANDBOX_TIMEOUT_S = 15 * 60
CMD_TIMEOUT_S = 10 * 60
REMOTE_ROOT = "/work"
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\w.\-]*/)+[\w.\-]+(?:\.py)?(?::\d+)?|[\w\-]+\.py(?::\d+)?")
_LINE_RE = re.compile(r",? ?\bline \d+\b")
TAR_EXCLUDE = {"__pycache__", ".pytest_cache", ".venv", "node_modules", ".mypy_cache"}


class SuiteResult(BaseModel):
    name: str
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    junit_xml: str | None
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0

    @property
    def passed_count(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped

    def cases(self) -> list[tuple[str, str]]:
        """(test name, "passed" | "failed" | "error" | "skipped") for every test case in the JUnit report."""
        if not self.junit_xml:
            return []
        out = []
        for case in ET.fromstring(self.junit_xml).iter("testcase"):
            status = next((tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None), None)
            out.append((case.get("name") or "?", {"failure": "failed", None: "passed"}.get(status, status)))
        return out

    def failures_redacted(self) -> list[tuple[str, str]]:
        """(test name, one-line message) per failing test: no paths, line numbers, module names or bodies."""
        if not self.junit_xml:
            return []
        out = []
        for case in ET.fromstring(self.junit_xml).iter("testcase"):
            node = case.find("failure")
            if node is None:
                node = case.find("error")
            if node is None:
                continue
            msg = (node.get("message") or node.tag).strip().splitlines()[0] if (node.get("message") or "").strip() \
                else node.tag
            msg = _PATH_RE.sub("<path>", msg)
            msg = _LINE_RE.sub("", msg)
            out.append((case.get("name") or "?", msg[:200]))
        return out

    def failed_ids(self) -> list[str]:
        if not self.junit_xml:
            return []
        out = []
        for case in ET.fromstring(self.junit_xml).iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                out.append(f"{case.get('classname')}::{case.get('name')}")
        return out


class VerifyResult(BaseModel):
    passed: bool
    backend: str  # "modal" | "local"
    project_rel: str
    agent: SuiteResult
    holdout: SuiteResult
    setup: dict  # install step: command, exit_code, output tail
    wall_time_s: float
    sandbox_id: str | None = None

    def summary(self) -> str:
        a, h = self.agent, self.holdout
        return (f"{'PASS' if self.passed else 'FAIL'} [{self.backend}] agent {a.passed_count}/{a.tests} (exit {a.exit_code}), "
                f"holdout {h.passed_count}/{h.tests} (exit {h.exit_code}), {self.wall_time_s:.1f}s")


def _junit_counts(xml: str | None) -> dict:
    if not xml:
        return {}
    root = ET.fromstring(xml)
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        return {}
    return {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")} | {
        "duration_s": float(suite.get("time", 0.0))}


def _suite(name: str, cmd: list[str], code: int, out: str, err: str, xml: str | None) -> SuiteResult:
    return SuiteResult(name=name, command=cmd, exit_code=code, stdout=out, stderr=err, junit_xml=xml,
                       **_junit_counts(xml))


def pytest_commands(xml_dir: str, holdout_name: str = "holdout_v2") -> tuple[list[str], list[str]]:
    """The two verification commands, run from the project directory."""
    agent = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml_dir}/agent.xml", "tests/"]
    holdout = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest", "-c", "/dev/null",
               "--rootdir", ".", f"--junitxml={xml_dir}/holdout.xml", f"{holdout_name}/"]
    return agent, holdout


def main_root(worktree: Path) -> Path:
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=worktree,
                            check=True, capture_output=True, text=True).stdout.strip()
    return Path(common).parent


def resolve_holdout(worktree: str | Path, holdout_dir: str | Path | None = None) -> tuple[Path, str]:
    """Hold-out dir in the source checkout, and the project path (relative to the repo root) it belongs to."""
    worktree = Path(worktree).resolve()
    root = main_root(worktree)
    if holdout_dir is None:
        found = sorted(p for p in root.rglob("holdout_*") if p.is_dir() and ".driftzero" not in p.parts
                       and ".git" not in p.parts)
        if len(found) != 1:
            raise ValueError(f"pass --holdout explicitly; found {[str(p) for p in found]}")
        holdout_dir = found[0]
    holdout_dir = Path(holdout_dir).resolve()
    return holdout_dir, os.path.relpath(holdout_dir.parent, root)


def _tar_bytes(worktree: Path, holdout_dir: Path, project_rel: str, holdout_name: str | None = None) -> bytes:
    """gzip tar of the worktree (incl. its .git; in a git worktree that is a gitdir pointer file, not a
    standalone repo) with the hold-outs placed at <project>/<holdout_name>, replacing anything the agent put there."""
    holdout_arc = f"wt/{project_rel}/{holdout_name or holdout_dir.name}"

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(p in TAR_EXCLUDE or p.endswith(".egg-info") for p in parts):
            return None
        if info.name == holdout_arc or info.name.startswith(holdout_arc + "/"):
            return None  # never trust a hold-out dir from the worktree
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(worktree, arcname="wt", filter=keep)
        tar.add(holdout_dir, arcname=holdout_arc,
                filter=lambda i: None if any(p in TAR_EXCLUDE for p in Path(i.name).parts) else i)
    return buf.getvalue()


def _tail(text: str, n: int = 40) -> str:
    return "\n".join(text.splitlines()[-n:])


def verify_in_modal(worktree: str | Path, holdout_dir: str | Path | None = None, *, project_rel: str | None = None,
                    holdout_name: str | None = None, holdout_requirements: list[str] | tuple = (),
                    target_requirement: str | None = None,
                    on_line=lambda s: print(s, flush=True)) -> VerifyResult:
    """`project_rel`: the project inside the worktree; by default inferred from where the hold-outs live.

    Installs the target's declared dependencies together with `target_requirement` (strict: a stale pin fails),
    then the hold-outs' own `holdout_requirements`, before running the two suites.
    """
    import modal

    from driftzero.env import install_plan

    worktree = Path(worktree).resolve()
    holdout_dir, inferred = resolve_holdout(worktree, holdout_dir)
    project_rel = project_rel or inferred
    holdout_name = holdout_name or holdout_dir.name
    payload = _tar_bytes(worktree, holdout_dir, project_rel, holdout_name)
    project = f"{REMOTE_ROOT}/wt/{project_rel}"
    agent_cmd, holdout_cmd = pytest_commands("/tmp", holdout_name)
    plan = install_plan(worktree / project_rel, target_requirement)
    if holdout_requirements:
        plan.append(list(holdout_requirements))

    start = time.monotonic()
    image = modal.Image.debian_slim(python_version=PY_VERSION).pip_install("pytest", "numpy", "setuptools", "wheel")
    app = modal.App.lookup("driftzero-verify", create_if_missing=True)
    sb = modal.Sandbox.create(app=app, image=image, timeout=SANDBOX_TIMEOUT_S, workdir=REMOTE_ROOT)
    on_line(f"sandbox {sb.object_id} up in {time.monotonic() - start:.1f}s; uploading {len(payload) / 1024:.0f} KiB")

    def run(cmd: list[str], workdir: str) -> tuple[int, str, str]:
        p = sb.exec(*cmd, workdir=workdir, timeout=CMD_TIMEOUT_S)
        out, err = p.stdout.read(), p.stderr.read()
        return p.wait(), out, err

    try:
        sb.filesystem.write_bytes(payload, "/tmp/wt.tar.gz")
        code, out, err = run(["tar", "xzf", "/tmp/wt.tar.gz", "-C", REMOTE_ROOT], REMOTE_ROOT)
        if code:
            raise RuntimeError(f"extract failed: {err}")
        steps, icode = [], 0
        for args in plan:
            cmd = ["python", "-m", "pip", "install", "-q", "--disable-pip-version-check",
                   *(["--no-build-isolation"] if "-e" in args else []), *args]
            code, out, err = run(cmd, project)
            steps.append({"command": cmd, "exit_code": code, "output": _tail(out + err, 25)})
            on_line(f"pip install {' '.join(args)[:90]}: exit {code}")
            if code:
                icode = code
                break
        setup = {"command": [s["command"] for s in steps], "exit_code": icode,
                 "output": "\n".join(s["output"] for s in steps if s["exit_code"]) or "", "steps": steps}

        suites = []
        for name, cmd in (("agent", agent_cmd), ("holdout", holdout_cmd)):
            code, out, err = run(cmd, project)
            try:
                xml = sb.filesystem.read_bytes(f"/tmp/{name}.xml").decode()
            except Exception:  # noqa: BLE001 - pytest may die before writing the report
                xml = None
            suites.append(_suite(name, cmd, code, out, err, xml))
            on_line(f"{name}: exit {code}  {_tail(out, 1)}")
    finally:
        sb.terminate()
    wall = time.monotonic() - start
    agent, holdout = suites
    return VerifyResult(passed=agent.exit_code == 0 and holdout.exit_code == 0 and icode == 0, backend="modal",
                        project_rel=project_rel, agent=agent, holdout=holdout, setup=setup,
                        wall_time_s=round(wall, 1), sandbox_id=sb.object_id)


def verify_local(worktree: str | Path, holdout_dir: str | Path | None = None, *, project_rel: str | None = None,
                 holdout_name: str | None = None, holdout_requirements: list[str] | tuple = (),
                 target_requirement: str | None = None,
                 python: str = sys.executable, on_line=lambda s: print(s, flush=True)) -> VerifyResult:
    """Same two commands in a throwaway copy, with `python` as is: nothing is installed (pass the repair venv's
    interpreter for an external target; hold-out requirements must already be present there)."""
    worktree = Path(worktree).resolve()
    holdout_dir, inferred = resolve_holdout(worktree, holdout_dir)
    project_rel = project_rel or inferred
    holdout_name = holdout_name or holdout_dir.name
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="driftzero-verify-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(_tar_bytes(worktree, holdout_dir, project_rel, holdout_name))) as tar:
            tar.extractall(tmp, filter="data")
        project = Path(tmp) / "wt" / project_rel
        env = {**os.environ, "PYTHONPATH": str(project), "PYTHONDONTWRITEBYTECODE": "1"}
        suites = []
        for name, cmd in zip(("agent", "holdout"), pytest_commands(tmp, holdout_name)):
            cmd = [python, *cmd[1:]]
            p = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
            xml_path = Path(tmp) / f"{name}.xml"
            xml = xml_path.read_text() if xml_path.exists() else None
            suites.append(_suite(name, cmd, p.returncode, p.stdout, p.stderr, xml))
            on_line(f"{name}: exit {p.returncode}  {_tail(p.stdout, 1)}")
    agent, holdout = suites
    return VerifyResult(passed=agent.exit_code == 0 and holdout.exit_code == 0, backend="local",
                        project_rel=project_rel, agent=agent, holdout=holdout,
                        setup={"command": ["PYTHONPATH=<project>"], "exit_code": 0, "output": ""},
                        wall_time_s=round(time.monotonic() - start, 1))


def verify(worktree: str | Path, holdout_dir: str | Path | None = None, *, local: bool = False, **kw) -> VerifyResult:
    return (verify_local if local else verify_in_modal)(worktree, holdout_dir, **kw)


def save_result(result: VerifyResult, runs_dir: Path, run_id: str) -> list[Path]:
    """<id>.verify.json plus both JUnit XMLs and full pytest logs."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    paths = [runs_dir / f"{run_id}.verify.json"]
    paths[0].write_text(result.model_dump_json(indent=2) + "\n")
    for s in (result.agent, result.holdout):
        if s.junit_xml:
            p = runs_dir / f"{run_id}.{s.name}.xml"
            p.write_text(s.junit_xml)
            paths.append(p)
        p = runs_dir / f"{run_id}.{s.name}.log"
        p.write_text(f"$ {' '.join(s.command)}\n# exit {s.exit_code}\n{s.stdout}\n--- stderr ---\n{s.stderr}")
        paths.append(p)
    return paths


if __name__ == "__main__":
    wt_arg = Path(sys.argv[1])
    res = verify(wt_arg, local="--local" in sys.argv)
    print(res.summary())
