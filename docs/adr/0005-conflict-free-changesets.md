# ADR-0005: Conflict-free changesets, self-built

- Status: Accepted
- Date: 2026-06-24

## Context

N agents editing one `CHANGELOG.md` or a shared version field guarantees merge conflicts — exactly
the churn we must eliminate. The Changesets *pattern* solves this, but the `@changesets/cli` npm
package is an off-the-shelf core dependency we have ruled out ([ADR-0009](0009-no-third-party-critical-path.md)),
and it is JS-only.

## Decision

Reimplement the pattern ourselves, language-agnostic. Each work-unit drops a **uniquely-named**
fragment `.conductor/changesets/<ULID>-<agent>-<sha8>.cset.yaml` declaring semver intent + changelog
body + affected components + issue link. Aggregation is a **commutative semilattice fold**
(flatten-by-max bump, delete-on-consume) maintained as a standing "Version Packages" release PR (a
recomputable cache). Merging it is the atomic release. Per-ecosystem `VersionAdapter` plugins
(npm/cargo/gomod/pep621/gradle/maven/plain) keep the engine from ever parsing code.

## Consequences

- Unique paths ⇒ N agents never collide on the changelog/version. Lost updates on the *plan* are
  impossible by construction.
- Squash/rebase-safe: the consumed-fragment list + computed versions are persisted in a commit
  trailer, so the release plan recovers regardless of merge strategy.
- A new language = one adapter; the engine is unchanged.
- We own and must maintain the fold and the adapters.

## Alternatives considered

- **`@changesets/cli`** — rejected: off-the-shelf core package; JS-only.
- **Everyone edits CHANGELOG/version** — rejected: guaranteed conflicts.
- **semantic-release** — rejected: third-party, and commit-message-driven rather than intent-file-driven.
