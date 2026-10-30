"""repair -> guard -> verify, retried with the failure fed back (max 3 iterations).

One worktree for the whole loop; each retry runs Muse again in it with the previous
failure in the prompt. The guard runs first: a rejected diff skips the Modal sandbox.
The loop's own log is `.driftzero/runs/<id>.jsonl` (`start`, one `iteration` record per
pass with tokens and USD, `end`); Muse's raw events for pass n go to `<id>.muse-i<n>.jsonl`.

Retry feedback: guard rejections verbatim; the agent's own pytest output (first 150 lines);
hold-out failures redacted to test name + one-line assertion message, never paths, line
numbers or test bodies.

Smoke test (free: echo provider + local verify):
    python -m driftzero.loop benchmarks/robotics_telemetry_drift benchmarks/robotics_telemetry_drift/driftspec_v2.json --dry-run
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from driftzero import repair as repair_mod
from driftzero import target as target_mod
from driftzero import verify as verify_mod
from driftzero.blast_radius import BlastRadius, analyze
from driftzero.guard import GuardResult, guard_check
from driftzero.spec import DriftSpec
from driftzero.telemetry import Cost, cost_from_usage

FAILURE_HEAD_LINES = 150


@dataclass
class LoopResult:
    task_id: str
    worktree: repair_mod.Worktree
    spec: DriftSpec
    blast_radius: BlastRadius
    passed: bool
    iterations: list[dict]
    log_path: Path
    cost: Cost
    wall_time_s: float
    model: str
    steps: int
    answer: repair_mod.RepairAnswer | None = None
    guard: GuardResult | None = None
    verify: verify_mod.VerifyResult | None = None
    hidden_holdout_files: list[str] = field(default_factory=list)
    ended_at: float | None = None  # unix time the loop finished (the PR step's clock starts here)
    target_url: str | None = None   # set when --repo was a URL
    target_slug: str | None = None  # owner/name, for gh pr create -R
    diff_cost: Cost | None = None   # the differ's model call, when the spec was generated
    command: str | None = None      # the CLI command that started the run (for "Reproduce")
    review: object | None = None    # reviewer.ReviewOutcome, set after the PR is opened (latest pass)
    review_fix: dict | None = None  # the --review-fix pass: first review's findings, responses, commits, outcome
    settings: dict = field(default_factory=dict)  # holdout, local, dry_run, contributor, model, effort, max_steps


def _head(text: str, n: int = FAILURE_HEAD_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:n]) + (f"\n... ({len(lines) - n} more lines)" if len(lines) > n else "")


def build_prior_failure(result: verify_mod.VerifyResult) -> str:
    """Failure context for the next repair pass after a failed verification."""
    parts: list[str] = []
    h, a = result.holdout, result.agent
    if h.exit_code != 0:
        parts.append(f"### Hidden contract tests: {h.passed_count}/{h.tests} passed")
        parts.append("These check the v2 contract and are not in your worktree. Failing tests (name: message):")
        failures = h.failures_redacted()
        parts += [f"- {name}: {msg}" for name, msg in failures] or [f"- the suite failed to run (exit {h.exit_code})"]
        parts.append("")
    if a.exit_code != 0:
        parts += [f"### Your test suite: exit {a.exit_code}, {a.passed_count}/{a.tests} passed",
                  _head(a.stdout + a.stderr), ""]
    if result.setup.get("exit_code"):
        parts += ["### `pip install -e .` failed", result.setup.get("output", ""), ""]
    return "\n".join(parts)


def repair_until_verified(
    repo: str | Path,
    spec: DriftSpec | str | Path,
    max_iterations: int = 3,
    *,
    holdout_dir: str | Path | None = None,
    local: bool = False,
    dry_run: bool = False,
    contributor: bool = False,
    model: str = repair_mod.DEFAULT_MODEL,
    max_steps: int = repair_mod.DEFAULT_MAX_STEPS,
    effort: str = repair_mod.DEFAULT_EFFORT,
    on_line: Callable[[str], None] = lambda s: print(s, flush=True),
    home: Path | None = None,
    targets_file: Path | None = None,
    diff_info: dict | None = None,
    command: str | None = None,
) -> LoopResult:
    """`repo` is a local path or a git URL (cloned under <home>/.driftzero/clones/<id>).

    `home` is where .driftzero/ state lives: DriftZero's checkout for URL targets, the repo's own
    root for local ones. `diff_info` ({"cost": Cost, ...}) records a differ run that produced `spec`.
    """
    t0 = time.monotonic()
    spec_path = None if isinstance(spec, DriftSpec) else str(Path(spec).resolve())
    spec = spec if isinstance(spec, DriftSpec) else DriftSpec.load(spec)
    task_id = repair_mod.new_task_id()
    remote = target_mod.is_url(repo)
    home = Path(home).resolve() if home else (target_mod.home_dir() if remote else None)
    target = target_mod.prepare(repo, home or Path.cwd(), task_id)
    br = analyze(target.path, spec)
    wt = repair_mod.create_worktree(target.path, task_id, home=home)
    runs = wt.runs_dir
    runs.mkdir(parents=True, exist_ok=True)
    log_path = runs / f"{task_id}.jsonl"

    if holdout_dir:
        holdout = Path(holdout_dir).resolve()
    else:
        holdout = target_mod.holdout_for(target, home or target_mod.home_dir(),
                                         **({"targets_file": targets_file} if targets_file else {}))
    inside = holdout is not None and holdout.is_relative_to(wt.root)
    hidden = repair_mod.hide_in_worktree(wt, [os.path.relpath(holdout, wt.root)]) if inside else []

    def log(event: str, **data) -> None:
        with log_path.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": event, "task_id": task_id, **data}, default=str) + "\n")

    diff_cost = (diff_info or {}).get("cost")
    if diff_info:
        with log_path.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": "diff", "task_id": task_id,
                                 **{k: v for k, v in diff_info.items() if k != "cost"},
                                 "cost": diff_cost.as_dict() if diff_cost else None}, default=str) + "\n")
    log("start", repo=str(repo), target_url=target.url, target_slug=target.slug, source_root=str(wt.root),
        home=str(wt.state_dir.parent), command=command, spec_path=spec_path, branch=wt.branch, base=wt.base, worktree=str(wt.path), dry_run=dry_run,
        local=local, model=model, contributor=contributor, max_iterations=max_iterations,
        holdout=str(holdout) if holdout else None, hidden_from_agent=len(hidden),
        impacted_symbols=len(br.symbols), impacted_tests=len(br.tests))
    on_line(f"task {task_id}: {len(br.symbols)} impacted symbols in {len(br.files)} files, {len(br.tests)} tests")
    on_line(f"worktree {wt.path} (branch {wt.branch}); {len(hidden)} hold-out file(s) hidden from the agent")

    iterations: list[dict] = []
    prior_failure: str | None = None
    passed = False
    total = Cost()
    steps = 0
    answer = guard = vres = None
    for n in range(1, max_iterations + 1):
        record, run, cost, diff, guard, vres = run_iteration(
            wt, spec, br, n, max_iterations, prior_failure=prior_failure, runs=runs, task_id=task_id,
            holdout=holdout, local=local, dry_run=dry_run, contributor=contributor, model=model,
            max_steps=max_steps, effort=effort, on_line=on_line)
        total = total + cost
        steps += run.steps
        answer = run.answer or answer
        passed = record["passed"]
        iterations.append(record)
        log("iteration", **record)
        if passed:
            break
        if not guard.ok:
            prior_failure = guard.retry_message()
        elif vres is not None:
            prior_failure = build_prior_failure(vres)

    wall = round(time.monotonic() - t0, 1)
    if diff_cost:
        total = total + diff_cost
    log("end", passed=passed, iterations=len(iterations), cost=total.as_dict(), wall_time_s=wall,
        diff_cost=diff_cost.as_dict() if diff_cost else None)
    on_line(f"{'VERIFIED' if passed else 'NOT VERIFIED'} after {len(iterations)} iteration(s), "
            f"${total.usd or 0:.4f}, {wall:.0f}s; log {log_path}")
    return LoopResult(task_id=task_id, worktree=wt, spec=spec, blast_radius=br, passed=passed, iterations=iterations,
                      log_path=log_path, cost=total, wall_time_s=wall, model=model, steps=steps, answer=answer,
                      guard=guard, verify=vres, hidden_holdout_files=hidden, ended_at=time.time(),
                      target_url=target.url, target_slug=target.slug, diff_cost=diff_cost, command=command,
                      settings={"holdout": str(holdout) if holdout else None, "local": local, "dry_run": dry_run,
                                "contributor": contributor, "model": model, "effort": effort, "max_steps": max_steps})


def run_iteration(wt, spec, br, n: int, of: int, *, prior_failure: str | None, runs: Path, task_id: str,
                  holdout: Path | None, local: bool, dry_run: bool, contributor: bool, model: str, max_steps: int,
                  effort: str, on_line: Callable[[str], None], reviewer_findings: list | None = None,
                  kind: str = "repair"):
    """One repair -> guard -> verify pass in the worktree. Returns (record, run, cost, diff, guard, verify)."""
    it_start = time.monotonic()
    label = f"iteration {n}/{of}" if kind == "repair" else f"iteration {n} (review fix)"
    on_line(f"=== {label}: repair")
    prompt = repair_mod.write_repair_prompt(spec, br, prior_failure, project_rel=wt.project_rel,
                                            out_path=runs / f"{task_id}.prompt-i{n}.md",
                                            reviewer_findings=reviewer_findings)
    run = repair_mod.run_muse(wt, prompt, contributor=contributor, run_id=f"{task_id}.muse-i{n}",
                              dry_run=dry_run, model=model, max_steps=max_steps, effort=effort, on_line=on_line)
    cost = cost_from_usage(run.usage, default_model=model, contributor=contributor, free=dry_run)
    diff = repair_mod.collect_diff(wt)
    (runs / f"{task_id}.i{n}.diff").write_text(diff.patch)

    on_line(f"=== {label}: guard")
    guard = guard_check(wt, diff)
    on_line(f"guard {'ok' if guard.ok else 'REJECTED'}: {len(guard.rejections)} rejection(s), "
            f"{len(guard.review_items)} item(s) for review")
    for f in guard.rejections:
        on_line(f"  reject {f}")

    vres = None
    if guard.ok and holdout is not None:
        on_line(f"=== {label}: verify ({'local' if local else 'modal'})")
        vres = verify_mod.verify(wt.path, holdout, local=local, project_rel=wt.project_rel, on_line=on_line)
        verify_mod.save_result(vres, runs, f"{task_id}.i{n}")
        on_line(vres.summary())
    passed = bool(guard.ok and vres and vres.passed)
    record = {
        "iteration": n,
        "kind": kind,
        "repair": {"terminal": run.terminal, "exit_code": run.exit_code, "steps": run.steps,
                   "tool_calls": len(run.commands), "duration_s": run.duration_s,
                   "answer": run.answer.model_dump() if run.answer else None},
        "cost": {**cost.as_dict(), "provider": "echo" if dry_run else "meta", "model": None if dry_run else model,
                 "contributor": contributor},
        "diff": {"files": len(diff.files), "added": diff.added, "deleted": diff.deleted},
        "guard": guard.as_dict(),
        "verify": None if vres is None else {
            "passed": vres.passed, "backend": vres.backend, "sandbox_id": vres.sandbox_id,
            "wall_time_s": vres.wall_time_s,
            "agent": {"exit_code": vres.agent.exit_code, "tests": vres.agent.tests,
                      "passed": vres.agent.passed_count, "failed": vres.agent.failed_ids()},
            "holdout": {"exit_code": vres.holdout.exit_code, "tests": vres.holdout.tests,
                        "passed": vres.holdout.passed_count, "failed": vres.holdout.failed_ids()}},
        "wall_time_s": round(time.monotonic() - it_start, 1),
        "passed": passed,
    }
    on_line(f"{label}: {'PASSED' if passed else 'failed'}; ${cost.usd or 0:.4f} "
            f"({cost.input_tokens} in, {cost.cached_pct:.0f}% cached, {cost.output_tokens} out)")
    return record, run, cost, diff, guard, vres


def review_fix_iteration(res: LoopResult, findings: list, *, on_line: Callable[[str], None] = print) -> dict:
    """One extra repair pass in the same worktree addressing reviewer findings, then guard and verify.

    Hard cap: called at most once per run (by `driftzero migrate --review-fix`). If it does not verify, the
    worktree is reset to the already-pushed commit, so the open PR is left exactly as it was.
    """
    st = res.settings
    wt = res.worktree
    n = len(res.iterations) + 1
    record, run, cost, diff, guard, vres = run_iteration(
        wt, res.spec, res.blast_radius, n, n, prior_failure=None, runs=wt.runs_dir, task_id=res.task_id,
        holdout=Path(st["holdout"]) if st.get("holdout") else None, local=st.get("local", False),
        dry_run=st.get("dry_run", False), contributor=st.get("contributor", False),
        model=st.get("model", res.model), max_steps=st.get("max_steps", repair_mod.DEFAULT_MAX_STEPS),
        effort=st.get("effort", repair_mod.DEFAULT_EFFORT), on_line=on_line, reviewer_findings=findings,
        kind="review_fix")
    res.iterations.append(record)
    res.cost = res.cost + cost
    res.steps += run.steps
    res.wall_time_s = round(res.wall_time_s + record["wall_time_s"], 1)
    reverted = False
    if record["passed"]:
        res.answer = run.answer or res.answer
        res.guard, res.verify = guard, vres
    else:
        # keep the PR on the verified first version
        subprocess.run(["git", "reset", "-q", "--hard", "HEAD"], cwd=wt.path, check=True)
        subprocess.run(["git", "clean", "-q", "-fd"], cwd=wt.path, check=True)
        reverted = True
    with res.log_path.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "event": "iteration", "task_id": res.task_id, **record,
                             "reverted": reverted}, default=str) + "\n")
    record["reverted"] = reverted
    record["answer"] = run.answer
    return record


if __name__ == "__main__":
    res = repair_until_verified(sys.argv[1], sys.argv[2], dry_run="--dry-run" in sys.argv,
                                local="--local" in sys.argv or "--dry-run" in sys.argv)
    sys.exit(0 if res.passed else 1)
