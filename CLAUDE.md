# DriftZero

Autonomous API-drift repair agent. Solo Indie-track entry, Meta Global AI Developer Hackathon.
Submission deadline: **Wed Nov 4, 2026, 9:00 PM ET** (3-minute demo video + public repo + CV platform form).
Builder has qualifying exams Nov 13, so nothing here may spill past Nov 4.

## What DriftZero does (one sentence)

Given a target repo and a description of an upstream API change (changelog text, OpenAPI v1→v2 diff, or a runtime error), it maps the impacted code, repairs it in an isolated git worktree with Meta's models, proves the repair in a Modal sandbox with `pytest`, and opens a GitHub pull request with the verification log attached.

## Hackathon rules that constrain every decision

- Repo must be public; everything demoed must be built Oct 26–Nov 4 (first commit a8b97b8 is the start marker).
- Project must use Meta models. Primary: Meta Model API. Open-weights bonus ($10K) requires Muse Spark Open served on Baseten or Fireworks.
- Judging: Impact 30%, Use of Meta tooling 25%, Demo 25%, Technical depth 20%. The video is the product.
- Never commit `.env`. Never print full API keys.

## Verified providers (smoke-tested Oct 30; do not substitute ids from memory)

| Provider | base_url | model ids on this account |
|---|---|---|
| Meta Model API ($150) | `https://api.meta.ai/v1`, key in `META_API_KEY` | `muse-spark-1.3`, `muse-spark-1.3-contributor`, `muse-wonder-1.0`, `muse-image-1.0`, `muse-voice-transcribe-1.0`, `sam-3.1` |
| Baseten ($100, 2M TPM) | `https://inference.baseten.co/v1`, key in `BASETEN_API_KEY` | `meta-models/muse-spark-open-1026` only. Reasoning text arrives in `message.reasoning_content`; final answer in `message.content`. Accepts `extra_body={"reasoning_effort": ...}`. |
| Fireworks ($100) | `https://api.fireworks.ai/inference/v1`, key in `FIREWORKS_API_KEY` | `accounts/fireworks/models/muse-spark-open` |
| Modal ($280, profile `ddavarma2609`) | `modal.Sandbox.create(...)` | debian_slim py3.11 images; sandbox exec verified |
| Muse Code CLI 1.4.14 | `muse exec --json [--model M] [--reasoning-effort E] [--approval-mode never] "<prompt>"` | bills `META_API_KEY`; emits JSONL events on stdout |

Known gotchas:
- Every Muse model reasons before answering. Give `max_tokens` real headroom (≥ 2000 for anything non-trivial) or the content comes back empty.
- Meta's docs recommend the Responses API (`client.responses.create`, read `output_text`) over chat completions. Prefer it for Meta calls.
- Baseten Spark Open: `json_schema` structured output is unconfirmed. Use JSON mode plus the schema in the prompt and validate with Pydantic.
- Contributor tier (`muse-spark-1.3-contributor`) is 12x cheaper but Meta trains on it. Use it for dev iteration only.
- Headless `muse exec` cannot answer approval prompts; always pass `--approval-mode never` (or `--disable-approval`) and run it inside a worktree so a bad edit can't touch `main`.

## Scope

### v0, must be green by Sun Nov 1 night (one scenario, CLI only, no frontend)

1. `benchmarks/robotics_telemetry_drift/`: a small real Python repo (dataclasses + NumPy, 4–6 modules, ~10 pytest tests) consuming a fleet telemetry API v1 (Euler roll/pitch/yaw in degrees, scalar noise). Plus `openapi_v1.json`, `openapi_v2.json` (quaternions qx,qy,qz,qw + 6x6 covariance + health score), and `CHANGELOG_v2.md`. v1 tests pass on `main`.
2. `driftzero/differ.py`: changelog/OpenAPI diff → `DriftSpec` (Pydantic) via `muse-spark-1.3`.
3. `driftzero/blast_radius.py`: Python `ast` walk → impacted files/functions/tests for the DriftSpec. Deterministic, no LLM.
4. `driftzero/repair.py`: create `git worktree add .driftzero/wt-<id>`; run `muse exec --json --approval-mode never` with the DriftSpec + blast radius as the prompt; capture the diff.
5. `driftzero/verify.py`: Modal sandbox: copy worktree in, `pip install -e .`, `pytest -q`; return `{passed, exit_code, stdout, stderr}`. On failure, feed the traceback back to step 4 (max 3 iterations).
6. `driftzero/guard.py`: refuse any diff that removes a test function or an `assert`. Count via `ast`; compare before/after.
7. `driftzero/pr.py`: push branch, `gh pr create` with blast-radius table + sandbox log + token/cost telemetry in the body.
8. `driftzero/cli.py` (Typer): `driftzero migrate --repo <path> --changelog <file> [--open-pr]`.

Definition of done for v0: one command, run on the benchmark, ends with a real PR on GitHub whose CI-equivalent (the Modal pytest log) is green, in under 5 minutes.

### v1, only after v0 is green (Mon–Tue)

- Parallel repair: fan out independent blast-radius clusters to Baseten `muse-spark-open-1026` (open-weights bonus) with Muse Code as the other worker.
- `muse-wonder-1.0` as the planner that splits the DriftSpec into the clusters.
- Second scenario (fintech float-dollars → integer-cents) to show generality.
- Minimal live view: stream JSONL events from `muse exec` and the sandbox log to a terminal UI (rich) that records well on video. No Next.js unless v0+v1 are done by Mon night.
- Optional: `muse-image-1.0` diagram in the PR body; `sam-3.1` has no role here, don't force it.

### Explicitly out of scope before Nov 4

Adapter/dual-write mode, multi-language support, GitHub App webhooks, tree-sitter, mypy/ruff oracles, AST visualizers, cryptographic log signing, three benchmarks.

## Repo layout

```
driftzero/              # package (cli, differ, blast_radius, repair, verify, guard, pr, telemetry)
benchmarks/robotics_telemetry_drift/
scripts/verify_providers.py
tests/                  # DriftZero's own tests (guard, blast_radius, differ parsing)
CLAUDE.md  AGENTS.md -> CLAUDE.md
pyproject.toml          # `driftzero` console entry point
```

## Engineering rules

- Build incrementally: each stage runnable on its own with a `python -m driftzero.<stage>` smoke path before wiring the next.
- Log tokens, provider, latency and cost per call to `.driftzero/runs/<id>.jsonl`; the PR body reads from it.
- All LLM calls go through `driftzero/providers.py` (one OpenAI-compatible client per provider, retries, timeouts).
- The repair may change tests only by updating literals and fixture data to the v2 contract and by adding tests. It may never remove a test, reduce asserts, add skip/xfail, or alter pytest configuration. The guard enforces this and lists every allowed change for human review.
- Keep every commit small and pushed; the commit log is evidence for the "new work only" rule.
- Never state unverified specifics as fact, in code comments, docs, PR bodies or reports. If something is unconfirmed, say so or verify it first.
