# Reviewer consistency eval (v2, v2.1)

**Question:** how reliably does reviewer v2 (Muse Spark Open, `meta-models/muse-spark-open-1026` on Baseten,
`reasoning_effort=high`, `max_tokens=24000`) report the two known issues in the two existing migration PRs?

- **Rounding:** DriftZero PR #1's `client.py` rounds the derived Euler angles to 1e-3 deg
  (`round(v, 3)`, justified by an unsupported "v1 wire resolution" comment) so that the unchanged test
  `frame.pose.yaw == 87.0` keeps passing. fleet-telemetry-v1 PR #1 does not contain it (there Muse loosened the
  test to `pytest.approx`, which the static guard reports).
- **Defaults:** both PRs give `TelemetryFrame.covariance` and `TelemetryFrame.health` defaults, although the
  DriftSpec marks them required.

**Protocol:** 3 independent runs per PR, same inputs as the posted v2 review (diff, guard items, DriftSpec,
changelog, unchanged covering tests), nothing posted. Script: [`scripts/reviewer_eval.py`](../scripts/reviewer_eval.py).
Detection is mechanical, from what the finding quotes, not from its wording:
*rounding caught* = some finding's `evidence` quotes a line containing `round(`; *defaults caught* = some finding's
`evidence` quotes the `health: float = 1.0` line or a defaulted `covariance:` line. Full records with reasoning:
[`reviewer_eval/*.eval.jsonl`](reviewer_eval/), summary: [`reviewer_eval/summary.json`](reviewer_eval/summary.json).

## Results (clean batch, 6 runs)

| PR | run | verdict | findings (severity:category) | rounding caught | defaults caught | cost | latency |
|---|---|---|---|---|---|---|---|
| DriftZero PR #1 | 1 | comment | low:test_shaped_code, medium:incomplete_migration, medium:precision_loss | yes (medium · precision_loss) | yes | $0.0201 | 29 s |
| DriftZero PR #1 | 2 | comment | low:silent_behavior_change, medium:test_shaped_code | yes (medium · test_shaped_code) | yes | $0.0327 | 44 s |
| DriftZero PR #1 | 3 | comment | low:incomplete_migration, low:test_shaped_code | yes (low · test_shaped_code) | yes | $0.0235 | 33 s |
| fleet-telemetry-v1 PR #1 | 1 | approve | – | n/a (no rounding in this diff) | no | $0.0349 | 50 s |
| fleet-telemetry-v1 PR #1 | 2 | approve | – | n/a (no rounding in this diff) | no | $0.0316 | 45 s |
| fleet-telemetry-v1 PR #1 | 3 | approve | – | n/a (no rounding in this diff) | no | $0.0252 | 37 s |

## Catch rates

| finding | DriftZero PR #1 | fleet-telemetry-v1 PR #1 |
|---|---|---|
| rounding reported (any category) | **3/3** | n/a |
| rounding reported as `test_shaped_code` | 2/3 (the other run called it `precision_loss`, medium) | n/a |
| required-fields-with-defaults reported | **3/3** | **0/3** |
| verdict `approve` | 0/3 | 3/3 |

Severity was not stable: the rounding finding came out medium in 2 runs and low in 1; the defaults finding came out
medium, low, low. Clean-batch cost: $0.1680 for 6 runs ($0.0280 per review).

## Supplementary runs (not part of the clean batch)

- **Posted v2 review** (one run, the one on the PRs): DriftZero PR #1 → rounding (medium · test_shaped_code) and
  defaults (medium · incomplete_migration); fleet-telemetry-v1 PR #1 → defaults (medium · incomplete_migration) and
  the Euler-input path in `Pose` (low · dead_compat_code).
