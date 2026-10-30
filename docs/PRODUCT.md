# DriftZero, explained end to end

Written for the builder, from the implementation reports of Oct 30, 2026. Everything below was observed in a real run or is in the committed code; anything not yet verified is marked.

## 1. The problem in one paragraph

A service you depend on changes its API (v1 → v2). Your code still speaks v1. Tools like Dependabot bump version numbers; they do not rewrite call sites, data models, or the tests that mock the old response. A coding agent that "fixes" it has a trap waiting: the existing tests mock v1, so if it migrates the code honestly the tests fail, and if it makes the tests pass it may have weakened them or shaped the code around them. Nobody checks the agent's claim that "tests pass" with a test the agent didn't touch.

DriftZero's position: the agent never grades its own homework. Repair happens in an isolated worktree, a static guard rejects test tampering, an independent sandbox reruns the agent's tests plus contract tests the agent has never seen, an open-weights reviewer reads the diff for smells static analysis can't see, and the result is a GitHub pull request with every piece of evidence attached.

## 2. The pipeline, stage by stage

```
changelog + openapi_v1 + openapi_v2
        │
        ▼
 [1] differ.py ─── deterministic JSON diff of the two OpenAPI specs
        │           + one muse-spark-1.3 call (Meta Responses API, strict JSON schema)
        │             that annotates: replaced_by mappings, migration formulas, value changes.
        │             It cannot add or drop structure; anything dropped is restored.
        ▼          → DriftSpec (Pydantic)
 [2] blast_radius.py ── Python `ast` walk of the target repo, no LLM.
        │               Finds impacted files, functions, classes, constants, Pydantic models,
        │               test functions, fixtures, and the edges between them (116 links on the benchmark).
        ▼          → impact set + link graph
 [3] repair.py ─── git worktree (.driftzero/wt-<id>, branch driftzero/<id>)
        │           Muse Code headless: muse exec --json --prompt-file ... --approval-mode never
        │           --trust-workspace --no-foreign-personal-context --max-model-steps 40
        │           --model muse-spark-1.3 --contributor-mode off --output-schema ...
        │           Prompt = DriftSpec + impact set + new fields to wire in + changelog formulas
        │           + hard rules (tests may change only by updating literals and fixture data to v2 and by
        │             adding tests; never remove a test, reduce asserts, add skip/xfail or touch pytest config;
        │             run pytest directly).
        ▼          → diff + Muse's schema-shaped answer + JSONL event log
 [4] guard.py ──── AST diff of tests/ between base and worktree.
        │           Rejects: removed test, fewer asserts, skip/xfail added, assert → bare expr,
        │           new conftest.py or pytest-config change. A rejection skips the sandbox and
        │           goes back to Muse as "restore test X".
        │           Reports (allowed, every one listed in the PR for human review): literal changes, fixture
        │           data, added tests, comparison loosening (== → approx), stale test names (test says v1,
        │           body says v2).
        ▼
 [5] verify.py ─── Modal Sandbox (debian_slim py3.11): upload worktree, pip install -e,
        │           run (a) the agent's tests/ and (b) holdout_v2/ (8 contract tests) as separate commands with
        │           --noconftest -c /dev/null so the agent can't influence them.
        │           Hold-outs live only in DriftZero, hidden from the worktree (git skip-worktree),
        │           never in the prompt. On retry, only failing hold-out names + one-line reasons
        │           reach the agent. Up to 3 iterations in the same worktree (loop.py).
        ▼          → passed only if both suites exit 0
 [6] pr.py ─────── commit, push driftzero/<id> to the TARGET repo's origin, gh pr create -R.
        │           Body: lead box, agent's own summary, DriftSpec table, blast radius with
        │           "covered by tests", test changes needing review, verification (hold-out names),
        │           cost (tokens, cached %, USD), wall-time split, links to run logs, reproduce line.
        ▼
 [7] reviewer.py ─ meta-models/muse-spark-open-1026 on Baseten (Fireworks as alternate),
                    JSON mode, reasoning_effort high, max_tokens 24,000.
                    Input: diff + unchanged tests covering changed functions (from the link graph)
                    + guard items + DriftSpec + changelog + the repair agent's hard rules as
                    "agreed migration policy" (scoped to interfaces, not numeric fidelity).
                    Comments in the diff are treated as the agent's claims, not evidence. Every
                    candidate finding is reported with a confidence; suppression is not allowed.
                    Posted as a PR review with inline comments; ≥0.5 in "Review findings",
                    <0.5 in "Possible issues". Prompt frozen at v2.3.
                    --review-fix: findings ≥ medium and ≥ 0.6 confidence trigger one extra
                    repair iteration, then guard, verify and, if code changed, a second review.
```

