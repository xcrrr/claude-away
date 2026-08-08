# Product specification

## Vision

Claude Away turns a user's absence into a bounded, auditable autonomous engineering window.

The user should be able to say:

> I will be away for eight days. These are the repositories you may touch. These are my goals. Do the highest-value safe work you can, use my included Claude capacity intelligently, keep proof of what you finish, adapt when the plan becomes wrong, and tell me exactly what happened when I return.

The product is successful when the user returns to **reviewable progress**, not simply high usage.

## Target user

The first target is a heavy Claude Code Pro/Max user who:

- owns or actively maintains multiple software projects;
- already trusts Claude Code with substantial engineering tasks;
- periodically loses useful subscription capacity during travel, sleep, school, weekends, or other absence;
- is comfortable reviewing branches and PRs created by an agent;
- wants long-horizon autonomy without surrendering control of production.

## Product principles

### 1. Goals, not a backlog treadmill

Every generated task must trace to a user-declared goal. When the queue is empty, replanning starts from goals and current project state. If no useful next task can be justified, stop.

### 2. Evidence, not model confidence

A model saying “done” is not completion. A task reaches `DONE` only after its acceptance criteria have produced recorded evidence.

### 3. The model reasons; the controller remembers facts

Claude may propose priorities, implementation strategies, task splits, or plan changes. Deterministic code owns state transitions, dependency resolution, retry budgets, locks, timestamps, capacity windows, and evidence.

### 4. Replanning is normal

The plan is a hypothesis. Re-evaluate locally after every completed/failed/blocked task and run a strategic replan every 84 hours by default.

### 5. Safe autonomy beats maximum autonomy

Default to branches, worktrees, tests, commits, PRs, and human review. Production deploys, destructive operations, secret access, protected-branch merges, and irreversible external actions require explicit policy grants.

### 6. Use documented Claude surfaces

Do not scrape OAuth credentials, call undocumented subscription endpoints, or attempt to evade limits. Integrate through documented Claude Code plugins, hooks, workflows, Agent SDK/non-interactive mode, Routines, and status-line telemetry.

## Core user journey

### Prepare

`/claude-away:init`

1. Discover candidate repositories, but enroll only those the user approves.
2. Inspect repository health, tests, build commands, branch protection assumptions, dirty worktrees, and existing agent instructions.
3. Optionally build/update Graphify structural context.
4. Create the Markdown brain.
5. Ask the minimum high-value questions about goals and boundaries.
6. Persist the answers as configuration, not conversational memory only.

### Plan

`/claude-away:plan`

1. Read goals, project state, Graphify reports, open work, and recent decisions.
2. Generate atomic tasks with dependencies.
3. Give every task acceptance criteria and a verification strategy before execution.
4. Estimate task size/risk/capacity class.
5. Produce a human-readable Master Plan plus the machine task DAG.
6. Flag tasks that can never run without human input.

### Start

`/claude-away:start 8d`

1. Confirm the return time, execution mode, capacity target, repo set, and safety profile.
2. Run a permissions/tooling preflight.
3. Refuse to start if state cannot be persisted safely.
4. Start the supervisor/routine schedule.

### Execute

For each selected task:

1. acquire the task lock;
2. create/resolve an isolated branch or worktree;
3. checkpoint `RUNNING` before asking Claude to work;
4. use a saved dynamic workflow when parallelization improves the task;
5. update the brain with material discoveries, not a transcript dump;
6. enter `VERIFYING` and run declared checks;
7. record evidence;
8. move to `DONE`, retry, `BLOCKED`, or `FAILED`;
9. micro-replan affected dependencies/priorities;
10. checkpoint before launching the next task.

### Replan

Strategic replanning runs every 84 hours by default and immediately when a major assumption is invalidated.

It may:

- reorder ready tasks;
- split oversized tasks;
- retire tasks made obsolete by earlier work;
- create new tasks grounded in a declared goal;
- mark work as requiring human judgment;
- change capacity allocation across projects.

It may not silently weaken acceptance criteria for a failing task merely to declare success.

### Return

`/claude-away:back`

Generate a briefing with:

- goal progress;
- completed tasks and verification evidence;
- links/branches/PRs;
- blocked and failed tasks;
- decisions Claude made and why;
- notable project knowledge learned;
- capacity-window history;
- changes to the original plan;
- the first three things the user should review.

## Capacity policy

Capacity is a scheduling input, not the objective function.

Local mode will ingest documented Claude Code `five_hour` and `seven_day` usage windows. The scheduler should:

- target a configurable percentage of each window;
- retain a small completion/checkpoint reserve by default;
- prefer tasks likely to fit before the next reset;
- stop launching new tasks near the reserve boundary;
- resume after reset with persisted state;
- treat `StopFailure(error=rate_limit)` as a first-class recoverable event;
- never fabricate work solely to consume quota.

## Planning requirements

Every executable task must have:

- stable ID;
- source goal(s);
- repository;
- title and bounded scope;
- dependency IDs;
- value/priority;
- risk level;
- acceptance criteria;
- verification commands/checks where applicable;
- expected artifacts;
- explicit human-required flag when relevant.

## Multi-repository behavior

The planner may reason across repositories, but execution state remains repo-aware.

- A task modifies one primary repository unless explicitly modeled as a coordinated multi-repo task.
- Cross-repo dependencies are represented in the DAG.
- Repositories never inherit permissions from another repository.
- Each repo has its own test/build recipe and safety policy overrides.

## Non-goals

Claude Away is not intended to:

- bypass Anthropic usage limits;
- farm tokens or manufacture meaningless work;
- merge/deploy arbitrary changes without user policy;
- replace Git or CI as the source of truth for code;
- copy entire source repositories into Obsidian;
- promise that an LLM can safely make every product decision alone;
- depend on an undocumented Claude credential or private endpoint.

## v0.1 acceptance bar

The first release is not “working” until an automated test can kill the supervisor at multiple points in a task, restart it, and prove that:

1. a completed task is not executed twice;
2. an unverified task is never reported as done;
3. dependencies stay consistent;
4. a rate-limit failure preserves recoverable state;
5. a dirty/unexpected repository state stops safely;
6. pause prevents new work;
7. resume continues from persisted state;
8. `/back` reconstructs an accurate briefing from stored evidence.