- **A first batch that crashed** (a concurrency bug in the eval harness: six threads re-ran the guard, which writes
  the worktree's git index; fixed by building inputs once per PR). 5 of its 6 model calls completed and are kept in
  `reviewer_eval/*.partial-batch.jsonl`: DriftZero PR #1 → rounding 3/3 (all `test_shaped_code`), defaults 2/3;
  fleet-telemetry-v1 PR #1 → defaults 1/2, one `approve` with no findings.

Across every v2 run so far (clean 6 + posted 2 + partial 5): rounding reported on DriftZero PR #1 in **7/7** runs;
defaults reported in **6/7** on DriftZero PR #1 but only **2/6** on fleet-telemetry-v1 PR #1.

## On a fresh diff: fleet-telemetry-v1 PR #2

The live `driftzero migrate --review-fix` run (task `20261030-041743-ffdf04`) produced a new repair that **again
rounds the derived Euler angles** (`roll=round(eroll, 3)`, same for pitch and yaw), this time with the comment "wire
carries ~6-decimal quaternions, so round to millidegrees (well within v1's 0.1-degree precision)". The unchanged
`frame.pose.yaw == 87.0` test was in the reviewer's context. Reviewer v2 (`high`, one run) **approved with no
findings**, so `--review-fix` had nothing to act on. Its reasoning drafted the finding ("rounding is magic constant
to satisfy exact equality test, not required by spec"), half-accepted the new justification because the fixture
really carries 6-decimal quaternions, and then applied the prompt's own rule: "Given uncertainty, safest is to
approve with no findings, as per 'Report nothing you are unsure is real'." It also asked itself "Would an empty
findings list with approve be considered incorrect by evaluator?". Full trace:
[`runs/20261030-041743-ffdf04.jsonl`](runs/20261030-041743-ffdf04.jsonl) (`review` event).

So on the one unseen diff with the smell, v2 caught it **0/1**. The 3/3 above is on the diff whose miss motivated
v2's design; this run is the more honest signal of generalisation, and it is a miss.

## Reading

- The rounding is a reliable catch for v2 on DriftZero PR #1: every run that saw it reported it, varying only in
  label and severity. On a fresh diff with the same smell and a more plausible justification (PR #2) it was missed,
  so the 3/3 does not transfer.
- The defaults issue is reliable on one PR and mostly missed on the other. On fleet-telemetry-v1 the reviewer
  approved with no findings in 4 of 6 runs, so a single "approve" from it is weak evidence.
- Small numbers (3 runs per cell, one model, one effort level, two diffs). This measures run-to-run variance on
  known issues; it says nothing about false positives or issues nobody has labelled.

## v2.1: abstention policy (9 runs)

Change from v2, nothing else: the prompt no longer says "Report nothing you are unsure is real". Every finding carries
a `confidence` (0.0–1.0) and the prompt says "Suppression is not allowed; low confidence is." The verdict is `approve`
iff no finding reaches 0.5 (enforced in code). Same inputs as v2; the prompt still never mentions rounding. 3 runs per
PR at `high`, nothing posted, $0.43 total. Records: [`reviewer_eval/v2.1/`](reviewer_eval/v2.1/).

Ground truth per diff (checked in the code, not inferred from the reviews): rounding is present in DriftZero PR #1
and fleet-telemetry-v1 PR #2, absent in fleet-telemetry-v1 PR #1; required-fields-with-defaults is present in
DriftZero PR #1 and fleet-telemetry-v1 PR #1, absent in PR #2 (there `covariance` and `health` have no defaults).

| PR | run | verdict | findings | rounding (best confidence) | defaults (best confidence) | cost |
|---|---|---|---|---|---|---|
| DriftZero PR #1 | 1 | comment | 5 (4 ≥0.5) | yes, 0.88 | no | $0.0543 |
| DriftZero PR #1 | 2 | request_changes | 8 (7 ≥0.5) | yes, 0.88 | yes, 0.78 | $0.0587 |
| DriftZero PR #1 | 3 | request_changes | 6 (5 ≥0.5) | yes, 0.88 | yes, 0.78 | $0.0534 |
| fleet-telemetry-v1 PR #1 | 1 | comment | 7 (3 ≥0.5) | n/a | yes, 0.68 | $0.0490 |
| fleet-telemetry-v1 PR #1 | 2 | request_changes | 5 (5 ≥0.5) | n/a | yes, 0.71 | $0.0486 |
| fleet-telemetry-v1 PR #1 | 3 | comment | 6 (3 ≥0.5) | n/a | yes, 0.62 | $0.0437 |
| fleet-telemetry-v1 PR #2 | 1 | comment | 6 (3 ≥0.5) | yes, 0.62 | n/a | $0.0583 |
| fleet-telemetry-v1 PR #2 | 2 | approve | 5 (0 ≥0.5) | yes, 0.48 | n/a | $0.0408 |
| fleet-telemetry-v1 PR #2 | 3 | comment | 5 (5 ≥0.5) | yes, 0.78 | n/a | $0.0272 |

### Catch rates

| known issue | DriftZero PR #1 | fleet-telemetry-v1 PR #1 | fleet-telemetry-v1 PR #2 |
|---|---|---|---|
| rounding, reported at any confidence | **3/3** | n/a | **3/3** |
| rounding, confidence ≥ 0.5 (counts toward the verdict) | 3/3 | n/a | 2/3 |
| rounding, confidence ≥ 0.6 (would trigger `--review-fix`) | 3/3 | n/a | 2/3 |
| defaults, reported at any confidence | 2/3 | **3/3** | n/a |
| defaults, confidence ≥ 0.5 | 2/3 | 3/3 | n/a |
| false "defaults" finding on the diff without them | – | – | 0/3 |

Compared with v2 (rounding on PR #2: 0/1, defaults on fleet-telemetry-v1 PR #1: 0/3), v2.1 reports both known issues
on every diff that has them. On fleet-telemetry-v1 PR #2 the rounding came in at 0.62, 0.48 and 0.78: one run
reported it below 0.5 and so approved. In DriftZero PR #1 run 1 the reviewer did mention the `health` default, but in
a 0.45 finding whose evidence quotes the `Pose` lines, so the mechanical criterion does not count it.

The price is volume: 5–8 findings per run (v2: 0–3).

### Noise on fleet-telemetry-v1 PR #1

That diff's only known issue is the defaults one, so every other finding there was checked against the code by hand
(parsing the fixture with the PR's code where a claim was testable). Listed verbatim with my judgment so a reader can
disagree:

| run | conf | finding (verbatim) | judgment |
|---|---|---|---|
| 1 | 0.56 | "Spec adds health 0..1 required. Code parses as float but never validates range or finiteness, so health outside 0..1 or NaN passes silently and health alerts will be wrong. Changelog mandates 0..1." | **real**: `health=2.0` and `nan` parse without error |
| 1 | 0.42 | "Pose retains legacy Euler-to-quaternion path (quaternion_from_euler) only to keep unchanged test test_tilt_from_roll_and_pitch passing which constructs Pose(roll=20). ..." | real observation, a design choice the repair brief asked for (keep the Python API source-compatible) |
| 1 | 0.38 | "Companion to above: Euler fallback derives quaternion from roll/pitch/yaw defaults 0.0, allowing Tilt tests to avoid migration to quaternion. The spec says do not round-trip through Euler for tilt; keeping this path encourages it." | duplicate of the above; "encourages" is speculation, tilt is computed from the quaternion |
| 1 | 0.33 | "Scalar noise derived as sqrt(trace(P_pos)/3) per spec, but code only checks derived noise >0, not that position block is symmetric positive-definite or finite. ..." | debatable: true, but the spec only describes the matrix as symmetric, it does not require validation |
| 1 | 0.28 | "Storing derived roll/pitch/yaw in Pose re-introduces Euler gimbal-lock risk the changelog removed (ramp-climbing AMR-X at pitch ±90°). ..." | **spurious**: the rotation math uses the quaternion; derived Euler fields cannot reintroduce gimbal lock into it |
| 2 | 0.82 | "When a quaternion is supplied Pose normalizes it but never derives roll/pitch/yaw. Pose(..., qx/qy/qz/qw) therefore leaves roll/pitch/yaw at the default 0.0, so pose.yaw disagrees with heading_deg()/rotation_matrix() ..." | **real**: `Pose(0,0,0,qx=..,qy=..,qz=..,qw=..).yaw == 0.0` while the parsed pose has 86.99998 |
| 2 | 0.65 | "Pose retains a v1 Euler-only construction path that derives a quaternion from roll/pitch/yaw. ... This path exists to keep test_tilt_from_roll_and_pitch passing via pose(roll=20) rather than migrating the test to quaternion, hiding the migration." | real observation, same design choice as run 1's 0.42; rated high severity here, which overstates it |
| 2 | 0.58 | "Spec removes noise (replaced by covariance). Library keeps noise as a stored field alongside covariance with no consistency check; ..." | debatable: true, and the brief asked for a derived `noise`; the missing consistency check is a fair point |
| 2 | 0.52 | "Error now mentions noise after noise was removed from the wire schema. A zero/invalid covariance should raise a covariance error, not a noise error, and the trace-only check silently accepts non-positive-definite matrices ..." | minor but true (message says "noise must be positive"); not worth medium |
| 3 | 0.55 | "health is defined as 0..1 in DriftSpec/changelog but parsed with no range or symmetry validation. ..." | **real** (same as run 1's 0.56) |
| 3 | 0.58 | "Only TypeError is converted to TelemetryParseError. float(v) on bad strings raises ValueError which escapes as raw ValueError, breaking the documented error contract ..." | **real**: a string in `covariance` or `health` raises a bare `ValueError` (softened by `TelemetryParseError` subclassing `ValueError`) |
| 3 | 0.38 | "Spec removes orientation.roll/pitch/yaw ... yet Pose keeps Euler fields as first-class attributes ... comment admits 'for source compatibility' without spec support." | real observation, design choice (as above) |
| 3 | 0.33 | "noise was removed from the wire schema and replaced by covariance per DriftSpec. Keeping noise as a stored attribute on TelemetryFrame (derived from covariance) hides the migration ..." | debatable: the DriftSpec itself gives the scalar equivalent; keeping it was requested |
| 3 | 0.30 | "The 1e-6 server guarantee is not in DriftSpec or changelog; the diff relies on an unsupported justification. ..." | **spurious**: the guarantee is in `openapi_v2.json` ("Server guarantees \|q\| = 1 within 1e-6"), which the reviewer is not given; it applied the claims rule to a true claim |

Defaults findings are excluded from the table (they are the known issue). Tally over the 14 non-defaults findings:
**4 real defect reports, 3 distinct defects** (health range not validated, quaternion-constructed `Pose` leaves
its Euler fields at 0.0, bare `ValueError` escaping the parser) that neither the guard nor the hold-outs caught; **4 real
observations of a requested design choice**; **4 debatable**; **2 spurious** (both below 0.5, so neither affected a
verdict). One run on this PR returned `request_changes`, because of high-severity findings at ≥ 0.5.

### Reading v2.1

v2.1 is a different tool from v2: it no longer misses the known issues (rounding 3/3 and 3/3, defaults 3/3 and
3/3 at any confidence), and on the "clean" diff it also surfaced three genuine defects nobody had labelled. It is
also noisier: 5–8 findings per run, with severity inflation (design choices called high) and 2 of 14 findings on
fleet-telemetry-v1 PR #1 that are simply wrong. The confidence scores carry signal but are not calibrated: a known
issue came in at 0.48 once, and a wrong claim never exceeded 0.30 here. Small numbers again (3 runs per cell, one
judge: me).
