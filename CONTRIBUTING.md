# Contributing to Claude Away

Thanks for helping build Claude Away.

The project is pre-alpha, so the highest-value contribution is not “more autonomy.” It is making autonomy **more correct, recoverable, observable, and useful**.

## What we want early

- State-machine and crash-recovery edge cases.
- Claude Code plugin/hooks/workflows compatibility fixes.
- Cross-platform process/scheduler behavior.
- Minimal reproducible failures from real repositories.
- Verification strategies that reduce false `DONE` states.
- Graphify/Obsidian integration feedback.
- Scheduler simulations spanning 5-hour and weekly reset boundaries.
- Tests for idempotency, dirty git state, retries, and partial failure.

## What we will not merge

- OAuth credential scraping.
- Undocumented private Claude usage endpoints.
- Logic intended to bypass or evade provider limits.
- “Burn tokens until 100%” busywork generators.
- Default production deploys or protected-branch merges.
- Silent weakening/removal of failed acceptance criteria.
- Large prompt frameworks that duplicate state the deterministic core should own.

## Design rule

Ask one question when proposing a feature:

> Does this fact need model judgment, or can deterministic code own it?

If deterministic code can own it reliably, prefer that.

## Pull requests

Keep PRs narrow. Include:

1. the problem/failure mode;
2. the chosen behavior and why;
3. tests or evidence;
4. compatibility assumptions involving Claude Code versions/platforms;
5. any new safety boundary or permission the change requires.

Do not mix architectural refactors with unrelated feature work.

## Documentation

Document current behavior, not aspirational behavior, outside explicitly labeled roadmap/design sections. If a Claude Code behavior comes from upstream documentation, link the relevant official page.

## Current sources of truth

- Product behavior: [`docs/PRODUCT_SPEC.md`](docs/PRODUCT_SPEC.md)
- Component boundaries: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Task semantics: [`docs/STATE_MODEL.md`](docs/STATE_MODEL.md)
- Machine contracts: [`schemas/`](schemas/)

If code and a design document disagree, flag the mismatch rather than quietly choosing one.
