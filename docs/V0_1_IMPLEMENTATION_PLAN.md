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

**Split into three reviewable slices.** Milestone 1 landed as one very large pull request
and adversarial review then found twenty-two real defects in it, several critical. The
volume was the problem: a diff that big cannot be held in one reviewer's head, and the
issues that got through were exactly the ones that needed a reader who was still paying
attention. Milestone 2 ships in thirds so each can actually be reviewed.

| Slice | Scope | Status |
| --- | --- | --- |
| **M2A** | Enrolled-repository boundary, read-only Git inspection, expected-base resolution, deterministic safety policy | **implemented** |
| **M2B** | Repository locks, isolated branch/worktree lifecycle, attempt Git provenance | planned |
| **M2C** | Commit/push boundaries, crash reconciliation, fault-injection tests | planned |

Milestone 2 as a whole is **not complete** until M2B and M2C land.

### M2A - the boundary (implemented)

Read-only with respect to user repositories. Nothing in it creates a branch, writes a
commit, touches the index, or opens a network connection. It answers the questions that
must be answerable *before* any of that is safe:

1. **Was this repository explicitly enrolled?** `core/enrolment.py` turns configured
   projects into authorised repositories and fails closed on every ambiguity -- a path
   resolving to a subdirectory (which would silently widen scope to the parent), two ids
   resolving to one canonical root, a bare repository, or a state database inside an
   enrolled repository.
2. **What is this repository right now?** `adapters/git.py` reports the worktree root, HEAD,
   branch or detached state, staged/unstaged/untracked/unmerged paths, dirty submodules,
   and any interrupted Git operation.
3. **Is the base revision unambiguous?** `core/base_revision.py` returns a verdict rather
   than a commit, collecting every reason a repository is not safe to branch from.
4. **What would be permitted here?** `core/policy.py` answers deterministically, from
   configuration alone, with the rule responsible for each answer.

Two read-only commands expose it: `awayctl repos <path>` and `awayctl policy <path>` (both
also accept `--config <path>`).

**Deliberate decisions worth knowing about.**

*Subdirectory enrolment is refused.* Enrolling `repo/src` does not enrol `repo`. Accepting
it would grant authority over everything beside the directory the user actually named. The
refusal is now decided by the filesystem rather than by comparing against
`git rev-parse --show-toplevel` — see the trust boundary below for why that comparison was
itself unsafe.

## The Git trust boundary

This is the sharpest edge in M2A, it was found by review rather than by design, and it took
five attempts. The section is long because the four failed attempts are the argument for the
shape of the fifth.

**What is trusted, and what is not.**

| Thing | Trusted? | Why |
| --- | --- | --- |
| The enrolled path | Yes | The operator typed it. It is the only statement about this repository that did not come from inside it. |
| The operator's global and system Git configuration | Yes | `git lfs install` writes `filter.lfs.*` there. Refusing every LFS repository would be a bug wearing a safeguard's clothes. |
| `.git/config`, `config.worktree`, and every submodule's equivalent | **No** | Files inside the repository. From M2B they are files Claude itself can write, and today they arrive with any clone, archive, or pull request. |
| Tracked content, including `.gitmodules` and `.gitattributes` | **No** | Arrives through an ordinary commit. Needs no access to `.git` at all. |
| Git's own answers about the repository's shape (`rev-parse --show-toplevel`, `--absolute-git-dir`) | **No** | They are computed *from* the untrusted configuration. This is the part that took four rounds to accept. |
| The process boundary | Out of scope | Inspection runs as the operator, in the operator's filesystem namespace. See "residual risk" below. |

**Why it matters more than ordinary code execution.** Several configuration keys hold
commands Git executes, and `git status` alone fires them: `core.fsmonitor` runs on every
invocation, and `filter.<driver>.clean` runs whenever a tracked file's content is examined.
The execution is bad. The *masking* is worse: an `fsmonitor` hook decides what Git believes
changed and a clean filter decides what Git compares against, so a hostile one makes a
modified worktree report clean — which defeats `core/base_revision.py` entirely, and
`awayctl repos` prints `ready=1/1` over somebody else's uncommitted work.

