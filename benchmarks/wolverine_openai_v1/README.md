# External target: wolverine, OpenAI Python SDK v0 -> v1

- Target: [ddavarma/wolverine-driftzero-test](https://github.com/ddavarma/wolverine-driftzero-test), a fork of
  [biobootloader/wolverine](https://github.com/biobootloader/wolverine) (MIT, last upstream commit 2024-03-07, last
  code change 2023-04-26), not written by us. 427 Python lines including examples (244 in `wolverine/wolverine.py`),
  6 pytest tests (3 plus one parametrized x3) that mock the v0 API. `requirements.txt` pins `openai==0.27.4`; the 6
  tests pass offline on both `openai==0.27.4` and `openai==0.28.1` (Python 3.11, checked 2026-10-30 with outbound
  HTTP pointed at a dead proxy). No `pyproject.toml`/`setup.py`.
- Drift input: the same changelog as JobTracker,
  [`../openai_v0_to_v1/CHANGELOG_openai_v1.md`](../openai_v0_to_v1/CHANGELOG_openai_v1.md) (sections quoted verbatim
  from the official v1.0.0 migration guide).
- Hold-outs: `holdout_v2/`, 7 tests against the v1 client interface, mocked at the HTTP layer with respx: no
  `openai.ChatCompletion`/`openai.Model` left, `json_validated_response` through `POST /chat/completions` (body and
  bearer key), retry with conversation history, retry exhaustion, `send_error_to_gpt`'s prompt, and
  `check_model_availability` through `GET /models` (available, and exit when not). **Validated against the target
  dependency `openai>=1,<2`** (`validated_against` in `benchmarks/targets.json`): in a venv with wolverine's
  requirements minus `openai`, that pin and `respx>=0.21,<0.23` (openai 1.109.1 / respx 0.22.0 / httpx 0.28.1), they
  fail 7/7 on the v0 code and pass 7/7 on a hand-written reference migration (not committed anywhere). They also pass
  when the migrated code keeps appending the v1 response's message object to `messages` (the SDK serialises it), so
  they do not demand more than the guide's renames plus attribute access.
- Per-target rule (`repair_rules`): importing `wolverine` must keep working without `OPENAI_API_KEY`. v0 allowed it,
  the upstream CI workflow runs `pytest` without a key, and `OpenAI()` raises at construction when no key is set.
