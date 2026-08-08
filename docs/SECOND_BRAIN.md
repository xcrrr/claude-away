# Second brain

## Purpose

The second brain exists so a multi-day autonomous system does not have to rediscover the same project context every run and so the returning human can understand *why* the plan evolved.

It is plain Markdown first and Obsidian-friendly by design. Obsidian is optional.

## Separation of concerns

Three stores answer three different questions:

| Store | Question |
| --- | --- |
| Git | What exactly changed in the code? |
| SQLite | What is the authoritative execution/task state? |
| Brain | What did we learn, decide, and plan? |

Graphify adds a fourth view: **how is the project structurally connected?**

Do not duplicate whole source files into the brain.

## Layout

```text
.claude-away/brain/
├── Goals.md
├── Master Plan.md
├── Decisions.md
├── Blockers.md
├── Return Briefing.md
├── Projects/
│   └── <repo>/
│       ├── Overview.md
│       ├── Architecture.md
│       ├── Current State.md
│       └── Learnings.md
├── Tasks/
│   ├── AWAY-0001.md
│   └── AWAY-0002.md
└── Runs/
    └── <run-id>.md
```

## Note rules

### Goals.md

Human-owned intent. Claude may propose edits, but goal meaning/priority changes must be attributable to the user's instruction or an explicitly allowed inference policy.

### Master Plan.md

Human projection of the current DAG plus strategic replan history. It is regenerated from structured plan state where possible, not freely rewritten as the source of truth.

### Decisions.md

Only material decisions with consequences. Each entry contains date, task/run, decision, alternatives considered, and why.

### Blockers.md

Unresolved items requiring a human, permission, external service, missing secret, policy change, or unavailable dependency. Resolved blockers remain in history.

### Project notes

- `Overview.md`: stable purpose/scope and important entry points.
- `Architecture.md`: semantic architecture; link to Graphify rather than copy its output.
- `Current State.md`: mutable current focus, health, known work, and active branches.
- `Learnings.md`: durable non-obvious discoveries that future agents would otherwise rediscover.

### Task notes

One compact page per task: goal, scope, acceptance criteria, attempt summaries, final evidence, resulting commit/PR, and follow-ups.

### Run notes

Operational summary, not raw transcript. Include start/end, task attempt, notable events, failure/checkpoint reason, and pointers to artifacts/evidence.

## Update policy

Update the brain:

- after a task reaches a terminal state;
- after a material architectural/product discovery;
- after a decision changes future work;
- after a strategic replan;
- when a blocker appears or resolves.

Do **not** write a note after every tool call. That creates noise and burns capacity.

## Graphify integration

Graphify is optional but first-class.

Initial integration should call the public `graphify` CLI rather than couple Claude Away to Graphify internals.

- First inventory: build a graph for each enrolled repo.
- Later runs: prefer incremental update for changed files.
- Planning: load the structural report before broad raw-file discovery.
- Deep architecture question: query/path/explain the graph as needed.
- Obsidian export: optional; Claude Away's semantic brain remains separate so a Graphify format change cannot destroy execution memory.

## Privacy and secrets

The brain must never intentionally store:

- environment variable values;
- tokens/API keys/session cookies;
- decrypted secret files;
- raw credential output;
- unrelated personal files discovered outside enrolled roots.

Paths and secret *names* may be referenced when necessary to explain a blocker, but values are redacted before persistence.
