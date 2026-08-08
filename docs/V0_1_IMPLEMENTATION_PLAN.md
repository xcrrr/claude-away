# v0.1 implementation plan

This document is the implementation counterpart to [ROADMAP.md](ROADMAP.md). The roadmap
says *what* each release contains; this says *in what order we build it and why*, and
records the design decisions and document contradictions resolved along the way.

Milestone 1 is **implemented**. Milestones 2-7 are planned.

## Ordering principle

Build from the state machine outward. Every milestone below adds capability on top of a
layer whose failure modes are already tested, so that no later milestone has to weaken an
earlier guarantee to make progress.

The bar for each milestone is not "the happy path works". It is: *could a multi-day
autonomous supervisor be built on this without replacing it later?*

---

## Milestone 1 - Deterministic state core (implemented)

**Delivered:** Python package and `awayctl` entry point; versioned SQLite migrations;
the documented eight-state task lifecycle behind a single enforced transition layer; the
evidence gate; DAG validation and readiness; attempts; append-only evidence and audit
events; leases with expiry and reconciliation; idempotent replay; typed domain errors;
schema contracts; a test suite covering crash recovery, concurrency and replay; CI.

**Not delivered, by design:** anything that executes. No Git operations, no Claude
invocation, no verification command execution, no scheduler, no planner, no plugin skills.
Milestone 1 *models and enforces* the verification contract; Milestone 3 executes it.

### The invariants this milestone makes true

| Invariant | How it is enforced |
| --- | --- |
| `VERIFYING -> DONE` requires passing evidence from the current attempt | Gate evaluated inside the transition transaction |
| Evidence from a previous attempt cannot satisfy a new one | Gate scoped to the live `attempt_id`; retry closes the attempt |
| Evidence for a *since-edited* requirement cannot satisfy it | `spec_hash` recorded on evidence, compared to the requirement's current hash |
| A task with zero required checks cannot reach `DONE` | Refused at creation, *and* independently by `required_total >= 1` in the gate |
| An LLM review cannot be the only thing gating `DONE` | Validator requires one required check of a deterministic type |
| Evidence and audit events are append-only | SQLite `BEFORE UPDATE/DELETE ... RAISE(ROLLBACK)` triggers |
| `DONE` and `CANCELLED` are absorbing | Transition table *and* database triggers |
| Only the transition layer may change a status | Trigger calling a connection-scoped function; a connection without it cannot write a status at all |
| A task cannot be born `DONE` | `BEFORE INSERT` trigger requiring `PENDING` |
| Status guards cannot be dodged by delete-and-reinsert | `BEFORE DELETE` trigger; tasks are cancelled, never deleted |
| At most one runner owns a task | Partial unique index on unreleased leases |
| An expired lease is not permission to rerun | Expiry does not release; takeover requires explicit reconciliation with a recorded reason |
| A replan cannot silently weaken a failing check | Weakening guard in `update_verification_requirements` |
| Replaying an operation after a crash does not duplicate it | Idempotency keys with request fingerprints |
| Evidence must name the attempt that produced it | Refused at record time *and* by a database CHECK |
| No live attempt means the gate is closed | `GateReason.NO_ACTIVE_ATTEMPT`; `mark_verified` requires an active attempt |
| A replan cannot install a contract creation would reject | Shared `validate_verification_contract` on both paths |
| `INSERT OR REPLACE` cannot resurrect a retired task | `PRAGMA recursive_triggers = ON`, so DELETE triggers fire during REPLACE |
| A replay must still describe reality | `StaleReplayError` when the task or attempt moved on |

### Key design decisions

**One transition layer, enforced by the database.** `tasks.status` is guarded by a trigger
that calls an application-defined function registered only on connections owned by
`Database`. A trigger referencing a `TEMP` table was tried first and rejected: SQLite
refuses it outright (`cannot reference objects in database temp`). The function approach
has a useful side effect -- a connection that never registered it, such as the `sqlite3`
CLI, cannot change a status at all, while reads stay open.

**`RAISE(ROLLBACK)`, not `RAISE(ABORT)`.** `ABORT` rolls back only the failing statement
and leaves the transaction open, so a caller that swallows the error could commit a
partially-applied unit. `ROLLBACK` ends the transaction, which is why `Database.transaction`
checks `connection.in_transaction` before issuing its own rollback.

**`BEGIN IMMEDIATE` for every write.** SQLite's default deferred transaction takes a read
lock and upgrades on first write; two connections doing read-then-write deadlock on the
upgrade, and `busy_timeout` cannot rescue it. Lease acquisition is exactly that pattern.

