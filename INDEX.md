# INDEX.md — Universal

House Rules provides shared operating rules and task-specific skills.
This file is its index.

- Repository instructions and explicit operator choices override House Rules when they conflict.
- Both stay subject to the host instruction hierarchy, permissions, access, and approval controls.
- A repository override may tighten or loosen any House Rules rule, including a security rule, and names the rule it changes.

## Always load

At session start and after every reset or compaction, read these files in full:

- [core rules](rules/core.md)
- [outcome rules](rules/outcome.md)
- [delivery rules](rules/delivery.md)
- [writing rules](rules/writing.md)
- [Git rules](rules/git.md)
- [priority labels](rules/priority-labels.md)
- [session writing rules](rules/session-writing.md)

## Load when the task needs it

- Artifact paths, naming, or placement: load [STRUCTURE.md](STRUCTURE.md).
- Project or stack defaults: load [PREFERENCES.md](PREFERENCES.md).
- Parallel agents, lanes, workflows, or background jobs: load [agent-lanes](skills/agent-lanes/SKILL.md).
- Evidence-based audit or due-diligence reports: load [audit-report-authoring](skills/audit-report-authoring/SKILL.md).
- Comparative benchmarks, evaluations, A/B tests, or tuning sweeps: load [bench-discipline](skills/bench-discipline/SKILL.md).
- Reviewing code, a commit, a diff, a branch, or a pull request: load [change-review](skills/change-review/SKILL.md).
- CI build time, cost, caching, or scheduling work: load [ci-build-optimization](skills/ci-build-optimization/SKILL.md).
- Choices needing operator input, decisions, or explanations: load [decision-brief](skills/decision-brief/SKILL.md).
- Design decisions, components, refactors, specs, or architecture reviews: load [design-canon](skills/design-canon/SKILL.md).
- Features, design documents, prototypes, spikes, or feature closeout: load [design-flow](skills/design-flow/SKILL.md).
- New or changed benchmark, evaluation, A/B test, or tuning plans: load [experiment-planning](skills/experiment-planning/SKILL.md).
- Concrete failures, regressions, incorrect results, or unexpected performance: load [failure-forensics](skills/failure-forensics/SKILL.md).
- Ambiguous work, specs, agent prompts, surprising output, or significant merges: load [finding-unknowns](skills/finding-unknowns/SKILL.md).
- Autonomous iteration toward an explicit target or acceptance checklist: load [goal-loop](skills/goal-loop/SKILL.md).
- Compaction, session end, continuation, handover, or durable external runs: load [handoff-continuity](skills/handoff-continuity/SKILL.md).
- Status, steering, progress on long-running work, or decision escalation: load [operator-protocol](skills/operator-protocol/SKILL.md).
- Any operator-facing text: load [operator-writing](skills/operator-writing/SKILL.md).
- Push preparation, review rounds, review fixes, or PR merges: load [pr-ready](skills/pr-ready/SKILL.md).
- New projects or deliberate reconsideration of project foundations: load [project-bootstrap](skills/project-bootstrap/SKILL.md).
- Assumptions, causal claims, or verification needing deliberate checkpoints: load [reasoning-moves](skills/reasoning-moves/SKILL.md).
- Rust implementation or crate scaffolding: load [rust-canon](skills/rust-canon/SKILL.md).
- Writing or reviewing specs, design documents, or system maps: load [spec-writing](skills/spec-writing/SKILL.md).
- Specifications, design documents, or their agent context: load [specification rules](rules/specifications.md).
- Dependency defects, upstream reports or fixes, or maintainer feedback: load [upstream-contribution](skills/upstream-contribution/SKILL.md).
- Creating, claiming, handing off, closing, coordinating, or checking tracked work: load [work-tracking](skills/work-tracking/SKILL.md).
