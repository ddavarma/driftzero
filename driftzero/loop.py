"""repair -> guard -> verify, retried with the failure fed back (max 3 iterations).

One worktree for the whole loop; each retry runs Muse again in it with the previous
verification output in the prompt. The loop's own log is `.driftzero/runs/<id>.jsonl`
(one `iteration` record per pass, plus `start` / `end`); Muse's raw events for pass n
go to `<id>.muse-i<n>.jsonl`.

Smoke test (free: echo provider + local verify):
    python -m driftzero.loop benchmarks/robotics_telemetry_drift benchmarks/robotics_telemetry_drift/driftspec_v2.json --dry-run
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from driftzero import repair as repair_mod
from driftzero import verify as verify_mod
from driftzero.blast_radius import analyze
from driftzero.spec import DriftSpec

FAILURE_HEAD_LINES = 150


@dataclass
class GuardResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def guard_check(wt: repair_mod.Worktree, diff: repair_mod.RepairDiff) -> GuardResult:
    """Placeholder until stage 6 (driftzero/guard.py): always ok."""
    return GuardResult(ok=True, reasons=["guard not implemented yet (stage 6)"])


@dataclass
class LoopResult:
    task_id: str
    worktree: repair_mod.Worktree
    passed: bool
    iterations: list[dict]
    log_path: Path


def _head(text: str, n: int = FAILURE_HEAD_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:n]) + (f"\n... ({len(lines) - n} more lines)" if len(lines) > n else "")


def build_prior_failure(result: verify_mod.VerifyResult, guard: GuardResult | None = None) -> str:
    """Failure context for the next repair pass: first 150 lines of each pytest run."""
    parts = []
    if guard and not guard.ok:
        parts += ["### Guard rejected the diff", *guard.reasons, ""]
    for s in (result.holdout, result.agent):
        label = "hold-out contract tests (not in your worktree)" if s.name == "holdout" else "your test suite"
        parts += [f"### {label}: exit {s.exit_code}, {s.passed_count}/{s.tests} passed", _head(s.stdout + s.stderr), ""]
    if result.setup.get("exit_code"):
        parts += ["### pip install -e failed", result.setup.get("output", ""), ""]
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
    max_steps: int = repair_mod.DEFAULT_MAX_STEPS,
    effort: str = repair_mod.DEFAULT_EFFORT,
    on_line: Callable[[str], None] = lambda s: print(s, flush=True),
) -> LoopResult:
    spec = spec if isinstance(spec, DriftSpec) else DriftSpec.load(spec)
    repo = Path(repo).resolve()
    br = analyze(repo, spec)
    task_id = repair_mod.new_task_id()
    wt = repair_mod.create_worktree(repo, task_id)
    runs = wt.root / ".driftzero" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    log_path = runs / f"{task_id}.jsonl"

    holdout = Path(holdout_dir).resolve() if holdout_dir else None
    if holdout is None:
        found = sorted(p for p in repo.glob("holdout_*") if p.is_dir())
        holdout = found[0] if found else None
    hidden = repair_mod.hide_in_worktree(wt, [os.path.relpath(holdout, wt.root)]) if holdout else []

    def log(event: str, **data) -> None:
        with log_path.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "event": event, "task_id": task_id, **data}, default=str) + "\n")

    log("start", repo=str(repo), branch=wt.branch, base=wt.base, worktree=str(wt.path), dry_run=dry_run,
        local=local, max_iterations=max_iterations, holdout=str(holdout) if holdout else None,
        hidden_from_agent=len(hidden), impacted_symbols=len(br.symbols), impacted_tests=len(br.tests))
    on_line(f"task {task_id}: worktree {wt.path} ({len(hidden)} hold-out files hidden from the agent)")

    iterations: list[dict] = []
    prior_failure: str | None = None
    passed = False
    for n in range(1, max_iterations + 1):
        on_line(f"=== iteration {n}/{max_iterations}")
        prompt = repair_mod.write_repair_prompt(spec, br, prior_failure, project_rel=wt.project_rel,
                                                out_path=runs / f"{task_id}.prompt-i{n}.md")
        run = repair_mod.run_muse(wt, prompt, contributor=contributor, run_id=f"{task_id}.muse-i{n}",
                                  dry_run=dry_run, max_steps=max_steps, effort=effort, on_line=on_line)
        diff = repair_mod.collect_diff(wt)
        (runs / f"{task_id}.i{n}.diff").write_text(diff.patch)
        guard = guard_check(wt, diff)

        vres = None
        if guard.ok and holdout is not None:
            vres = verify_mod.verify(wt.path, holdout, local=local, on_line=on_line)
            verify_mod.save_result(vres, runs, f"{task_id}.i{n}")
            on_line(vres.summary())
        passed = bool(guard.ok and vres and vres.passed)

        record = {
            "iteration": n,
            "repair": {"terminal": run.terminal, "exit_code": run.exit_code, "steps": run.steps,
                       "tool_calls": len(run.commands), "duration_s": run.duration_s,
                       "answer": run.answer.model_dump() if run.answer else None},
            "cost": {**run.tokens, "provider": "meta" if not dry_run else "echo",
                     "model": None if dry_run else repair_mod.DEFAULT_MODEL,
                     "usd": None},  # Meta per-token prices not configured yet
            "diff": {"files": len(diff.files), "added": diff.added, "deleted": diff.deleted},
            "guard": {"ok": guard.ok, "reasons": guard.reasons},
            "verify": None if vres is None else {
                "passed": vres.passed, "backend": vres.backend, "wall_time_s": vres.wall_time_s,
                "agent": {"exit_code": vres.agent.exit_code, "tests": vres.agent.tests, "passed": vres.agent.passed_count,
                          "failed": vres.agent.failed_ids()},
                "holdout": {"exit_code": vres.holdout.exit_code, "tests": vres.holdout.tests,
                            "passed": vres.holdout.passed_count, "failed": vres.holdout.failed_ids()}},
            "passed": passed,
        }
        iterations.append(record)
        log("iteration", **record)
        if passed:
            break
        if vres is not None:
            prior_failure = build_prior_failure(vres, guard)
        elif not guard.ok:
            prior_failure = "### Guard rejected the diff\n" + "\n".join(guard.reasons)

    totals = {k: sum(it["cost"][k] for it in iterations) for k in ("input_tokens", "cached_tokens", "output_tokens")}
    log("end", passed=passed, iterations=len(iterations), tokens=totals)
    on_line(f"{'VERIFIED' if passed else 'NOT VERIFIED'} after {len(iterations)} iteration(s); log {log_path}")
    return LoopResult(task_id=task_id, worktree=wt, passed=passed, iterations=iterations, log_path=log_path)


if __name__ == "__main__":
    res = repair_until_verified(sys.argv[1], sys.argv[2], dry_run="--dry-run" in sys.argv,
                                local="--local" in sys.argv or "--dry-run" in sys.argv)
    sys.exit(0 if res.passed else 1)