One command runs all of it: `driftzero migrate --repo <path-or-github-url> --changelog <md> --openapi-v1 <json> --openapi-v2 <json> [--review-fix] [--no-pr] [--no-review]`.

## 3. Which Meta model does what, and why

| Step | Model | Why this one |
|---|---|---|
| Differ annotation | `muse-spark-1.3` via Meta Responses API, strict `json_schema` output (confirmed working) | One cheap structured call; the deterministic diff carries the structure |
| Repair | `muse-spark-1.3` inside Muse Code (`muse exec`) | Muse Code already has the agent loop, worktree awareness, sandboxed shell, JSONL telemetry |
| Review | `meta-models/muse-spark-open-1026` on Baseten (open weights) | A different model family reading the diff gives independence; qualifies for the open-weights bonus with a load-bearing role |
| Not used | `muse-wonder-1.0`, `muse-image-1.0`, `sam-3.1`, `muse-voice-transcribe-1.0` | No honest role in this pipeline; don't force them in |

## 4. What the real runs showed (all numbers from committed run logs)

Benchmark: `fleet-telemetry-v1`, a robot fleet telemetry client. v1 → v2 drift: two endpoint renames, Euler angles (roll/pitch/yaw) replaced by quaternions (two robots have negative qw), scalar `noise` replaced by a 6×6 covariance, a required `health` field added with nothing in v1 to map to.

| Run | What | Result |
|---|---|---|
| Stage 4 first repair | Muse alone, hand-written spec | 22 steps, 122 s, 12 files +473/−51, 21/21 own tests, $0.46 |
| DriftZero PR #1 | first `migrate` end to end | 17 steps, 142 s, 30/30 own tests, 7/7 hold-outs, $0.37 |
| fleet-telemetry-v1 PR #1 | from changelog, separate target repo | differ matched hand-written spec exactly ($0.024), 33 steps, 25/25 + 7/7, $0.59, 193 s |
| fleet-telemetry-v1 PR #2 | `--review-fix`, reviewer v2 | reviewer approved, no fix iteration, 260 s, $0.67 |
| fleet-telemetry-v1 PR #3 | `--review-fix`, reviewer v2.1 | 6 findings ≥0.5, 4 triggered a fix pass; Muse justified all 4 (repair rule: keep Python API compatible) and changed nothing, 300 s, $0.84 |
| fleet-telemetry-v1 PR #4 | `--review-fix`, reviewer v2.2, 8 hold-outs | 2 iterations (17 + 11 steps). Iteration 1 passed its own 22 tests but failed the new health-range hold-out (7/8); the retry prompt carried only the redacted line `test_health_outside_documented_range_is_rejected: Failed: DID NOT RAISE TelemetryParseError`, and iteration 2 added the 0..1 check (22/22, 8/8). Review: approve, no findings (this diff has neither known smell), no fix pass. Differ $0.026, total $0.68, 234 s |