**Four rounds, one shape.** Every round found a critical in the previous round's fix, and
every one was the same defect: repository-controlled configuration changed what the
controller executed or believed.

1. `core.fsmonitor` executed during a "read-only" inspection.
2. The audit stopped at the superproject, so a filter driver one directory down executed
   anyway — `git status --ignore-submodules=none` spawns a child status in every gitlinked
   submodule, and that child reads the submodule's own config.
3. `core.worktree` moved `rev-parse --show-toplevel`, which the recursion used to decide
   whether a gitlinked path was a separate repository. Pointing it at a decoy made the skip
   fire, the audit never ran, and Git descended and ran the filter regardless.
4. `core.worktree` again, this time redirecting *what got inspected*. The audit read the
   real submodule's config and cleared it; `git status` reported on the decoy. Reproduced
   directly: `is_clean=True`, `dirty_submodules=[]`, while `super/mod/a.txt` on disk read
   `SOMEONE ELSE'S UNCOMMITTED WORK`.

The lesson is not that a key was missed. A deny-list over Git's configuration space cannot
be completed by inspection — Git has hundreds of keys and adds more each release — and
`core.worktree` is not even command-bearing: `git submodule add` writes it legitimately, so
"refuse anything that names a command" would never have caught rounds three or four.

**Three controls replace the deny-list, and all three are required.**

1. **Layout comes from the filesystem.** `adapters/gitlayout.py::discover_layout` reads
   `.git` directly — directory, `gitdir:` pointer file, `commondir` — and starts no Git
   process at all, because `git rev-parse` is exactly where `core.worktree` gets a vote. It
   handles the four real shapes (plain `git init`, clone, `--separate-git-dir`, linked
   worktree) and refuses ambiguity rather than guessing. `inspect_repository` consequently
   requires a repository *root*: a subdirectory is refused outright instead of being widened
   through Git. `core/enrolment.py` re-diagnoses that case from the filesystem so the
   operator still gets "this is a subdirectory, enrol the root" rather than "not a
   repository".
2. **Repository-local configuration is default-deny.** `audit_local_config` parses
   `.git/config` — and `config.worktree` when `extensions.worktreeConfig` is set — with
   `git config --file <path> --no-includes --list -z`, which performs no repository
   discovery and loads no effective configuration, so the parse itself cannot be steered.
   Every key is checked against a small allow-list of what a normal repository needs;
   anything else is refused, which is what makes the *next* key Git invents safe before
   anybody hears about it. Values that decide identity are validated rather than merely
   allowed: `core.bare`, `core.repositoryformatversion`, and `core.worktree` — which must
   resolve to the enrolled tree, the legitimate submodule case, and is refused when it
   points anywhere else. Include directives are refused rather than followed, because an
   include is a way to add configuration the audit would never see.
3. **Every invocation is bound, and recursion is explicit.** `GitRunner` passes
   `--git-dir` and `--work-tree` from the validated layout, so no configuration value,
   working directory, or enclosing repository can change which tree is inspected. Each
   repository's own status runs with `--ignore-submodules=all`, so Git never chooses to
   descend; submodules are enumerated from the index (`git ls-files -s`, the authoritative
   record — `.gitmodules` is rewritable tracked content) and each initialised child goes
   through the same five steps: discover layout, audit config, bind a runner, run status,
   recurse. One walker does configuration validation *and* dirtiness collection, because
   keeping them apart is precisely what produced rounds two and four.

**Consequences of walking rather than descending.** The signals `--ignore-submodules=none`
used to provide are reconstructed from each child's own inspection, which is strictly more
information: the child's HEAD is compared against the gitlink OID recorded in the parent's
index, and assume-unchanged and skip-worktree paths are collected at every level — something
no parent porcelain has ever reported. `submodule.<name>.ignore` and `.gitmodules`'
`ignore = all` are no longer overridden; they are simply never consulted, because nothing
asks Git to descend. Nested paths are prefixed so a refusal or a dirty report names *where*.

`--ignore-submodules=all` stops Git spawning a child status, but it does **not** stop Git
reading a submodule's configuration file — an unparseable one aborts the parent's status
with exit 128. So a repository's own status is the last step of its walk rather than the
first: by the time it runs, every configuration file it will touch has been through the
allow-list.

