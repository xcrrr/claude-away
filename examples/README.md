# Examples

JSON has no comment syntax and the config schema rejects unknown keys, so the annotations
that would normally sit inline live here instead.

Validate the example:

```bash
awayctl validate-config examples/config.example.json
```

## `config.example.json`

A deliberately conservative starting point. The parts worth understanding before you copy it:

**`stateDbPath`** — keep this **outside every enrolled repository**. It is the authority for
the entire run, and an agent working inside one of your repos should not be able to reach
it. Relative paths resolve against the config file's directory, never the process working
directory, so a stray `cd` cannot bring a second state database into existence.

**`capacity`** — these are soft targets, not quotas to spend. The headroom below 100% exists
so a task can finish verifying and write its checkpoint instead of dying at the rate-limit
cliff. `noBusywork` is a fixed `true`: refusing to manufacture work to consume quota is not
something the project offers as a toggle.

**`goals[].successCriteria`** — write these as things somebody could check, not as
aspirations. "Every public function in `payments/` has at least one test" is checkable.
"Improve testing" is not, and a planner given the second one will produce vague tasks.

**`execution.maxConcurrentTasks`** — leave at `1` for now. Parallel tasks are only safe once
repository locking exists (Milestone 2 in the [roadmap](../docs/ROADMAP.md)).

**`execution.leaseHeartbeatSeconds`** — must be at most half of `leaseSeconds`, which the
validator enforces. Any closer and ordinary scheduling jitter will expire a perfectly
healthy lease.

**`safety`** — the defaults are timid on purpose. `allowPush`, `allowMerge`, `allowDeploy`
and `allowDestructive` are all off. Turn something on only if you would be comfortable with
it happening at 3am with nobody watching. `protectedPaths` are paths that autonomous work
must not modify at all; CI configuration and infrastructure are good candidates, because a
change there affects far more than the task at hand.

**`priority`** — higher numbers are scheduled first, for both goals and tasks.
