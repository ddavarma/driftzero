# `muse exec --json` event vocabulary (Muse Code 1.4.14-R7309.1)

Derived from real captures, not docs:

- [`muse_smoke_full.jsonl`](muse_smoke_full.jsonl): meta provider, `muse-spark-1.3`, effort low, one `ls` tool call. 45 lines, 10.6 s.
- An `--provider echo` run (no file kept; shapes noted below where they differ).
- The live repair run under [`runs/`](runs/), which adds any event types seen there (see the end of this file).

## Envelope

Every stdout line is one JSON object:

```json
{
  "schema_version": 1,
  "id": "018f0000-…",                  // event id
  "stream": {"kind": "session" | "exec.usage", "id": "<session uuid>"},
  "sequence": 12,                       // per-stream, monotonic
  "recorded_at": 1780531400000019,      // microseconds
  "record_type": "event" | "status" | "reconciliation",
  "durability": "durable" | "ephemeral",
  "causation_id": "<command id>",
  "payload_type": "task.lifecycle.started",   // <- dispatch on this
  "payload_schema_version": 1,
  "payload": { "kind": "...", "command_id": "...", "run_stream": {"kind": "run", "id": "..."}, ... }
}
```

Dispatch on `payload_type`. `payload.kind` is a coarser snake_case tag (e.g. every `task.lifecycle.*` has `kind: "task_lifecycle"`, and the specific phase is in `payload.event.kind`).
`ephemeral` records (`run.output.delta`, `turn.input.user`, `run.usage`) are display-only; don't rely on them for correctness.

## Session / run framing

| payload_type | record_type | payload (beyond command_id/run_stream) | notes |
|---|---|---|---|
| `runtime.command.accepted` | reconciliation | `command_kind: "turn.submit"`, `client_id` | first line |
| `session.run.linked` | event | `run_stream` | |
| `run.model.configured` | event | `provider_id`, `profile_id`, `model_id`, `display_label`, `source: "startup"` | meta provider only; absent with echo |
| `session.workspace_branch.observed` | event | `record: {workspace_root, reference: {kind: "branch", name}, vcs: "git", commit, dirty?}` | emitted at start **and** end; the end copy carries `dirty` |
| `turn.input.user` | status, ephemeral | `prompt` | |
| `run.lifecycle.started` | event | `prompt` | |
| `run.output.delta` | status, ephemeral | `text` | streaming chunks of the final answer |
| `run.usage` | status, ephemeral, `stream.kind = "exec.usage"` | `models: [{model, input_tokens, cached_tokens, output_tokens, reasoning_tokens, calls}]`, `calls`, `completed_calls`, `failed_calls`, `cancelled_calls`, `children_included`, `allowance` | once, just before the terminal event |
| **`run.terminal.completed`** | event | **`terminal: "completed"`, `text`, `reason: null`** | **the final answer.** With `--output-schema`, `text` is the schema-shaped JSON string |

### `run.lifecycle.completed` does not exist (in this version)

Neither capture contains `run.lifecycle.completed`. The run opens with `run.lifecycle.started` and closes with
**`run.terminal.<terminal>`**. The final-answer text is in `payload.text` of that event, and `payload.terminal`
repeats the outcome. `driftzero/repair.py` treats any `run.terminal.*` as the end of the run and reads `terminal` and
`reason` from it, so a non-`completed` terminal (step cap, failure, cancel) is reported rather than mistaken for success.
The streamed `run.output.delta` chunks concatenate to the same text, but they are ephemeral, so don't depend on them.

## Tasks

Each unit of work is a task with its own stream:
`task.stream.linked` (`task_id`, `task_stream`) followed by a lifecycle sequence.
Every `task.lifecycle.*` payload has `event: {kind, task_id, ...}`:

