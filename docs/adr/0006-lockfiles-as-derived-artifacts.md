# ADR-0006: Lockfiles & generated code are engine-owned derived artifacts

- Status: Accepted
- Date: 2026-06-24

## Context

Lockfiles (package-lock.json, Cargo.lock, go.sum) and generated code (protobuf, OpenAPI, ORM
clients) are the dominant source of shared-file contention: every dependency-changing PR collides
on the same file, forcing a serialized global lane. Text-merging a lockfile is unsound (peer/
transitive conflicts are invisible to a key-union merge), and so is text-merging generated output.

## Decision

Treat lockfiles and deterministic generated artifacts as **engine-owned build outputs**, removed
from the agent diff and the scope map. Agents edit only the *source*: manifest dependency
declarations, or the `.proto`/`.sql`/schema. The engine regenerates the artifact **inside each
speculative spec** — lockfiles via a hermetic resolver, generated code by re-running the pinned
generator on the merged source (the generator *is* the merge function). The artifact is committed
only in the fast-forward land commit.

## Consequences

- Removes the shared file from contention; what remains is genuine dependency-*key* coupling.
- "Per-PR green implies green-after-land" holds **only** under a hermetic resolver — pinned
  package-manager version + frozen registry snapshot + recorded platform. This is enforced by the
  **hermetic resolution gate** (`tools/hermetic`), which fails closed otherwise.
- Requires a budgeted frozen-index mirror; non-deterministic resolvers (poetry/pip) must fail closed
  to a serialized lane.
- Whenever a regen moves any resolved version, dependents whose transitive closure changed must be
  re-tested.

## Alternatives considered

- **Agents edit lockfiles directly** — rejected: every dep PR collides; serializes the fleet.
- **Semantic 3-way lockfile merge** — rejected: unsound (constraint-satisfaction ≠ behavioral correctness).
- **Strip lockfile, regenerate only at land** — rejected: per-PR green would not imply post-regen green.
