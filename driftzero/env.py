"""The target's Python environment: one install plan, used by the repair venv (uv) and the verify sandbox (pip).

The plan installs whatever the project declares, together with the DriftSpec's target dependency pin, so a
manifest that still pins the old version fails loudly instead of silently winning:
  - requirements*.txt files (all of them, in one resolver call, plus the pin),
  - the project itself, editable, if it has pyproject.toml / setup.py / setup.cfg (plus the pin),
  - the pin alone, if the project declares nothing,
  - pytest, and any extra requirements (verify adds the hold-outs' own requirements).

Smoke test:  python -m driftzero.env <project_dir> [requirement]
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg")


def _req_name(line: str) -> str | None:
    from packaging.requirements import InvalidRequirement, Requirement

    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    try:
        return Requirement(line).name.lower().replace("_", "-")
    except InvalidRequirement:
        return None


def declared_requirements(project: Path) -> list[str]:
    """Requirement strings from requirements*.txt and pyproject.toml [project].dependencies."""
    import tomllib

    project = Path(project)
    out: list[str] = []
    for f in sorted(project.glob("requirements*.txt")):
        out += [ln.split("#", 1)[0].strip() for ln in f.read_text().splitlines() if _req_name(ln)]
    pp = project / "pyproject.toml"
    if pp.is_file():
        try:
            out += list(tomllib.loads(pp.read_text()).get("project", {}).get("dependencies", []) or [])
        except tomllib.TOMLDecodeError:
            pass
    return out


def install_plan(project: Path, target_requirement: str | None = None, extra: list[str] | tuple = (),
                 *, lenient: bool = False) -> list[list[str]]:
    """Argument lists for `pip install` (each run from the project directory).

    strict (verify): install exactly what the project declares, together with the target pin, so a manifest that
    still pins the old version fails to resolve. lenient (the repair venv, built before the repair): install the
    declared requirements minus any that name the target package, plus the pin, and the project without its deps.
    """
    project = Path(project)
    pin = [target_requirement] if target_requirement else []
    has_manifest = any((project / m).is_file() for m in MANIFESTS)
    steps: list[list[str]] = []
    if lenient:
        target = _req_name(target_requirement) if target_requirement else None
        deps = [r for r in declared_requirements(project) if _req_name(r) != target]
        if deps or pin:
            steps.append(deps + pin)
        if has_manifest:
            steps.append(["--no-deps", "-e", "."])
    else:
        reqs = sorted(p.name for p in project.glob("requirements*.txt") if p.is_file())
        if reqs:
            steps.append([a for r in reqs for a in ("-r", r)] + pin)
        if has_manifest:
            steps.append(["-e", "."] + pin)
        if not steps and pin:
            steps.append(list(pin))
    steps.append(["pytest", *extra])
    return steps


@dataclass
class EnvResult:
    python: str
    steps: list[dict] = field(default_factory=list)
    ok: bool = True
    seconds: float = 0.0

    def failure(self) -> str | None:
        bad = next((s for s in self.steps if s["exit_code"]), None)
        return None if bad is None else f"`pip install {' '.join(bad['args'])}` exit {bad['exit_code']}: {bad['output'][-1500:]}"


def build_repair_env(project: Path, venv: Path, target_requirement: str | None = None,
                     python_version: str = "3.11") -> EnvResult:
    """A fresh uv venv with the project's declared dependencies and the target pin, for Muse's test runs."""
    start = time.monotonic()
    venv = Path(venv)
    subprocess.run(["uv", "venv", "-q", "--clear", "-p", python_version, str(venv)], check=True, capture_output=True)
    py = str(venv / "bin" / "python")
    res = EnvResult(python=py)
    for args in install_plan(project, target_requirement, lenient=True):
        p = subprocess.run(["uv", "pip", "install", "-q", "-p", py, *args], cwd=project, capture_output=True, text=True)
        res.steps.append({"args": args, "exit_code": p.returncode, "output": (p.stdout + p.stderr)[-3000:]})
        if p.returncode:
            res.ok = False
            break
    res.seconds = round(time.monotonic() - start, 1)
    return res


if __name__ == "__main__":
    proj = Path(sys.argv[1])
    for lenient in (True, False):
        print("repair venv (lenient):" if lenient else "verify (strict):")
        for step in install_plan(proj, sys.argv[2] if len(sys.argv) > 2 else None, lenient=lenient):
            print("  pip install", " ".join(step))
