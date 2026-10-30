"""DriftZero command line. `migrate` lands with stage 8."""
from __future__ import annotations

from pathlib import Path

import typer

from driftzero import providers
from driftzero.blast_radius import analyze, render
from driftzero.spec import DriftSpec

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Autonomous API-drift repair agent."""


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


if __name__ == "__main__":
    app()
