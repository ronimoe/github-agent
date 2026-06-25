# ADR-0002: GitHub is the single source of truth

- Status: Accepted
- Date: 2026-06-24

## Context

A speculative merge queue needs durable state (queue order, an in-flight spec DAG, leases,
flaky history). The tempting move is an authoritative event log or database "above" git. That
manufactures a permanent dual-source-of-truth — the deepest flaw found in the design review.

## Decision

GitHub (git refs + API) is the **only** source of truth. Durable state lives in append-only
orphan refs under `refs/conductor/*`, **sharded by mutability and per-lane** to avoid a single
global hot ref. A committed JSONL provenance ledger is a **rebuildable projection only**; if it
ever disagrees with GitHub, GitHub wins and we regenerate it ([ADR-0010](0010-provenance-ledger-projection.md)).

## Consequences

- No split-brain. Every production escape hatch — incident revert, hotfix, security force-push,
  dependabot — works, because there is no authoritative store fighting GitHub.
- Per-lane sharding removes the second global serialization point (the state ref saturating
  before trunk).
- The reducer keeps no RAM state; it reconstructs from refs + API after any crash.
- Trunk is ground truth for "what landed"; lanes read trunk, never their own state, to learn landings.

## Alternatives considered

- **Authoritative event log / external DB above git** — rejected: dual-source-of-truth, a new
  SPOF, install friction, and it violates the GitHub-native constraint.
- **Single global state ref** — rejected: saturates before trunk at tens of agents.