**Fail-closed rules.** A check that did not run is never a pass. A submodule whose
configuration cannot be read is refused, not skipped. Nesting deeper than the walker will
follow is refused, not truncated — a bound that silently stops checking is a bypass with a
limit constant next to it. A gitlink resolving outside its own superproject, or onto a git
directory already being walked, is refused. A Git too old to report configuration scopes is
refused rather than accepted with the check quietly returning "found nothing".

The walk carries a flat budget as well as a depth cap, because the depth cap alone does not
bound it: the repository chooses how many gitlinks each level holds, and the same subtree can
be gitlinked from many paths, so `breadth ** depth` repositories is a shape the repository
can pick and none of those paths is a cycle. Exceeding the budget is a refusal, for the same
reason as the depth cap.

**Refusals never print values.** A configuration file can hold credentials —
`remote.<name>.url` routinely does — and a security refusal that prints what it refused is a
new problem rather than a fix. Refusals name keys and paths only, and an included file's
contents never reach diagnostics at all, because includes are refused before they are read.

**Honest scope note on `remote.<name>.uploadpack`.** The allow-list refuses it, as
unsupported repository-controlled behaviour. It is *not* a confirmed current exploit: in the
direct reproduction against M2A's command set it did not execute, because nothing in M2A
contacts a remote. It is listed here so the record does not overstate what was demonstrated.

## Known unresolved: the allow-list does not close the family, and M2A must not merge

A fifth adversarial round, against the three-control design above, confirmed the same class
again — this time **inside the allow-list that replaced the deny-list**. These are open. They
are recorded here rather than patched, because patching them one key at a time is the
approach that failed four times.

**Confirmed Critical — `core.ignorecase` masks untracked work.** It is on the allow-list,
because `git init` writes it on macOS and Windows. On a case-sensitive filesystem it makes
the index name-hash case-insensitive, so an untracked file whose path is a case-variant of
any tracked path is not reported at all. Reproduced: a repository with `a.txt` and
`src/lib.py` tracked, plus untracked `A.TXT` and `src/LIB.py` holding arbitrary content,
reports `is_clean: true` with `untracked: []` after one `git config core.ignorecase true` —
and the files survive `git checkout -b`. It reproduces at submodule depth too. It violates
the allow-list's own stated admission rule, and it was admitted by exactly the reasoning
that admitted `core.worktree`: real tooling writes it. It cannot simply be refused, because
on a genuinely case-insensitive filesystem the value is correct and necessary, so refusing
it would refuse every macOS repository. `core.filemode = false` is the same shape, weaker.

**Confirmed High — `.gitattributes` moves the attack out of configuration entirely.** No
control in this design reads `.gitattributes` or `.git/info/attributes`, and neither needs a
configuration key. On any machine where `git lfs install` has been run — which puts
`filter.lfs.*` in the operator's *global* config, the scope this design trusts — a
repository that commits `* filter=lfs` makes `inspect_repository` spawn `sh -c <driver>`,
for repository-chosen paths, during what is documented as an inert read. Reproduced with
`GIT_TRACE` showing five `run_command` spawns. The repository chooses *whether*, *where* and
with what `%f`; the program body comes from the operator, which is why this is High rather
than Critical. A lossy global driver would additionally mask real modifications.

**Confirmed Medium — `.git/info/exclude` and a tracked `.gitignore` hide untracked work,**
reported as `is_clean: true`. Arguably correct Git semantics, but the design refuses
`core.excludesFile` explicitly as a masking vector while leaving the two default files it
points to unguarded, so at minimum the position is inconsistent and undocumented.

**The conclusion.** The three controls are a real improvement — they close rounds one
through four, and eight of nine mutations of them fail the suite. They are still not
sufficient, and the reason is structural rather than a matter of list maintenance: the
inputs that decide what `git status` finds are not confined to configuration files, and
among the configuration keys that do decide it, some are ones ordinary repositories must be
allowed to set. **M2A should not merge on this basis.** What it needs is a hermetic
execution boundary — inspection in a separate process with its own `HOME`, its own
configuration search path, and a read-only view of the repository, so that the answer does
not depend on enumerating which repository-controlled inputs matter. That is a larger piece
of work than a milestone gate, and it should be designed rather than bolted on.

