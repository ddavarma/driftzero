# External target: JobTracker, OpenAI Python SDK v0 -> v1

- Target: [ddavarma/JobTracker-driftzero-test](https://github.com/ddavarma/JobTracker-driftzero-test), a fork of
  [resumejob/JobTracker](https://github.com/resumejob/JobTracker) (MIT, last upstream commit 2024-01-06), not written
  by us. 607 non-blank Python lines, 5 pytest tests that mock the v0 API and pass offline with `openai==0.28.1`.
- Drift input: `CHANGELOG_openai_v1.md`, sections quoted verbatim from the official v1.0.0 migration guide
  (github.com/openai/openai-python/discussions/742). No OpenAPI documents exist for this change.
- Hold-outs: `holdout_v2/` (named to match the directory DriftZero's verify step runs), 7 tests against the v1
  client interface, mocked with respx. Checked: 7/7 fail on the v0 code, 7/7 pass on a hand-written reference
  migration (not committed anywhere).
