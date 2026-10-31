# External target: JobTracker, OpenAI Python SDK v0 -> v1

- Target: [ddavarma/JobTracker-driftzero-test](https://github.com/ddavarma/JobTracker-driftzero-test), a fork of
  [resumejob/JobTracker](https://github.com/resumejob/JobTracker) (MIT, last upstream commit 2024-01-06), not written
  by us. 607 non-blank Python lines, 5 pytest tests that mock the v0 API and pass offline with `openai==0.28.1`.
- Drift input: [`../openai_v0_to_v1/CHANGELOG_openai_v1.md`](../openai_v0_to_v1/CHANGELOG_openai_v1.md) (shared with other OpenAI v0→v1 targets), sections quoted verbatim from the official v1.0.0 migration guide
  (github.com/openai/openai-python/discussions/742). No OpenAPI documents exist for this change.
- Hold-outs: `holdout_v2/` (its sandbox name is the `holdout_name` field in `benchmarks/targets.json`), 7 tests
  against the v1 client interface, mocked at the HTTP layer with respx. They are **validated against the target
  dependency `openai>=1,<2`** (`validated_against` in targets.json): in a venv with that pin plus the hold-outs'
  `holdout_requirements` (`respx>=0.21,<0.23`), resolving to openai 1.109.1 / respx 0.22.0 / httpx 0.28.1, they fail
  7/7 on the v0 code and pass 7/7 on a hand-written reference migration (not committed anywhere). They are not valid
  for openai 2.x/3.x, which use a different HTTP transport (`httpx2`); runs must pin `--target-dep "openai>=1,<2"`, and
  DriftZero warns if a run's pin differs from `validated_against`.