**Migrations do not use `executescript`.** It issues an implicit `COMMIT`, which would drop
the migration out of its transaction. Statements are split with
`sqlite3.complete_statement` (which understands `BEGIN ... END` trigger bodies) and applied
individually alongside the ledger row.

**Time is injected.** No business logic calls `datetime.now()`. Timestamps are fixed-width
UTC ISO-8601, so SQLite's lexicographic `TEXT` ordering equals chronological ordering.

**Retry opens a new attempt; interruption does not.** `VERIFYING -> RUNNING` closes the
attempt and opens the next one, because the runner is about to change the code and every
prior evidence row describes a different artifact. A rate limit or crash *suspends* the
attempt instead: nothing about the artifact changed, only the clock.

### Deliberately deferred from Milestone 1

* **Evidence hash chaining.** Would add tamper-*evidence* on top of the append-only
  triggers. Not tamper-*proofing* -- anyone with write access to the file can
  `DROP TRIGGER`. `awayctl doctor` detects a missing guard trigger and refuses to call the
  state healthy; that is the honest mitigation, and chaining can be added later without a
  schema break.
* **A trigger requiring every status change to cite a matching audit event.** Stronger than
  the current flag guard. Atomicity already guarantees the pairing today, so this is
  hardening rather than a fix.
* **`awayctl approve` for manual verification.** Human approval is legitimately a human
  action, but the token handling it needs belongs with the rest of the UX in Milestone 5.
  Until then, manual evidence is recorded through the Python API only.
* **Windows CI.** Linux and macOS run now; Windows is a v0.1 release-hardening item in the
  roadmap, and adding a job we do not yet honour would be fake green.

---

## Milestone 2 - Git isolation and safety policy

Recommended scope, building directly on Milestone 1:

1. **Project enrolment** against the config schema; refuse any path not enrolled.
2. **Repository inspection**: dirty worktree detection, expected-base resolution, branch
   protection assumptions. A dirty or unexpected repository stops the task safely rather
   than guessing.
3. **Branch/worktree creation** as `claude-away/<task-id>-<slug>`, recorded on the attempt
   (already modelled: `base_commit`, `branch`, `worktree_path`).
4. **Repository locks** so two tasks in one repo cannot interleave. `maxConcurrentTasks`
   defaults to 1 precisely because this does not exist yet.
5. **Git reconciliation after a crash** -- the concrete implementation of
   `reconcile_expired`, which currently records the decision but cannot yet inspect the
   repository that motivates it. This is the most valuable single item in the milestone.
6. **Safety policy enforcement** from `config.safety`: no force pushes, no protected-branch
   merges, no destructive operations, commit/push gated by policy.

Release gate: fault-injection tests that kill the process at each step of branch creation,
commit and push, and prove that restart never double-commits and never pushes work that
did not pass its gate.

---

## Milestones 3-7

Unchanged from [ROADMAP.md](ROADMAP.md): Claude execution and the verification contract;
capacity provider and scheduler; planner and plugin UX; supervisor and recovery; release
hardening. Two constraints already fixed by Milestone 1 are worth restating:

* The scheduler must treat capacity as an input, never an objective. `capacity.noBusywork`
  is a JSON Schema `const`, not a boolean, so "farm tokens" is not a configuration option.
* Unattended execution must select effort explicitly (`claude --effort ultracode` or a
  saved workflow). The prompt keyword only opts in for human-origin prompts, so relying on
  it in a scheduled prompt would silently degrade every autonomous run.

---

## Contradictions found in the v0.0.1 documents, and how they were resolved

Recorded rather than silently patched, per CONTRIBUTING's instruction to flag mismatches.

