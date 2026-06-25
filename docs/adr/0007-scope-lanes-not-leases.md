# ADR-0007: Scope lanes keyed on dependency-keys; footprint is a soft heuristic, not a lease

- Status: Accepted
- Date: 2026-06-24

## Context

One design approach proposed *preventing* conflicts by predicting each agent's file footprint and
handing out hard leases before dispatch. But you cannot predict an exploratory LLM agent's edits
before it makes them, and semantic conflicts are orthogonal to file disjointness, so leasing cannot
shrink the queue's hard residue.

## Decision

- Partition work into **lanes** = connected components over the scope-overlap graph; disjoint scopes
  run as parallel mini-queues, shared scopes serialize.
- Lane on the **set of dependency keys** a change touches, not on file paths, so unrelated dep-adds
  run in parallel.
- Footprint-disjointness is a **soft scheduling heuristic** that orders and batches the queue — it
  **never** gates dispatch. Mispredictions are caught by the queue, not punished at write time.
- Any PR touching shared/global files (lockfiles, root manifest, generated, CI) is routed to a single
  monitored global lane with agent backpressure.

## Consequences

- The merge queue is first-class and fully resourced; it carries the irreducible semantic-conflict residue.
- The footprint heuristic helps batching but degrades to coarse batching on tangled monorepos — accepted.
- Lockfile/shared-file contention collapsing the global lane is the common case at scale; this is a
  work-decomposition constraint, surfaced by `tools/measure-f`, not something the engine can remove.

## Alternatives considered

- **Hard ownership leases (pre-dispatch)** — rejected: unpredictable LLM edits; a lock-cluster SPOF;
  degrades worst exactly on the tangled repos where it's needed.
- **Pure file-path lanes** — rejected: re-converge on shared manifests; miss transitive coupling.
