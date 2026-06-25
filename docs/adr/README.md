# Architecture Decision Records

Each ADR records one load-bearing decision for Conductor — its context, the decision, the
consequences, and the alternatives rejected. The narrative that ties them together is
[`DESIGN.md`](../../DESIGN.md).

Format: [Michael Nygard's](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions.html).
Status values: Proposed · Accepted · Superseded.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-reinvent-control-plane-not-git.md) | Reinvent the control plane, not git's object model | Accepted |
| [0002](0002-github-is-single-source-of-truth.md) | GitHub is the single source of truth | Accepted |
| [0003](0003-ff-push-serialization-primitive.md) | FF-push + force-with-lease as the serialization primitive | Accepted |
| [0004](0004-speculative-spec-linear-trunk.md) | Speculative spec model + linear-history-only trunk | Accepted |
| [0005](0005-conflict-free-changesets.md) | Conflict-free changesets, self-built | Accepted |
| [0006](0006-lockfiles-as-derived-artifacts.md) | Lockfiles & generated code are engine-owned derived artifacts | Accepted |
| [0007](0007-scope-lanes-not-leases.md) | Scope lanes keyed on dependency-keys; footprint is a soft heuristic | Accepted |
| [0008](0008-correctness-over-presence.md) | Correctness over presence; cross-model reviewer advisory | Accepted |
| [0009](0009-no-third-party-critical-path.md) | No third-party products in the critical path | Accepted |
| [0010](0010-provenance-ledger-projection.md) | Provenance ledger is a rebuildable projection | Accepted |
