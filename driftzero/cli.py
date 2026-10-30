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
    repo: str = typer.Option(..., help="Local repo / project directory, or a git URL (https://github.com/owner/name)."),
    spec: Path = typer.Option(None, exists=True, dir_okay=False, help="DriftSpec JSON (or give the inputs below)."),
    changelog: Path = typer.Option(None, exists=True, dir_okay=False, help="Changelog; runs `driftzero diff` first."),
    openapi_v1: Path = typer.Option(None, "--openapi-v1", exists=True, dir_okay=False, help="Old OpenAPI document."),
    openapi_v2: Path = typer.Option(None, "--openapi-v2", exists=True, dir_okay=False, help="New OpenAPI document."),
    no_pr: bool = typer.Option(False, "--no-pr", help="Push the verified branch but don't open a PR."),
    max_iterations: int = typer.Option(3, help="Repair/verify attempts before giving up."),
    contributor: bool = typer.Option(False, "--contributor", help="Contributor mode (cheaper; Meta trains on it). Dev only."),
    local: bool = typer.Option(False, "--local", help="Verify locally instead of in a Modal sandbox."),
    no_review: bool = typer.Option(False, "--no-review", help="Skip the open-weights code review of the PR."),
    strict: bool = typer.Option(False, "--strict", help="Post REQUEST_CHANGES when the reviewer has a high finding."),
    reviewer_provider: str = typer.Option("baseten", help="Where Muse Spark Open runs: baseten | fireworks."),
    review_fix: bool = typer.Option(False, "--review-fix",
                                    help="If the review has a medium/high finding, run ONE extra repair iteration on it, "
                                         "re-verify, push, and review again."),
) -> None:
    """(diff ->) blast radius -> Muse repair -> guard -> sandbox verify (retrying) -> PR -> open-weights review."""
    import shlex
    import sys
    import time

    from driftzero import differ
    from driftzero import loop as loop_mod
    from driftzero import pr as pr_mod
    from driftzero import target as target_mod

    if not spec and not (changelog or (openapi_v1 and openapi_v2)):
        raise typer.BadParameter("give --spec, or --changelog and/or --openapi-v1 + --openapi-v2")
    if not target_mod.is_url(repo) and not Path(repo).is_dir():
        raise typer.BadParameter(f"--repo {repo!r} is neither a directory nor a git URL")
    t_start = time.monotonic()
    command = "driftzero " + " ".join(shlex.quote(a) for a in sys.argv[1:])
    diff_info = None
    if not spec:
        typer.echo("=== diff: changelog / OpenAPI -> DriftSpec")
        res_d = differ.diff(changelog, openapi_v1, openapi_v2)
        runs = target_mod.home_dir() / ".driftzero" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        spec = runs / f"diff-{time.strftime('%Y%m%d-%H%M%S')}.driftspec.json"
        res_d.spec.dump(spec)
        n = sum(len(getattr(res_d.spec, c)) for c in differ.CATEGORIES)
        typer.echo(f"DriftSpec: {n} changes from {len(res_d.candidates)} OpenAPI candidates "
                   f"(+{len(res_d.model_added)} from the changelog, {len(res_d.restored)} restored) -> {spec}")
        typer.echo(f"differ: ${res_d.cost.usd or 0:.4f}, {res_d.cost.input_tokens} in / {res_d.cost.output_tokens} out, "
                   f"{res_d.latency_s:.1f}s")
        diff_info = {"cost": res_d.cost, "spec_path": str(spec), "candidates": len(res_d.candidates),
                     "model_added": res_d.model_added, "restored": res_d.restored, "latency_s": res_d.latency_s,
                     "inputs": {"changelog": str(changelog) if changelog else None,
                                "openapi_v1": str(openapi_v1) if openapi_v1 else None,
                                "openapi_v2": str(openapi_v2) if openapi_v2 else None}}

    res = loop_mod.repair_until_verified(repo, spec, max_iterations, local=local, contributor=contributor,
                                         diff_info=diff_info, command=command)
    if not res.passed:
        typer.echo(f"not verified after {len(res.iterations)} iteration(s); worktree kept at {res.worktree.path}")
        raise typer.Exit(1)
    pr = pr_mod.open_pr(res, create=not no_pr, spec_path=str(spec))
    typer.echo(f"pushed {pr.branch} @ {pr.commit[:10]} to {res.target_url or 'origin'}")
    typer.echo(f"PR: {pr.url}" if pr.url else "PR: not created (--no-pr)")
    if pr.url and not no_review:
        typer.echo(f"=== review: Muse Spark Open on {reviewer_provider}")
        cl_text = Path(changelog).read_text() if changelog else None
        posted = pr_mod.review_pr(res, pr.url, provider=reviewer_provider, strict=strict, changelog=cl_text)
        rv = res.review
        verdict = f"{rv.result.verdict}, {len(rv.result.findings)} finding(s)" if rv.result else f"failed: {rv.error}"
        typer.echo(f"reviewer: {verdict}; ${rv.cost.usd or 0:.4f}, {rv.latency_s:.0f}s; posted {posted['event']} "
                   f"({posted['inline']} inline) {posted['url']}")
        if rv.cost.usd:
            res.cost = res.cost + rv.cost
        pr_mod.update_body(res, pr.url, pr_s=pr.pr_s, spec_path=str(spec))
        actionable = rv.result is not None and any(f.severity in ("medium", "high") for f in rv.result.findings)
        if review_fix and actionable:
            typer.echo("=== review fix: one extra repair iteration on the reviewer's findings")
            info = pr_mod.apply_review_fix(res, pr.url, rv, provider=reviewer_provider, strict=strict,
                                           changelog=cl_text)
            sr = info.get("second_review")
            typer.echo(f"review fix: {'verified, pushed ' + info['commit'][:10] if info['verified'] else 'not verified, reverted'}"
                       + (f"; second review: {sr['verdict']}, {sr['findings']} finding(s)" if sr else ""))
            pr_mod.update_body(res, pr.url, pr_s=pr.pr_s, spec_path=str(spec))
        elif review_fix:
            typer.echo("review fix: nothing medium/high to address")
    usd = "n/a" if res.cost.usd is None else f"${res.cost.usd:.4f}"
    typer.echo(f"{len(res.iterations)} iteration(s), {usd} total, {time.monotonic() - t_start:.0f}s wall end to end")


