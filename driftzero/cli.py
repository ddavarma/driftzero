"""DriftZero command line: `migrate` runs the whole pipeline; the other commands run one stage."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer

from driftzero import providers
from driftzero import repair as repair_mod
from driftzero import verify as verify_mod
from driftzero.blast_radius import analyze, render
from driftzero.spec import DriftSpec

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Autonomous API-drift repair agent."""


@app.command()
def migrate(
    repo: Path = typer.Option(..., exists=True, file_okay=False, help="Repo (or project subdirectory of a repo) to migrate."),
    spec: Path = typer.Option(..., exists=True, dir_okay=False, help="DriftSpec JSON."),
    no_pr: bool = typer.Option(False, "--no-pr", help="Push the verified branch but don't open a PR."),
    max_iterations: int = typer.Option(3, help="Repair/verify attempts before giving up."),
    contributor: bool = typer.Option(False, "--contributor", help="Contributor mode (cheaper; Meta trains on it). Dev only."),
    local: bool = typer.Option(False, "--local", help="Verify locally instead of in a Modal sandbox."),
) -> None:
    """Blast radius -> Muse repair -> guard -> sandbox verify (retrying) -> push + pull request."""
    from driftzero import loop as loop_mod
    from driftzero import pr as pr_mod

    res = loop_mod.repair_until_verified(repo, spec, max_iterations, local=local, contributor=contributor)
    if not res.passed:
        typer.echo(f"not verified after {len(res.iterations)} iteration(s); worktree kept at {res.worktree.path}")
        raise typer.Exit(1)
    pr = pr_mod.open_pr(res, create=not no_pr)
    typer.echo(f"pushed {pr.branch} @ {pr.commit[:10]}")
    typer.echo(f"PR: {pr.url}" if pr.url else "PR: not created (--no-pr)")
    usd = "n/a" if res.cost.usd is None else f"${res.cost.usd:.4f}"
    typer.echo(f"{len(res.iterations)} iteration(s), {usd}, {res.wall_time_s:.0f}s end to end")


@app.command("diff")
def diff_cmd(
    changelog: Path = typer.Option(None, exists=True, dir_okay=False, help="Provider changelog (markdown)."),
    openapi_v1: Path = typer.Option(None, "--openapi-v1", exists=True, dir_okay=False, help="OpenAPI document, old."),
    openapi_v2: Path = typer.Option(None, "--openapi-v2", exists=True, dir_okay=False, help="OpenAPI document, new."),
    out: Path = typer.Option(..., help="Where to write the DriftSpec JSON."),
    compare: Path = typer.Option(None, exists=True, dir_okay=False, help="A reference DriftSpec to compare against."),
) -> None:
    """Changelog / OpenAPI v1 -> v2 into a DriftSpec (deterministic diff + one muse-spark-1.3 merge call)."""
    from driftzero import differ

    res = differ.diff(changelog, openapi_v1, openapi_v2)
    res.spec.dump(out)
    n = sum(len(getattr(res.spec, c)) for c in differ.CATEGORIES)
    typer.echo(f"{out}: {n} changes ({len(res.candidates)} OpenAPI diff candidates, {len(res.model_added)} added from "
               f"the changelog, {len(res.restored)} restored after the model dropped them)")
    typer.echo(f"differ: {res.cost.input_tokens} in / {res.cost.output_tokens} out tokens, "
               f"${res.cost.usd or 0:.4f}, {res.latency_s:.1f}s")
    if compare:
        typer.echo(differ.format_comparison(differ.compare(res.spec, DriftSpec.load(compare))))


@app.command("pr-body")
def pr_body(
    task: str = typer.Option(..., help="Task id of a finished run (.driftzero/runs/<id>.jsonl)."),
    update: str = typer.Option(None, help="PR number or URL to update with gh pr edit."),
) -> None:
    """Regenerate a run's PR body from saved data (no model calls); optionally update the PR."""
    from driftzero import pr as pr_mod

    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                               check=True).stdout.strip())
    body, path = pr_mod.regenerate(root, task, update)
    typer.echo(body)
    typer.echo(f"written to {path}" + (f"; PR {update} updated" if update else ""), err=True)


@app.command()
def ping(model: str = providers.DEFAULT_META_MODEL) -> None:
    """One tiny Meta Responses-API call to check the key and model."""
    r = providers.respond("Reply with the single word OK.", model=model)
    typer.echo(f"{r.provider}/{r.model}: {r.text!r}  tokens in={r.input_tokens} out={r.output_tokens}  {r.latency_s}s")


