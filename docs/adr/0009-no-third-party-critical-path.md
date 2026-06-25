# ADR-0009: No third-party products or off-the-shelf core packages in the critical path

- Status: Accepted
- Date: 2026-06-24

## Context

A merge engine could be assembled by wrapping a commercial merge-queue product (Aviator, Mergify,
Graphite) and an off-the-shelf release tool (`@changesets`, semantic-release, release-please). But
then Conductor is a *delivery stack*, not a new solution — "if we use third party, why reinvent?"
The goal is a new, self-contained, auditable system.

## Decision

Build the **merge engine, conflict router, flaky-quarantine, correctness verifier, and version/
changeset/release backend ourselves** — that is the IP. GitHub's own primitives (REST/GraphQL API,
Actions, Rulesets, Apps, Issues/PRs, and raw git/refs) are the **platform** and are allowed; they are
not third-party dependencies. Ordinary library dependencies that do not make merge decisions (YAML/
TOML/XML parsers for editing files; per-language toolchains for the public-API differ) are
acknowledged honestly but kept out of the merge-decision path. LLM inference is advisory only
([ADR-0008](0008-correctness-over-presence.md)).

## Consequences

- Full ownership, auditability, and no hidden vendor dependency or SaaS outage in the hard path.
- More to build: the engine and flaky-quarantine ship before any throughput is visible (engine-first,
  no vendor stopgap).
- The product's moat is correctness + the self-built control plane, not a repackaged vendor stack.

## Alternatives considered

- **Wrap a vendor merge-queue backend** — rejected: makes us a delivery stack; adds a paid third-party
  dependency to every repo.
- **`@changesets` / semantic-release / release-please** — rejected: off-the-shelf core packages; see
  [ADR-0005](0005-conflict-free-changesets.md).
