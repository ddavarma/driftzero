"""Consistency eval for reviewer v2: N independent runs per PR at one reasoning effort. Nothing is posted.

    python scripts/reviewer_eval.py --runs 3 --effort high

Writes every run to docs/reviewer_eval/<task>.eval.jsonl (the reviewer's own log record, reasoning included)
and prints a JSON summary. Detection is mechanical, from quoted evidence, not from wording:
  rounding  = some finding's evidence quotes a line containing `round(`
  defaults  = some finding's evidence quotes the `health: float = 1.0` line or a defaulted `covariance:` line
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from driftzero import reviewer  # noqa: E402

TASKS = {
    "20261030-023010-e61999": "DriftZero PR #1",
    "20261030-031042-391b51": "fleet-telemetry-v1 PR #1",
}
OUT = ROOT / "docs" / "reviewer_eval"


def detect(findings: list[dict]) -> dict:
    rounding = any("round(" in f["evidence"] for f in findings)
    defaults = any(re.search(r"health:\s*float\s*=\s*1\.0", f["evidence"]) or
                   re.search(r"covariance:[^\n=]*=", f["evidence"]) for f in findings)
    return {"rounding": rounding, "defaults": bool(defaults)}


def one(task: str, run: int, effort: str, inputs: dict) -> dict:
    res, diff, guard_items, changelog, ctx = inputs[task]
    out = reviewer.review(diff, guard_items, res.spec, changelog, effort=effort, test_context=ctx,
                          log_path=OUT / f"{task}.eval.jsonl", task_id=task)
    findings = [f.model_dump() for f in (out.result.findings if out.result else [])]
    return {"task": task, "pr": TASKS[task], "run": run, "effort": effort, "verdict": out.verdict_label,
            "status": out.status, "categories": sorted(f"{f['severity']}:{f['category']}" for f in findings),
            **detect(findings), "usd": out.cost.usd, "output_tokens": out.cost.output_tokens,
            "latency_s": out.latency_s, "findings": findings}


if __name__ == "__main__":
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 3
    effort = sys.argv[sys.argv.index("--effort") + 1] if "--effort" in sys.argv else "high"
    OUT.mkdir(parents=True, exist_ok=True)
    # Inputs are built once per task, sequentially: loading a run re-runs the guard, which touches the worktree's
    # git index, so it must not run concurrently. Only the model calls run in parallel.
    inputs = {}
    for t in TASKS:
        res, diff, guard_items, changelog = reviewer.inputs_for_task(ROOT, t)
        inputs[t] = (res, diff, guard_items, changelog, reviewer.test_context_for(res, diff))
    jobs = [(t, r) for t in TASKS for r in range(1, runs + 1)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda tr: one(tr[0], tr[1], effort, inputs), jobs))
    (OUT / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps([{k: v for k, v in r.items() if k != "findings"} for r in results], indent=1))