**Residual risk.** These controls constrain what Git is told to do; they are not a sandbox.
Inspection still runs as the operator, with the operator's filesystem access and the
operator's global Git configuration — which is trusted by design, and which a compromised
developer machine could carry a hostile filter driver in. Symlinks inside a working tree are
followed by Git as Git normally follows them. If a future review confirms another critical
of this same family — repository-controlled configuration redirecting execution or belief —
the answer is not another allow-list entry: it is a hermetic execution boundary (a separate
process with its own `HOME` and a read-only bind mount, or an equivalent), and M2A should
not merge until that exists.

**What is pinned against regression.** `tests/integration/test_git_layout_boundary.py`
covers redirection at the top level and inside a submodule, two-level nesting through four
different hiding routes, default-deny polarity, include refusal, secret-free diagnostics, and
— just as important — that plain `git init`, a clone, `--separate-git-dir`, a linked
worktree, `extensions.worktreeConfig`, an ordinary submodule, and ordinary local settings all
still work. Nine mutations of the guards were applied; eight failed the suite and the ninth -- removing
`--no-includes` -- is an equivalent mutant, because `git config --file` does not expand
includes at all. A later round found five further guards that no test pinned (the
containment check on a child worktree, the cycle check, the gitlink-OID comparison, the
nested `unverifiable` paths, and the `core.bare`/format-version value validation); each now
has a test written from the proof-of-concept that survived.

*The default branch is never guessed, and it says where it came from.* Configuration, then
`refs/remotes/origin/HEAD`, then `None`. There is no fallback to "main" because guessing the
default branch means guessing which branch is protected, and a wrong guess is a
protected-branch mutation.

`init.defaultBranch` was the third source and has been removed. It names what `git init`
should call a *new* repository's first branch: a personal preference with no bearing on a
repository that already exists, and one the repository could override in its own config, so
a single appended line moved protection off `main`. What remains carries provenance --
`configured` or `origin_head` -- because only the first is the operator speaking, and the
branch this resolves is the branch that gets protected.

*Protected branches are the union of declared and discovered.* `awayctl policy` used to read
only the configured `defaultBranch`, so a project relying on discovery was reported as
having no protected branch while `awayctl repos` happily resolved a base on it. Taking the
union means a decoy written into a repository can only ever *add* protection, never move it,
and projects whose default branch cannot be determined at all are named explicitly -- "none"
and "we could not tell" are different answers.

That union was documented before it was true. `_resolve_default_branch` returned the
configured value the moment there was one and never consulted `refs/remotes/origin/HEAD`, so
the set was always `{declared}` *or* `{discovered}` and never both — meaning that for a
project with no declaration, a decoy written into the repository *replaced* the protected
branch rather than adding to it, which is the exact failure the paragraph claimed to
prevent. Discovery is now computed unconditionally and carried alongside the effective
answer. A union you cannot compute is not a union.

Declaring `defaultBranch` remains the only way to make protection independent of the
repository: for an undeclared project the protected set is still decided by a file inside
the tree being supervised, and no amount of unioning changes that.

*One unreadable repository does not stop the others.* A repository whose state cannot be
read is recorded as a per-project failure rather than raised: it is not enrolled, so it
grants no authority, but the rest of the run continues. Configuration mistakes still stop
everything, because the operator has to fix those before any of it means what they think it
means.

*Untracked files count as dirty.* They are what a broad `git add` sweeps up, and a task
that starts on top of them cannot afterwards say which changes were its own.

*So do paths Git has been told not to look at.* `git update-index --assume-unchanged` and
`--skip-worktree` are ordinary habits for a local config file, and `git status` then reports
nothing about the path however much its content differs from HEAD — while the difference
still survives `git checkout -b`. Those paths get their own refusal rather than being folded
into "dirty", because they may genuinely be unmodified: the problem is that nothing can
establish it. "We looked and found nothing" and "we were told not to look" are different
statements, and only the first supports a claim that a tree is clean.

*Minimum Git is 2.26*, set by `git config --list --show-scope`, which the
repository-configuration audit depends on. An older Git that rejects one of the options this
adapter relies on now says so, instead of reporting a perfectly good repository as not being
a repository and sending the operator to check a configuration that was fine.