| payload_type | `event` fields | notes |
|---|---|---|
| `task.lifecycle.proposed` | `task_kind` | the **only** place `task_kind` appears; remember it by `task_id` |
| `task.lifecycle.accepted` | | |
| `task.lifecycle.scheduled` | `idempotency_key` | |
| `task.lifecycle.side_effect_intent` | `operation`, `idempotency_key`, `policy_decision` (`allow:policy`, `not_applicable`, `reminder_…`), `cancellation_handle`, `parent_task_id` | approval/policy outcome is visible here |
| `task.lifecycle.started` | `span_id` (tools) | |
| `task.lifecycle.status` | `message`, `details.phase` (`opening_stream`, `stream_succeeded`), `details.facets[]` | model tasks: attempt n/10, `request_id`, `response_id`, `model`, stream byte counts |
| `task.lifecycle.output` | `chunk`, `final_result: bool` | tool tasks: `chunk` is the same JSON string as `tool.result.text` |
| `task.lifecycle.completed` | | |
| `task.lifecycle.failed` | `reason` | seen with echo: reminder tasks fail with `"invalid run configuration: provider does not support base instructions"` |
| `task.lifecycle.rejected` | | known from earlier runs; not in these captures |

`task_kind` values seen:

| task_kind | meaning |
|---|---|
| `model.meta.response` | one model step (one Responses-API call). **DriftZero's step counter counts these.** |
| `model.unknown.response` | the echo provider's model step |
| `tool.bash` | shell command |
| `reminder.agent.skill-reminder`, `reminder.agent.goal-reminder`, `reminder.agent.verify-reminder` | background reminder agents. `--disable-reminders` suppresses them with meta; echo still spawns them |

## Tool results

`tool.result` (event): `call_id`, `correlation_facts: {tool_name, outcome}`, and `text`. For bash, `text` is a **JSON string**:

```json
{
  "chunk_id": "exec-1-1",
  "command": "ls -R benchmarks/robotics_telemetry_drift/fleet_telemetry",
  "description": "List benchmark telemetry files",
  "exit_code": 0,
  "terminal_status": "completed",
  "output": "__init__.py\n…",
  "original_output_bytes": 360,
  "original_output_tokens": 90,
  "truncated": false
}
```

The command only becomes visible at result time: `task.lifecycle.proposed` for a `tool.bash` task carries no command. The live trace therefore prints one line per `tool.result`.
`outcome` is `success` / `tool_error` / … (strings in the binary: `success`, `tool_error`, `failure_kind`).

## Typical order (smoke run)

```
runtime.command.accepted → session.run.linked → run.model.configured → session.workspace_branch.observed
→ turn.input.user → run.lifecycle.started
→ [task model.meta.response: linked, proposed, accepted, scheduled, side_effect_intent, started, status×2, completed]
→ [task tool.bash: linked, proposed, accepted, scheduled, side_effect_intent, started, output, completed] → tool.result
→ [task model.meta.response: …, run.output.delta×9 …, completed]
→ run.usage → run.terminal.completed → session.workspace_branch.observed (dirty=false)
```

## CLI facts established empirically (1.4.14)

- `--output-schema` is **rejected with `--provider echo`** (exit 2: "the provider does not send the text.format json_schema block"). Dry runs must omit it.
- `--worktree existing` **requires session logging** (exit 2 with `--no-session-log`).
- `--workspace X --worktree existing --worktree-existing X` → exit 2 "`--worktree-existing must differ from --workspace`".
  `--workspace <repo root> --worktree existing --worktree-existing <wt>` works: the effective workspace root becomes `<wt>`
  and Muse logs "caller-owned worktree retained" (it will not delete it).
  `--workspace <subdir of repo> --worktree-existing <wt>` → exit 1 "existing worktree path must differ from source repository root".
- `--worktree existing <wt>` with the path as a positional value does not work: `-w` takes an optional mode and the path is parsed as inline PROMPT text ("--prompt-file cannot be used with inline prompt text").
- Muse walks up from the workspace to the git root for `AGENTS.md`; with `--trust-workspace` it loads it. In a DriftZero worktree that is DriftZero's own `CLAUDE.md`, so the repair prompt says to ignore it.
- Cost shape: the agent's system context is ~30K input tokens per model step (smoke: 2 calls → 60,974 input tokens, 16,241 cached).
