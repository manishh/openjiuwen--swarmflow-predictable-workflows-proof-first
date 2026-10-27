# Demo contract

## Product and version

- JiuwenSwarm, PyPI package `workswarm==0.2.7` (installs the `jiuwenswarm` Python
  package and the `jiuwenswarm-*` commands), which pulls in openJiuwen Agent Core
  `openjiuwen==0.1.19` (the SwarmFlow engine: `openjiuwen/agent_teams/workflow/`).
- Model: OpenAI `gpt-5.5` (`gpt-5.5-2026-04-23`, `MODEL_PROVIDER=OpenAI`) for the Team Leader,
  every SwarmFlow worker and the human-reply classifier.
- Python 3.12 on Linux (Ubuntu).

## Claim

A SwarmFlow script is the fixed topology of a multi-agent workflow. A `human()`
checkpoint in that script halts the run before later stages spend anything. When
the reviewer asks for an edit, the run pauses and is resumed under the same
`run_id`, so only the changed stage and the stages after it run again while
unchanged stages replay from the run's journal. Two token ceilings, a per-run
`workflow_token_limit` and the team-wide `swarmflow_budget`, stop a run, each with
its own diagnostic and recovery advice.

## Scenarios per repeat (one fresh JiuwenSwarm home, one server, four sessions)

### A. Human checkpoint, edit, partial re-run (`pipeline/slugify_pipeline.py`)

Stages: `spec` agent -> `codegen` agent -> `human()` review -> `docs` agent.

- **Control (before the edit)**: the run reaches the `human()` checkpoint. The
  `docs` stage has not run, and the code generated so far has no `max_len`
  parameter. Executed on a long title, it returns a slug longer than 40 characters.