Repeatable behaviours observed across runs: Muse rounds derived Euler angles to 1e-3 degrees so an unchanged exact-equality test still passes (three runs: the stage 4 repair, DriftZero PR #1, fleet-telemetry-v1 PR #2), and in three other runs loosened `== 87.0` to `pytest.approx` instead (fleet-telemetry-v1 PR #1, #3, #4). The guard reports every loosening for human review; it cannot see the rounding, which lives in library code.

PR #4 is the clearest case for hold-outs: the agent's own suite was green, the hidden contract test was not, and a one-line redacted failure was enough for Muse to fix it without seeing the test.

## 5. The reviewer story (keep it honest, it's the best material you have)

- **v1**: saw only the diff, 8,000-token budget. Approved both PRs. The reasoning trace shows it raised "rounding to 3 decimals to make a test pass?" then accepted Muse's own comment ("v1 wire resolution") as justification. At `xhigh` it ran out of tokens mid-thought and returned nothing.
- **v2**: added the unchanged tests that cover changed code, 24,000 tokens, "comments are claims, not evidence." Caught the rounding 3/3 on DriftZero PR #1 and found a second defect nobody had labelled (required `covariance`/`health` given defaults). On a fresh diff (PR #2) it drafted the rounding finding and then suppressed it, citing the prompt's "report nothing you are unsure is real."
- **v2.1**: suppression banned, confidence field added. 9-run eval: rounding 3/3 on both diffs where present (≥0.6 confidence on 5/6), required-fields-with-defaults 2/3 on DriftZero PR #1 and 3/3 on fleet-telemetry-v1 PR #1, plus three real defects on the baseline PR that guard and hold-outs missed (`health` accepts 2.0 and NaN; a Pose built from a quaternion leaves Euler fields at 0.0; a bare ValueError escapes the parser). The health one became the 8th hold-out. Cost: 5–8 findings per run instead of 0–3, inflated severities, two spurious low-confidence findings, confidence uncalibrated. Live (PR #3): the fix pass ran and Muse justified every finding with the repair prompt's compatibility rule, which the reviewer did not know about.
- **v2.2**: the reviewer gets the repair contract as "agreed migration policy". Noise gone on the baseline PR (10 non-defaults findings: 8 real, 2 debatable, 0 spurious), but recall fell: the PR #2 rounding never reached 0.5 (0/3; v2.1 2/3 at ≥0.6), because the reviewer read "Euler may remain as derived values" as permitting rounding them.
- **v2.3** (frozen): one sentence scoping the policy to interfaces, not numeric fidelity. It made things worse: in 9 runs no finding reached 0.5 (highest 0.45), every run approved, `--review-fix` would never trigger. The right things are still mentioned at low confidence (7 of 9 baseline findings real). At ≥0.6 the rounding was caught 5/6 (v2.1), 2/6 (v2.2), 0/6 (v2.3). Full tables: `docs/reviewer_eval.md`.

## 6. Known limitations (say these out loud)

- Python only; one real scenario; one target repo you authored.
- The guard catches structural test tampering, not library code shaped to satisfy a test. The reviewer notices it but, as frozen (v2.3), rates it below 0.5, so its verdict is `approve` on these diffs; a human has to read the "Possible issues" table.
- Hold-outs are hand-written per target (`benchmarks/targets.json`); this target has 8. A new repo needs new hold-outs.
- Muse's `git status` fails inside its sandbox on cloned targets (exit 128); harmless, DriftZero commits outside it.
- The review-fix commit-and-second-review path has run only in offline tests; no live run has produced a fix commit yet.
- Reviewer line numbers were off by 1–12 until anchoring on quoted evidence was added.
- Cost per full run $0.37–$0.84 at the rates in `telemetry.py` (as of 2026-09); 86–96% of repair input tokens are cached.

## 7. Where things live

- Tool: github.com/ddavarma/driftzero (PR #1 is a draft kept as evidence; `docs/runs/` has every Muse event log, Modal log and reviewer trace; `docs/reviewer_eval.md` has the eval tables; `docs/muse_events.md` documents the Muse JSONL vocabulary).
- Demo target: github.com/ddavarma/fleet-telemetry-v1 (PRs #1–#4 opened by DriftZero).
- Credits: Meta $150 (differ + Muse Code), Baseten $100 (reviewer), Fireworks $100 (reviewer alternate), Modal $280 on `ddavarma2609` (sandboxes).

## 8. Remaining work before the video

Done since the first draft: reviewer gets the repair contract (v2.2) and a scope sentence (v2.3, frozen), both evaluated; `health` range hold-out (8th); live `--review-fix` runs (PR #3: fix pass ran, no change; PR #4: no trigger). Remaining:

1. Optional, high value: run on a public Python repo you did not write.
2. Terminal recording of one full run; video script written from real numbers only.
3. Not yet seen live: a review-fix pass that commits a change and gets a second review.