*Force push has no configuration key at all.* It is denied by a rule, not by a flag being
false, so no future edit to a config file can enable it.

*Protected paths match component-wise.* `infra` covers `infra/main.tf` but not
`infrastructure.md`. Matching is case-sensitive, like Git's index; on a case-insensitive
filesystem `INFRA/x` is therefore not caught by `infra`. That is a known limitation rather
than a fold that would be wrong on Linux, and it is why protected paths are a backstop
rather than the primary control.

### M2B - locks, branches and worktrees (planned)

Repository locks so two tasks in one repository cannot interleave (`maxConcurrentTasks`
defaults to 1 precisely because this does not exist). Branch and worktree creation as
`claude-away/<task-id>-<slug>`, recorded on the attempt -- `base_commit`, `branch` and
`worktree_path` are already modelled and currently always `None`.

### M2C - commit/push boundaries and reconciliation (planned)

Commit and push gated by the policy M2A established. The concrete implementation of
`reconcile_expired`, which today records a takeover decision but cannot yet inspect the
repository that justifies it -- the single most valuable item in Milestone 2. Fault
injection that kills the process at each step of branch creation, commit and push, proving
restart never double-commits and never publishes work that did not pass its gate.

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

A second review pass found five more, all fixed here:

6. **The leased-task contract freeze was a TOCTOU** (high). The lease was read *before* the
   transaction opened, so a runner could acquire it in between and have its execution
   contract rewritten underneath it. Now checked under the write lock.
7. **A guard rollback inside a nested block surfaced as a raw sqlite3 error** (medium).
   `RAISE(ROLLBACK)` ends the transaction; if a caller swallowed it, the outer `COMMIT`
   failed with "cannot commit - no transaction is active" and nothing said that every write
   in the block had been discarded. Now a typed `DatabaseError` that says exactly that.
8. **`migrate()` read the schema version outside the write lock** (medium). Two processes
   opening a fresh database could both read 0 and both attempt migration 1.
   **This fix did not actually land — see the correction below.**
9. **`PRAGMA journal_mode = WAL` could escape as an unwrapped error** (medium). WAL is
   unavailable on some network filesystems. It buys read availability, not correctness, so
   the rollback journal is now an acceptable fallback rather than a failure to open.
10. **Lease expiry was computed from a clock sampled before `BEGIN IMMEDIATE`** (low). After
    a lock wait, the lease was issued already partly spent.

A third pass found four more, all fixed:

11. **Evidence type was never checked against requirement type** (critical). The rule that
    an LLM opinion cannot replace a deterministic check was enforced only on the
    *requirement* side, so an `EvidenceType.REVIEW` row discharged a required `command`
    requirement and the command never ran. Now a compatibility map, enforced at record time
    and again in the gate query.
12. **Evidence could be appended to a finished attempt** (medium), letting a sealed fail be
    followed by a pass after the fact.
13. **The idempotency fingerprint omitted `base_commit`, `branch` and `max_attempts`**
    (medium), so a materially different request replayed instead of raising.
14. **`awayctl doctor` reported the supported rate-limit suspend path as corruption**
    (medium). It is now reported as `task_awaiting_resume`, which is what it is.

A fourth pass found four more, all fixed:

15. **`transaction()` partially committed after a swallowed guard abort, and reported the
    opposite** (high). Once `RAISE(ROLLBACK)` ends SQLite's transaction, every later
    statement in the block runs in autocommit and lands individually -- while the error
    said all writes were discarded. A nested block now re-opens a transaction the moment it
    detects the abort, so the rest of the block stays under control and is discarded with it.
16. **`migrate()` released the write lock before the migration loop** (medium), leaving the
    concurrent-first-open race it was meant to close.
    **This fix did not actually land either — see the correction below.**
17. **A silent WAL fallback kept `synchronous=NORMAL`** (medium), which SQLite documents as
    corruption-prone with a rollback journal. Durability now follows the journal mode that
    was actually granted.
18. **The weakening guard called a proven requirement unproven once its attempt closed**
    (medium), blocking legitimate replanning. The gate is attempt-scoped because it asks
    "is this run finished?"; the guard asks "was this criterion ever met?", which is not the
    same question.

