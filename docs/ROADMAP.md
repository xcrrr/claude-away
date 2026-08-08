# Roadmap

Claude Away is being built from the state machine outward. A flashy autonomous demo without crash recovery is not a release milestone.

## v0.0.1 — Foundation

**Status: current**

- public product specification;
- architecture and state model;
- second-brain design;
- configuration/task JSON schemas;
- Claude Code plugin manifest;
- open-source contribution rules;
- documented safety boundaries.

Exit condition: the intended product can be implemented without asking the model to own transactional state.

## v0.1.0 — Local Away MVP

Goal: leave a machine running against enrolled local repositories and return to verified branches with recoverable state.

- Python `awayctl` CLI;
- SQLite migrations and event/evidence ledger;
- DAG validation and task readiness;
- `/claude-away:init`, `plan`, `start`, `status`, `pause`, `resume`, `back` skills;
- one task → one bounded Claude execution contract;
- branch/worktree isolation;
- `TaskCompleted` verification gate;
- `StopFailure` rate-limit checkpointing;
- local 5h/7d telemetry ingestion from documented status-line JSON;
- capacity-aware scheduler;
- pause/resume and process-crash recovery;
- accurate return briefing;
- Linux + macOS tests, then Windows compatibility pass.

Release gate: fault-injection tests kill the supervisor throughout execution without duplicate task completion or lost evidence.

## v0.2.0 — Memory + multi-repo

- Graphify adapter and incremental refresh;
- Markdown/Obsidian brain renderer;
- cross-repo DAG dependencies;
- project-specific build/test recipes;
- micro-replan after material task events;
- strategic replan every 84h by default;
- regenerate a useful next plan when the queue is empty;
- decision/blocker history;
- more complete return briefing.

Release gate: an 8-day simulated clock run spanning multiple capacity resets keeps state/plan/brain internally consistent.

## v0.3.0 — Cloud Away Mode

- Claude Code Routines adapter;
- persistent state strategy appropriate for fresh cloud runs;
- GitHub-only execution mode with PR handoff;
- documented fallback when exact quota telemetry is unavailable;
- routine retry/idempotency tests;
- explicit cloud permission/connectors preflight.

Release gate: laptop can be off for the full simulation and all completed work remains independently verifiable from repository state/evidence.

## v0.4.0 — Agent orchestration + polish

- saved inventory/implement/verify/replan workflows;
- Ultracode-aware local launcher;
- adaptive parallelism based on repo conflicts and remaining capacity;
- plugin marketplace packaging;
- onboarding UX and safe configuration presets;
- status dashboard / concise terminal UI;
- exportable run report;
- benchmark harness for autonomous reliability.

## v1.0.0 — Trustworthy absence

The 1.0 bar is behavioral, not a feature count:

- recovery is boring;
- state is explainable;
- no task is done without evidence;
- no hidden/private Claude APIs;
- no irreversible action by default;
- multi-day runs are reproducible enough to debug;
- the user can understand the entire week in the return briefing;
- installation and removal leave no mystery state behind.

## Later ideas

- GitHub issue/Linear/Obsidian task import;
- value-vs-capacity forecasting learned from historical attempts;
- optional desktop/mobile notifications for `NEEDS_HUMAN` blockers;
- reviewer diversity (different model/agent for implementation vs verification);
- team shared goals and policy packs;
- generic adapters for other coding agents without weakening the Claude Code experience.
