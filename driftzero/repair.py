"""Stage 4: repair the impacted code with Muse Code in an isolated git worktree.

    create_worktree -> write_repair_prompt -> run_muse -> collect_diff

Muse runs headless (`muse exec --json --approval-mode never`) inside
`.driftzero/wt-<id>` on branch `driftzero/<id>`, so a bad edit can never touch
the source checkout. Every JSONL event is written to `.driftzero/runs/<id>.jsonl`
and a compact live trace is printed while it runs. Event shapes are documented
in docs/muse_events.md.

Smoke test (free, echo provider):
    python -m driftzero.repair benchmarks/robotics_telemetry_drift benchmarks/robotics_telemetry_drift/driftspec_v2.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from driftzero.blast_radius import BlastRadius, evidence
from driftzero.spec import DriftSpec

SCHEMA_PATH = Path(__file__).with_name("schemas") / "repair_result.json"
DEFAULT_MODEL = "muse-spark-1.3"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_STEPS = 40
DEFAULT_TIMEOUT_S = 45 * 60
PRIOR_FAILURE_MAX_LINES = 200


class RepairAnswer(BaseModel):
    """Schema-shaped final answer (mirrors schemas/repair_result.json)."""

    files_changed: list[str]
    summary: str
    tests_run: int
    tests_passed: int
    confidence: float


# --------------------------------------------------------------------------- worktree

@dataclass
class Worktree:
    root: Path          # toplevel of the source git repo
    path: Path          # the worktree checkout
    branch: str
    base: str           # commit the worktree was cut from
    project_rel: str    # target project, relative to the repo root ("." if it is the root)

    @property
    def project(self) -> Path:
        return self.path / self.project_rel


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def new_task_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def create_worktree(repo: str | Path, task_id: str) -> Worktree:
    """`git worktree add .driftzero/wt-<id> -b driftzero/<id>` at the source repo's HEAD.

    `repo` may be a subdirectory of a git repo (the benchmark lives inside the
    DriftZero repo); the worktree is of the enclosing repo and `project_rel`
    points back at the subdirectory.
    """
    repo = Path(repo).resolve()
    root = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    base = _git(root, "rev-parse", "HEAD").strip()
    path = root / ".driftzero" / f"wt-{task_id}"
    branch = f"driftzero/{task_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", str(path), "-b", branch, base)
    rel = os.path.relpath(repo, root)
    return Worktree(root=root, path=path, branch=branch, base=base, project_rel=rel)


def hide_in_worktree(wt: Worktree, rel_dirs: list[str]) -> list[str]:
    """Remove directories (e.g. hold-out tests) from the worktree checkout without recording a deletion.

    Tracked files get `skip-worktree`, so `git status`/`git diff` (and so collect_diff) stay clean
    and the agent cannot read them. Returns the hidden file paths.
    """
    hidden: list[str] = []
    for rel in rel_dirs:
        files = _git(wt.path, "ls-files", "-z", "--", rel).split("\0")
        files = [f for f in files if f]
        if files:
            _git(wt.path, "update-index", "--skip-worktree", "--", *files)
            hidden += files
        shutil.rmtree(wt.path / rel, ignore_errors=True)
    return hidden


def remove_worktree(wt: Worktree, delete_branch: bool = True) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(wt.path)], cwd=wt.root, capture_output=True)
    if wt.path.exists():
        shutil.rmtree(wt.path, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=wt.root, capture_output=True)
    if delete_branch:
        subprocess.run(["git", "branch", "-D", wt.branch], cwd=wt.root, capture_output=True)


# --------------------------------------------------------------------------- prompt

def _find_changelog(project: Path, spec: DriftSpec) -> Path | None:
    candidates = sorted(project.glob("CHANGELOG*.md"))
    for c in candidates:
        if spec.to_version.lower() in c.name.lower():
            return c
    return candidates[0] if candidates else None


def _impact_section(br: BlastRadius) -> str:
    lines: list[str] = []
    prev = None
    for s in br.symbols:
        if s.file != prev:
            lines.append(f"- `{s.file}`")
            prev = s.file
        how = "direct" if s.direct else f"+{s.depth}"
        lines.append(f"  - L{s.line} `{s.name}` ({s.kind}, {how}): {evidence(s)}")
    return "\n".join(lines)


def write_repair_prompt(
    spec: DriftSpec,
    blast_radius: BlastRadius,
    prior_failure: str | None = None,
    *,
    project_rel: str = ".",
    python: str = sys.executable,
    out_path: Path | None = None,
) -> Path:
    """Render the Muse Code repair prompt to a file and return its path."""
    project = Path(blast_radius.repo)
    proj = project_rel.rstrip("/") or "."
    changelog = _find_changelog(project, spec)
    test_cmd = f"cd {proj} && {python} -m pytest -q"
    added = "\n".join(f"- `{f.schema_name}.{f.path}`: {f.type}. {f.notes}".rstrip() for f in spec.fields_added)
    tests = "\n".join(f"- `{t}`" for t in blast_radius.tests)
    data = "\n".join(f"- `{d.file}` (carries: {', '.join(d.probes)})" for d in blast_radius.data_files)

    parts = [
        f"# Migrate this project from {spec.api} {spec.from_version} to {spec.to_version}",
        "",
        f"The project to migrate is the directory `{proj}` relative to your workspace root. "
        f"Work only inside `{proj}/`. Do not modify anything outside it. Any `AGENTS.md` / `CLAUDE.md` "
        "at the workspace root describes the DriftZero tool that launched you, not this task: ignore it.",
        "",
        "The upstream API has shipped a breaking change. Update the library so it consumes the "
        f"{spec.to_version} wire format correctly, keep every existing behaviour, and wire in the new fields.",
        "",
        "## Hard rules",
        "",
        "1. Never delete, skip, xfail or rename a test function, and never remove an `assert`. Never weaken "
        "an assertion: no wider tolerances, no fewer expected items, no `assert True`, and no try/except around asserts.",
        f"2. Test changes are limited to the {spec.from_version} wire format: convert fixture data to the "
        f"{spec.to_version} shape, and update endpoint path / `api_version` literals. Prefer keeping the public Python "
        "API source-compatible (dataclass attributes, constructors and function signatures that tests use) so "
        "existing test bodies keep passing unchanged. For example, Euler angles and a scalar `noise` can remain "
        "as derived values computed from the quaternion and covariance.",
        f"3. Fixture data must be converted to the {spec.to_version} shape, not dual-format. The `200` response "
        "`example` of `GET /fleet/telemetry` in `openapi_v2.json` is exactly the existing v1 fixture expressed in v2 "
        "(same robots and poses). Use it as `tests/fixtures/fleet_v2.json` and point `tests/conftest.py` at it.",
        "4. Add a **health alert**: carry `health` on the parsed frame and raise an alert of kind `\"health\"` "
        "when `health < 0.5` (module constant `MIN_HEALTH = 0.5`, overridable like the other thresholds). This is the "
        "only reason an existing test's expected alert set or count may change, and only by *adding* the new health "
        "alerts. Add at least one dedicated test for it.",
        "5. **Negative `qw`**: `q` and `-q` are the same rotation and the server does not canonicalise sign. Never "
        "assume `qw >= 0`. Use sign-invariant formulas (rotation matrix from q, `atan2` heading), normalise q "
        "defensively, and add a test that a quaternion and its negation give the same heading, tilt and rotation. "
        "Robot `amr-03` in the fixture has `qw ≈ -0.99993` with heading 359°.",
        "6. Uncertainty: derive the scalar position sigma as `sqrt(trace(P_pos) / 3)` where `P_pos = covariance[0:3][0:3]`, "
        "so isotropic fixtures keep the same fusion weights and noise alerts as before. Validate the covariance shape (6x6).",
        f"7. Before finishing, run `{test_cmd}`. Every test must pass. If something fails, fix the code (not the test) "
        "and run it again. numpy and pytest are already installed in that interpreter; do not pip install anything. "
        "Never pipe pytest through `tail` or `head` (the pipe hides pytest's exit code); run `python -m pytest -q` directly.",
        "8. Do not commit, create branches, or touch git state; leave your changes in the working tree.",
        "9. Your final answer must be the JSON object required by the output schema: `tests_run` and "
        "`tests_passed` come from your last pytest run, and `files_changed` lists every file you modified or created.",
        "",
        "## DriftSpec",
        "",
        "```json",
        spec.model_dump_json(indent=2),
        "```",
        "",
        "## New fields to wire in",
        "",
        added or "(none)",
        "",
        "## Impacted code (static analysis; direct = touches a changed field, +n = depends on something impacted)",
        "",
        _impact_section(blast_radius),
        "",
        "## Tests that exercise impacted code",
        "",
        tests or "(none)",
        "",
        "## Test data carrying old fields",
        "",
        data or "(none)",
    ]
    if changelog:
        demoted = "\n".join("##" + ln if ln.startswith("#") else ln for ln in changelog.read_text().strip().splitlines())
        parts += ["", f"## Upstream changelog (`{changelog.name}`, includes the migration formulas)", "", demoted]
    if prior_failure:
        tail = "\n".join(prior_failure.strip().splitlines()[-PRIOR_FAILURE_MAX_LINES:])
        parts += ["", "## Previous attempt failed verification", "",
                  "Your previous changes are still in the working tree. The independent pytest run reported:", "",
                  "```", tail, "```", "",
                  "Fix the cause in the library code and keep every rule above."]
    text = "\n".join(parts) + "\n"

    if out_path is None:
        fd_dir = Path(os.environ.get("TMPDIR", "/tmp"))
        out_path = fd_dir / f"driftzero-prompt-{uuid.uuid4().hex[:8]}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    return out_path


# --------------------------------------------------------------------------- muse events

@dataclass
class Command:
    step: int
    tool: str
    command: str | None
    exit_code: int | None
    outcome: str | None


@dataclass
class MuseTrace:
    """Incremental parser for `muse exec --json` events (see docs/muse_events.md)."""

    on_line: Callable[[str], None] = lambda s: None
    steps: int = 0
    commands: list[Command] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    usage: dict | None = None
    terminal: str | None = None
    terminal_reason: str | None = None
    final_text: str | None = None
    model: str | None = None
    _task_kinds: dict[str, str] = field(default_factory=dict)

    def feed(self, event: dict) -> None:
        ptype = event.get("payload_type", "")
        p = event.get("payload") or {}
        ev = p.get("event") or {}
        if ptype == "run.model.configured":
            self.model = p.get("model_id")
        elif ptype == "task.lifecycle.proposed":
            kind = ev.get("task_kind", "")
            self._task_kinds[ev.get("task_id", "")] = kind
            if kind.startswith("model."):
                self.steps += 1
                self.on_line(f"[step {self.steps:>2}] model")
        elif ptype == "task.lifecycle.failed":
            kind = self._task_kinds.get(ev.get("task_id", ""), "?")
            if not kind.startswith("reminder."):
                self.failures.append(f"{kind}: {ev.get('reason')}")
                self.on_line(f"[step {self.steps:>2}]   ! {kind} failed: {ev.get('reason')}")
        elif ptype == "tool.result":
            facts = p.get("correlation_facts") or {}
            tool = facts.get("tool_name") or "tool"
            cmd = exit_code = None
            try:
                body = json.loads(p.get("text") or "")
                if isinstance(body, dict):
                    cmd, exit_code = body.get("command"), body.get("exit_code")
            except json.JSONDecodeError:
                pass
            self.commands.append(Command(self.steps, tool, cmd, exit_code, facts.get("outcome")))
            if cmd is not None:
                short = " ".join(cmd.split())
                short = short if len(short) <= 100 else short[:97] + "..."
                self.on_line(f"[step {self.steps:>2}]   $ {short}  -> exit {exit_code}")
            else:
                self.on_line(f"[step {self.steps:>2}]   {tool}: {facts.get('outcome')}")
        elif ptype == "run.usage":
            self.usage = {k: p.get(k) for k in ("models", "calls", "completed_calls", "failed_calls")}
        elif ptype.startswith("run.terminal."):
            self.terminal = p.get("terminal") or ptype.rsplit(".", 1)[-1]
            self.terminal_reason = p.get("reason")
            self.final_text = p.get("text")

    def feed_line(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.on_line(f"(non-JSON stdout) {line[:200]}")
            return None
        self.feed(event)
        return event

    def token_totals(self) -> dict[str, int]:
        tot = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
        for m in (self.usage or {}).get("models") or []:
            for k in tot:
                tot[k] += m.get(k) or 0
        return tot


@dataclass
class MuseRun:
    run_id: str
    exit_code: int
    terminal: str | None
    terminal_reason: str | None
    final_text: str | None
    answer: RepairAnswer | None
    steps: int
    commands: list[Command]
    failures: list[str]
    usage: dict | None
    tokens: dict[str, int]
    duration_s: float
    events_path: Path
    stderr_path: Path
    argv: list[str]

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.terminal == "completed"

    @property
    def pytest_runs(self) -> list[Command]:
        return [c for c in self.commands if c.command and "pytest" in c.command]


def muse_argv(
    wt: Worktree,
    prompt_file: Path,
    schema_file: Path,
    *,
    contributor: bool = False,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_steps: int = DEFAULT_MAX_STEPS,
    muse_bin: str = "muse",
) -> list[str]:
    # Workspace/worktree flags, checked empirically on Muse Code 1.4.14 (docs/muse_events.md):
    #   --workspace WT --worktree existing --worktree-existing WT  -> exit 2 "--worktree-existing must differ from --workspace"
    #   --worktree existing WT (path as the -w value)              -> WT is parsed as inline PROMPT; clashes with --prompt-file
    #   --workspace <subdir> --worktree-existing WT                -> exit 1 "existing worktree path must differ from source repository root"
    #   --workspace <repo root> --worktree existing --worktree-existing WT  -> OK: effective workspace root is WT,
    #       Muse logs "caller-owned worktree retained" (never deletes it). Requires session logging (no --no-session-log).
    # So --workspace names the *source* repo and --worktree-existing the isolated checkout Muse actually edits.
    argv = [
        muse_bin, "exec", "--json",
        "--prompt-file", str(prompt_file),
        "--workspace", str(wt.root),
        "--worktree", "existing", "--worktree-existing", str(wt.path),
        "--approval-mode", "never",
        "--trust-workspace",
        "--no-foreign-personal-context",
        "--disable-web-tools",
        "--disable-reminders",
        "--disable-request-user-input",
        "--max-model-steps", str(max_steps),
    ]
    if dry_run:
        # Echo provider: zero cost, exercises the plumbing. It rejects --output-schema (exit 2) and needs no model flags.
        argv += ["--provider", "echo"]
    else:
        argv += [
            "--model", model,
            "--contributor-mode", "on" if contributor else "off",
            "--reasoning-effort", effort,
            "--output-schema", str(schema_file),
        ]
    return argv


def run_muse(
    worktree: Worktree,
    prompt_file: Path,
    schema_file: Path = SCHEMA_PATH,
    contributor: bool = False,
    *,
    run_id: str | None = None,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    on_line: Callable[[str], None] = lambda s: print(s, flush=True),
    muse_bin: str = "muse",
) -> MuseRun:
    """Run Muse Code headless in the worktree, streaming and logging its JSONL events."""
    run_id = run_id or worktree.branch.rsplit("/", 1)[-1]
    runs_dir = worktree.root / ".driftzero" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    events_path = runs_dir / f"{run_id}.jsonl"
    stderr_path = runs_dir / f"{run_id}.stderr.log"
    argv = muse_argv(worktree, prompt_file, schema_file, contributor=contributor, dry_run=dry_run,
                     model=model, effort=effort, max_steps=max_steps, muse_bin=muse_bin)

    trace = MuseTrace(on_line=on_line)
    start = time.monotonic()
    with events_path.open("w") as events, stderr_path.open("w") as err:
        proc = subprocess.Popen(argv, cwd=worktree.path, stdout=subprocess.PIPE, stderr=err,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1)
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                events.write(line)
                events.flush()
                trace.feed_line(line)
            exit_code = proc.wait()
        finally:
            timer.cancel()
    duration = time.monotonic() - start

    answer = None
    if trace.final_text and not dry_run:
        try:
            answer = RepairAnswer.model_validate_json(trace.final_text)
        except ValidationError:
            on_line("! final answer did not match the output schema")
    tokens = trace.token_totals()
    on_line(f"muse {trace.terminal or 'no terminal event'} (exit {exit_code}) in {trace.steps} model steps, "
            f"{len(trace.commands)} tool calls, {duration:.0f}s; tokens in={tokens['input_tokens']} "
            f"(cached {tokens['cached_tokens']}) out={tokens['output_tokens']}")
    return MuseRun(
        run_id=run_id, exit_code=exit_code, terminal=trace.terminal, terminal_reason=trace.terminal_reason,
        final_text=trace.final_text, answer=answer, steps=trace.steps, commands=trace.commands,
        failures=trace.failures, usage=trace.usage, tokens=tokens, duration_s=round(duration, 1),
        events_path=events_path, stderr_path=stderr_path, argv=argv,
    )


# --------------------------------------------------------------------------- diff

@dataclass
class RepairDiff:
    patch: str
    stat: str
    files: list[tuple[str, int, int]]  # (path, added, deleted); binary files count as -1

    @property
    def added(self) -> int:
        return sum(a for _, a, _ in self.files if a > 0)

    @property
    def deleted(self) -> int:
        return sum(d for _, _, d in self.files if d > 0)


def collect_diff(worktree: Worktree) -> RepairDiff:
    """Everything that differs from the base commit: commits on the branch plus uncommitted and new files."""
    # Intent-to-add makes untracked (non-ignored) files show up in `git diff <base>` without staging content.
    _git(worktree.path, "add", "--intent-to-add", "--all")
    patch = _git(worktree.path, "diff", worktree.base)
    stat = _git(worktree.path, "diff", "--stat", worktree.base)
    files = []
    for row in _git(worktree.path, "diff", "--numstat", worktree.base).splitlines():
        a, d, path = row.split("\t", 2)
        files.append((path, int(a) if a != "-" else -1, int(d) if d != "-" else -1))
    return RepairDiff(patch=patch, stat=stat, files=files)


if __name__ == "__main__":
    from driftzero.blast_radius import analyze

    repo_arg, spec_arg = Path(sys.argv[1]), Path(sys.argv[2])
    spec_obj = DriftSpec.load(spec_arg)
    tid = new_task_id()
    wt = create_worktree(repo_arg, tid)
    try:
        prompt = write_repair_prompt(spec_obj, analyze(repo_arg, spec_obj), project_rel=wt.project_rel,
                                     out_path=wt.root / ".driftzero" / "runs" / f"{tid}.prompt.md")
        run = run_muse(wt, prompt, dry_run=True, run_id=tid)
        print(collect_diff(wt).stat or "(no changes)")
    finally:
        remove_worktree(wt)