- **Action 1**: the reviewer replies with an edit request ("slugify must take a
  `max_len` parameter (default 40) and truncate...").
- **Observable**: SwarmFlow classifies the reply as `edit_rerun`, the run status
  becomes `paused`, the run journal (WAL) holds a `pause` record with
  `pause_reason=early_return`, and `docs` still has no journal record.
- **Action 2 (done by the Team Leader)**: SwarmFlow hands the Leader the "Edit points"
  with the instruction to edit the on-disk script minimally and relaunch with
  `resume_id=<run_id>`. The Leader edits the script (observed: an `edit_file` call
  that changes `CODEGEN_RULES`) and relaunches. The harness never touches the script.
  If the Leader only asked for confirmation, the harness would confirm once and record
  it as a nudge (not needed in the recorded runs).
- **Observable after**: the script on disk differs from v1 (diff recorded), and the
  same `run_id` continues. The engine log shows
  `spec` = `CACHE_HIT` and `codegen` = `CACHE_MISS`. The `spec` text is
  byte-identical to the first pass (replayed, not regenerated). The new code has
  `max_len`, and executed, it returns a slug of at most 40 characters. The
  checkpoint asks again (its prompt contains the new code). The reply `approve` is
  treated as continue, `docs` runs, and the run status is `completed`.

### B. Budget control (`pipeline/budget_demo_unlimited.py`)

Three sequential agents (`outline` -> `draft` -> `headline`), no per-run limit, a
large team budget. Expected: `completed`, and all three agents have journal records.

### C. Per-run ceiling (`pipeline/budget_demo.py`, `META["workflow_token_limit"] = 30000`)

Same three agents. Expected: status `failed`, `budget_exhausted_scope=workflow`,
the per-run ledger is `exhausted` with spent >= 30000, `headline` never runs (no
journal record), and the message delivered to the Leader contains
`Triggered layer: workflow (per-run)` plus the "redesign the workflow ... relaunch
with resume_id" guidance. Read from the trace file in the JiuwenSwarm home.

### D. Team (session) ceiling (`pipeline/budget_demo_unlimited.py`, small `swarmflow_budget`)

Same script as B, but the session's shared budget is set to the Leader's pre-launch
spend plus one worker call, both measured in scenario B (about 68k with gpt-5.5: a 43k
Leader call plus a 25k worker). The team ledger bills the Leader's own model calls and
gates the Leader too, so a fixed value would stop the Leader before it launches anything
whenever the Leader's prompt size changes. Expected: status
`stopped`, `budget_exhausted_scope=session`, the session ledger is `exhausted`,
`headline` never runs, and the Leader's message contains
`Triggered layer: session (shared)` plus the "raise the ceiling and open a new
session" guidance.

## Pass bar (every check, every repeat)

A1 checkpoint question arrives with `source=swarmflow_human` · A1 `docs` not run
yet · A1 v1 `slugify` has no `max_len` parameter and, called with defaults, returns a
slug longer than 40 characters ·
A2 classified `edit_rerun` (logged "Edit points") · A2 status `paused` · A2 WAL
pause record `early_return` · A2 `docs` has no WAL record · A3 the Leader edited the script and relaunched with
`resume_id=<run_id>` · A3 no other run_id in the session ·
A3 `spec` CACHE_HIT and `codegen` CACHE_MISS on resume · A3 `spec` text identical ·
A3 new code has `max_len` and returns a slug of at most 40 characters · A4
`completed`, `docs` ran · B `completed` with 3 agents journaled · C `failed`/
`workflow` scope, ledger exhausted, `headline` absent, guidance text present ·
D `stopped`/`session` scope, ledger exhausted, `headline` absent, guidance text
present · the API key appears in no file under the JiuwenSwarm home or the proof
output.

## Setup choices (all disclosed in the README and the article)

| Choice | Why |
|---|---|
| Fresh `JIUWENSWARM_HOME` per repeat, created by `jiuwenswarm-init` | No state leaks between repeats. Never touches `~/.jiuwenswarm` |
| `modes.team.jiuwen_team.enable_swarmflow: true` | SwarmFlow is off by default. It only runs in Team mode |
| `swarmflow_budget` sent with `chat.send` (3,000,000, or the measured value in scenario D) | The team-wide ceiling. The UI sends it the same way. D's value = Leader pre-launch spend + one worker call, measured in B, so the ceiling lands after the first worker |
| `preferred_language: en`, `setup_guide.enabled: false` | `jiuwenswarm-init` leaves Chinese as the default. The first-run guide distracts the Leader |
| `.env`: `MODEL_PROVIDER=OpenAI`, `ENDPOINT_PROFILE=openai`, `API_BASE=https://api.openai.com/v1`, `MODEL_NAME=gpt-5.5`. `API_KEY` (from `OPENAI_API_KEY`) passed only in the server's process env | The key never touches disk |
| Driven through the web channel WebSocket (`ws://127.0.0.1:19000/ws`, `jiuwenswarm-start app`) | The process SDK has no Team mode. This is the same API the web UI uses |
| Leader prompt, scenario A: "Run swarmflow(script_path=..., args=...)." | Naming the tool makes the Leader call it directly |
| Leader prompt, scenarios B-D: the same, plus "Do not edit the script and do not relaunch it yourself." | The budget guidance tells the Leader to redesign and relaunch. The demo needs to observe the stopped run as it is |
| The script has a comment above `CODEGEN_RULES` saying it is the line a reviewer's edit will change | It explains the journal to the reader. The Leader reads the script before editing, so this comment also steers where its edit lands. Disclose it |
| `workflow_token_limit: 30000` | One worker call costs about 25k tokens with gpt-5.5, because each worker is a full agent with a large system prompt. 30k is crossed during the second worker (`draft`), so `headline` never starts |

## Not demonstrated (content plan items that did not survive verification)

- **"Reject" intent**: a human reply is classified only as `continue` or
  `edit_rerun` (`avatar_session_backend.py::_INTENT_SCHEMA`). Rejection is
  something the script does with the reply. It is not a built-in outcome.
- **jiuwenswarm-tui run-tree**: the PyPI `jiuwenswarm-tui` is at 0.2.3 (latest
  pre-release 0.2.4b3), while JiuwenSwarm itself is at 0.2.7 (`workswarm`). Every
  release ships only `macosx_11_0_arm64` and `win_amd64` wheels, so it cannot be
  installed on Linux (`pip download jiuwenswarm-tui==0.2.3` -> "No matching
  distribution"). An interactive TUI also cannot be proved headlessly. The harness renders a text run tree from the same
  `workflow.updated` events (phases, per-agent status, tokens, both budget
  ledgers) that the UIs consume.
- **TUI pause / resume / stop**: the `swarmflow` tool accepts `resume_id` + `action`
  (`openjiuwen/agent_teams/workflow/tool_swarmflow.py::_control_run`). Not exercised here.
- **"agent-core Python SDK defines the stages"**: stages are a SwarmFlow script
  (`from swarmflow import agent, human, phase, ...`) that the Team Leader's
  `swarmflow` tool executes. There is no separate stage-declaration API.