@app.command()
def review(
    task: str = typer.Option(..., help="Task id of a finished run."),
    pr: str = typer.Option(None, help="PR URL to post the review on (and refresh its body)."),
    reviewer_provider: str = typer.Option("baseten", help="baseten | fireworks."),
    effort: str = typer.Option("high", help="reasoning_effort for Muse Spark Open."),
    reuse: bool = typer.Option(False, "--reuse", help="Post the most recent logged review instead of calling the model."),
    strict: bool = typer.Option(False, "--strict", help="REQUEST_CHANGES when a finding is high severity."),
) -> None:
    """Open-weights code review of a finished run's diff; optionally post it on the PR."""
    from driftzero import pr as pr_mod
    from driftzero import reviewer as reviewer_mod
    from driftzero.target import home_dir

    home = home_dir()
    res, spec_path = pr_mod.load_run(home, task)
    runs = home / ".driftzero" / "runs"
    outcome = res.review if reuse else None
    if reuse and outcome is None:
        raise typer.BadParameter(f"no logged review for {task}")
    if outcome is None:
        _, d, g, cl = reviewer_mod.inputs_for_task(home, task)
        outcome = reviewer_mod.review(d, g, res.spec, cl, provider=reviewer_provider, effort=effort,
                                      log_path=runs / f"{task}.review.jsonl", task_id=task,
                                      test_context=reviewer_mod.test_context_for(res, d))
    rv = outcome
    typer.echo(json.dumps({"verdict": rv.verdict_label, "status": rv.status, "error": rv.error,
                           "test_context": rv.test_context,
                           "model": rv.model, "effort": rv.effort, "cost": rv.cost.as_dict(), "latency_s": rv.latency_s,
                           "findings": [f.model_dump() for f in (rv.result.findings if rv.result else [])]}, indent=2))
    if pr:
        posted = pr_mod.review_pr(res, pr, outcome=outcome, strict=strict)
        typer.echo(f"posted {posted['event']} ({posted['inline']} inline): {posted['url']}")
        body, _ = pr_mod.regenerate(home, task, pr)
        typer.echo("PR body refreshed with the review findings")


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
    runs = wt.runs_dir
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
