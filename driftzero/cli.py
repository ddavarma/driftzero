"""DriftZero command line. `migrate` lands with stage 8; for now only a provider ping."""
from __future__ import annotations

import typer

from driftzero import providers

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Autonomous API-drift repair agent."""


@app.command()
def ping(model: str = providers.DEFAULT_META_MODEL) -> None:
    """One tiny Meta Responses-API call to check the key and model."""
    r = providers.respond("Reply with the single word OK.", model=model)
    typer.echo(f"{r.provider}/{r.model}: {r.text!r}  tokens in={r.input_tokens} out={r.output_tokens}  {r.latency_s}s")


if __name__ == "__main__":
    app()