## Known limitations, stated plainly

* **A process with write access to the state file can drop the guard triggers.** SQLite
  offers no defence. `awayctl doctor` detects it; that is detection, not prevention. The
  mitigation is architectural: keep the state database outside every enrolled repository,
  mode `0600` (which `awayctl init` sets), and never put its path in an agent's prompt or
  environment.
* **"Append-only" holds against every in-package path and against ordinary external
  `UPDATE`/`DELETE`, but not against a deliberate external `INSERT OR REPLACE`.**
  `PRAGMA recursive_triggers` is per-connection and defaults to OFF; this package sets it,
  a foreign connection does not. Same architectural mitigation, and worth stating rather
  than implying a guarantee that does not survive contact with the `sqlite3` CLI.
* **The gate constrains shape, not judgement.** A bug inside the transition layer could
  still emit a well-formed transition. This is why the guards are pure functions tested
  independently, and why `DONE`/`CANCELLED` absorption is enforced a second time in the
  database.
* **Nothing here has executed a real task yet.** Every guarantee above is about state
  management. The claim "Claude Away works" is not yet true and this document does not
  make it.

---

## Correction: the `migrate()` race was documented as fixed while still present

Two entries above (8 and 16) claimed the concurrent-first-open race in
`Database.migrate()` had been closed. **It had not.** The claim survived into a commit
message, into this document, and through a merge to `main`.

### What actually happened

The fix was applied with a `str.replace()` whose target text did not match the file —
`ruff format` had joined a two-part string literal onto one line after the pattern was
written. `str.replace` returns the input unchanged when it matches nothing, so the patch
silently did nothing. The commit message was written from *intent* rather than from the
diff, and no test covered the behaviour, so nothing contradicted it. Commit `9242ec3`
touches `db.py` but its diff contains no change to `migrate()` at all.

Of the four fixes claimed in that commit, three landed and one did not. The other three
were verified present on `main` by inspection before this correction was written.

### The race, reproduced

Two threads open the same fresh database. The rendezvous fires *after* the first
transaction of `migrate()` commits, so both openers have read version 0 with no lock held
before either enters the migration loop:

```
opener A: ok      schema v1
opener B: raised  MigrationError: migration 1 (initial deterministic state core)
                  failed: table meta already exists
```

No sleeps: the synchronisation is a `threading.Barrier`, and the assertion (every opener
must converge) does not depend on timing.

### The fix

The version check and the migrations it gates now share one `BEGIN IMMEDIATE`
transaction. A second opener cannot read the version until the first has committed, so it
sees the new version and skips. Per-migration ledger rows are unchanged — each migration
still writes its own `schema_migrations` row.

Atomicity is *not* unchanged, and an earlier version of this section said it was. Every
pending migration now runs inside one transaction, so a crash while applying migration 3 of
a 1..3 batch rolls back to the starting version rather than to 2. That is a stronger
property, not a weaker one, and it is unobservable today because there is a single
migration — which is exactly why the inaccurate sentence survived. It is corrected here
rather than left to become wrong the first time a second migration lands.

`_ensure_migration_ledger()` also moved inside the error boundary so that a `database is
locked` during its DDL surfaces as a `MigrationError` rather than a bare
`sqlite3.OperationalError`.

Regression: `tests/recovery/test_migration_race.py`, verified to **fail** against the
pre-fix implementation (`git show origin/main:src/claude_away/core/db.py`) and pass after.
The ledger relocation is covered separately by
`TestLedgerFailureIsTyped::test_a_locked_database_during_ledger_creation_is_a_migration_error`
— it was claimed in the original commit message with no test behind it, which is precisely
the pattern the process lesson below was written about.

### The process lesson

A commit message claiming a bug is fixed is not evidence that the bug is fixed. Three
habits changed as a result, and they are the reason this entry exists rather than a quiet
amendment:

1. Every mechanical patch asserts that it changed something; a no-op replace is now an
   error, not a silent success.
2. A fix for a behavioural bug ships with a test that was **observed failing** against the
   old code. "Tests still pass" only proves the fix broke nothing.
3. Claims in this document are checked against the diff before they are written.