@app.command()
def blast(
    repo: Path = typer.Option(..., exists=True, file_okay=False, help="Repo to analyze."),
    spec: Path = typer.Option(..., exists=True, dir_okay=False, help="DriftSpec JSON."),
    as_json: bool = typer.Option(False, "--json", help="Print the full BlastRadius as JSON."),
) -> None:
    """Map the code, tests and data a DriftSpec breaks (deterministic, no LLM)."""
    br = analyze(repo, DriftSpec.load(spec))
    if as_json:
        typer.echo(br.model_dump_json(indent=2))
    else:
        render(br)


@app.command()
def repair(
    repo: Path = typer.Option(..., exists=True, file_okay=False, help="Repo (or project subdirectory of a repo) to migrate."),
    spec: Path = typer.Option(..., exists=True, dir_okay=False, help="DriftSpec JSON."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use Muse's echo provider: no model calls, no cost."),
    contributor: bool = typer.Option(False, "--contributor", help="Contributor mode (cheaper; Meta trains on it). Dev only."),
    keep: bool = typer.Option(True, "--keep/--cleanup", help="Keep the worktree for verification (dry runs always clean up)."),
    max_steps: int = typer.Option(repair_mod.DEFAULT_MAX_STEPS, help="Cap on Muse model steps."),
    effort: str = typer.Option(repair_mod.DEFAULT_EFFORT, help="Muse reasoning effort."),
) -> None:
    """Repair the blast radius with Muse Code in an isolated git worktree."""
    spec_obj = DriftSpec.load(spec)
    br = analyze(repo, spec_obj)
    task_id = repair_mod.new_task_id()
    wt = repair_mod.create_worktree(repo, task_id)
    runs = wt.root / ".driftzero" / "runs"
    typer.echo(f"task {task_id}: {len(br.symbols)} impacted symbols in {len(br.files)} files, {len(br.tests)} tests")
    typer.echo(f"worktree {wt.path} (branch {wt.branch}, base {wt.base[:10]})")
    try:
        prompt = repair_mod.write_repair_prompt(spec_obj, br, project_rel=wt.project_rel,
                                                out_path=runs / f"{task_id}.prompt.md")
        run = repair_mod.run_muse(wt, prompt, contributor=contributor, run_id=task_id, dry_run=dry_run,
                                  max_steps=max_steps, effort=effort)
        diff = repair_mod.collect_diff(wt)
        (runs / f"{task_id}.diff").write_text(diff.patch)
        summary = {
            "task_id": task_id, "dry_run": dry_run, "branch": wt.branch, "base": wt.base, "worktree": str(wt.path),
            "terminal": run.terminal, "exit_code": run.exit_code, "steps": run.steps, "duration_s": run.duration_s,
            "tokens": run.tokens, "tool_calls": len(run.commands),
            "pytest_runs": [{"step": c.step, "command": c.command, "exit_code": c.exit_code} for c in run.pytest_runs],
            "answer": run.answer.model_dump() if run.answer else None,
            "diff": {"files": diff.files, "added": diff.added, "deleted": diff.deleted},
        }
        (runs / f"{task_id}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        typer.echo(diff.stat.rstrip() or "(no changes)")
        if run.answer:
            a = run.answer
            typer.echo(f"muse says: {a.tests_passed}/{a.tests_run} tests passed, confidence {a.confidence:.2f}")
        typer.echo(f"events: {run.events_path}")
    finally:
        if dry_run or not keep:
            repair_mod.remove_worktree(wt)
    if not run.ok:
        raise typer.Exit(1)


@app.command()
def verify(
    worktree: Path = typer.Option(..., exists=True, file_okay=False, help="Repair worktree (.driftzero/wt-<id>)."),
    holdout: Path = typer.Option(None, exists=True, file_okay=False, help="Hold-out test dir in the source checkout (auto-detected)."),
    local: bool = typer.Option(False, "--local", help="Run in a local temp copy instead of a Modal sandbox."),
    run_id: str = typer.Option(None, help="Name for the saved logs (default: from the worktree name)."),
) -> None:
    """Independently run the agent's tests and the hold-out contract tests."""
    res = verify_mod.verify(worktree, holdout, local=local)
    rid = run_id or worktree.resolve().name.removeprefix("wt-")
    runs = verify_mod.main_root(worktree.resolve()) / ".driftzero" / "runs"
    saved = verify_mod.save_result(res, runs, rid)
    typer.echo(res.summary())
    for s in (res.agent, res.holdout):
        for tid in s.failed_ids():
            typer.echo(f"  FAILED [{s.name}] {tid}")
    typer.echo(f"logs: {saved[0].parent}/{rid}.verify.json (+ xml, log)")
    if not res.passed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
