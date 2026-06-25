# ADR-0001: Reinvent the control plane, not git's object model

- Status: Accepted
- Date: 2026-06-24

## Context

Tens of AI agents integrate concurrently to one mainline. The brief explicitly invited
out-of-the-box thinking, "even reinvent git if needed." We studied jj (Jujutsu), Sapling,
Pijul, and the monorepo systems at Google (Piper/TAP) and Meta (Mononoke/Sapling).

## Decision

Keep git's git-compatible object model and GitHub's native enforcement surface; build a
disciplined **transactional control plane on top**. Do not reinvent git's data format, and do
not adopt jj as a hard dependency. jj's genuinely good ideas (a stable Change-Id, conflicts-as-
data, an op-log audit trail) are all VCS-agnostic and are grafted onto stock git without it.

## Consequences

- The "drop into any GitHub repo and just work" requirement is achievable only on standard git
  objects + GitHub primitives (Issues, PRs, CODEOWNERS, Rulesets, Merge Queue, Actions, audit log).
- We keep the entire auditability/tooling/release ecosystem.
- The defensible "reinvention" is the control plane (serialized speculative merge engine, conflict
  router, conflict-free changesets, provenance) — the discipline, not the data format.

## Alternatives considered

- **jj-native fleet** — rejected: a pre-1.0 dependency, a doubled VCS mental model, a remote-
  poisoning hazard, and a stateful single-writer service; its good ideas don't require it.
- **Reinvent git's object format** — rejected: discards the ecosystem the brief depends on and
  gains nothing the layer can't provide.
- **Authoritative event log above git** — rejected; see [ADR-0002](0002-github-is-single-source-of-truth.md).