| # | Contradiction | Resolution |
| --- | --- | --- |
| 1 | `STATE_MODEL` allowed only `BLOCKED -> READY`, but a dependency can be cancelled while a task is blocked, so promoting straight to `READY` is unsound. | Added `BLOCKED -> PENDING` to both the code and `STATE_MODEL.md`. A parity test parses the document and fails if the two ever diverge again. |
| 2 | `task.schema.json` had no stable identity for verification entries; array position cannot survive a replan reordering them. | Added a required `id` per entry. Evidence references it. |
| 3 | A verification entry could be structurally meaningless (`type: "command"` with no command) or carry another type's fields (`type: "artifact"` with a `command`, which a naive verifier would execute). | Per-type conditional `required`, plus `unevaluatedProperties: false` with type-specific properties declared only in their matching branch. |
| 4 | `STATE_MODEL` lists "status/version timestamps" among required task fields; the schema had no timestamp at all. | Added `createdAt`, `updatedAt`, `statusChangedAt`, required, fixed-width UTC. |
| 5 | `estimatedEffort` was optional, but `ARCHITECTURE`'s scoring formula divides by it -- the scheduler would have to invent a fact. | Made required. |
| 6 | `ARCHITECTURE` says "a planner cannot directly mark tasks `DONE`", but the single schema let a planner submit `status`. | Split the contract: the root schema is the controller-owned record; `#/$defs/taskProposal` is the only planner-submittable shape, and `status`, timestamps and plan versions are structurally unrepresentable in it. |
| 7 | `config.schema.json` allowed `mode: "hybrid"`, which no document defines. | Removed. Undefined surface the core would have to branch on is worse than no surface. |
| 8 | `STATE_MODEL` requires a "time-bound lease" and `README` promises "bounded retries", but nothing was configurable. | Added a required `execution` block: `maxAttemptsPerTask`, `maxConcurrentTasks`, `leaseSeconds`, `leaseHeartbeatSeconds`. |
| 9 | `STATE_MODEL` requires a "stable runner ID", but a config file gets copied between machines and two runners sharing an id would steal each other's leases. | The runner id is generated once and bound to the state database, not to configuration. |
| 10 | `priority` was `1..100` in both schemas with no stated polarity -- a coin flip that would produce a scheduler running the least valuable work first. | Documented as higher-is-first in both schemas. |
| 11 | `^AWAY-[0-9]{4,}$` admitted both `AWAY-0001` and `AWAY-00001` as spellings of one logical id, for ids the state model promises never to recycle. | Tightened to `^AWAY-(?:[0-9]{4}\|[1-9][0-9]{4,7})$`. |
| 12 | `PRODUCT_SPEC` forbids a replan from "silently weakening acceptance criteria", but criteria were bare strings with no identity to compare. | Acceptance criteria carry stable ids, and `update_verification_requirements` refuses to drop, demote or rewrite a required check whose evidence is absent or failing. |
| 13 | `PRODUCT_SPEC` hedges "verification ... **where applicable**" and "human-required flag **when relevant**", while `STATE_MODEL` and the schema require both unconditionally. | Schema and `STATE_MODEL` are right -- an optional verification spec is a `DONE` with no gate. Left as a documentation nit rather than changing product text in this PR. |
| 14 | `PRODUCT_SPEC` describes per-repo test/build recipes and safety overrides as current behaviour; `ROADMAP` puts them at v0.2 and no schema models them. | Not added. Fields that nothing validates are how schemas rot; the recipes arrive with the milestone that consumes them. |

## Bypasses found by adversarial review, and closed

Five holes were demonstrated working against an earlier revision of this milestone. Each
now has a regression test in `tests/integration/test_gate_bypass_regressions.py`.

1. **Evidence with a NULL `attempt_id` satisfied the gate** (critical). The gate matches
   `attempt_id IS ?`; evaluated with `None` it matched unattributed rows, and
   `record_evidence` defaulted that parameter to `None`. Reachable through the *documented*
   rate-limit path, which suspends the attempt while leaving the task in `VERIFYING`: one
   unattributed "pass" completed a task whose required check had never run.
   `GateReason.NO_ACTIVE_ATTEMPT` had been declared and never wired. Closed at three layers
   -- record-time validation, a database CHECK, and the gate itself.
2. **The replan path could install an LLM-review-only contract** (high). Creation refused
   it; `update_verification_requirements` never re-ran the invariant. Both paths now share
   `validate_verification_contract`, and `allow_weakening` does not waive it.
3. **`INSERT OR REPLACE` resurrected retired tasks** (medium). REPLACE deletes the old row
   without firing DELETE triggers unless `recursive_triggers` is on, so it walked past both
   `tasks_are_never_deleted` and the absorbing-status guards.
4. **A stale idempotent replay reported success for failed work** (medium). Replaying
   `start_attempt` returned a cheerful `READY -> RUNNING` for a task that had since become
   `FAILED`. Now raises `StaleReplayError`.
5. **`status_transition()` looked like public API** (low). It opens the database gate and
   validates nothing; every guard lives above it. Renamed to `_status_transition`.

## Known limitations, stated plainly

* **A process with write access to the state file can drop the guard triggers.** SQLite
  offers no defence. `awayctl doctor` detects it; that is detection, not prevention. The
  mitigation is architectural: keep the state database outside every enrolled repository,
  mode `0600` (which `awayctl init` sets), and never put its path in an agent's prompt or
  environment.
* **The gate constrains shape, not judgement.** A bug inside the transition layer could
  still emit a well-formed transition. This is why the guards are pure functions tested
  independently, and why `DONE`/`CANCELLED` absorption is enforced a second time in the
  database.
* **Nothing here has executed a real task yet.** Every guarantee above is about state
  management. The claim "Claude Away works" is not yet true and this document does not
  make it.
