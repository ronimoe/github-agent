# ADR-0003: FF-push + force-with-lease as the serialization primitive

- Status: Accepted
- Date: 2026-06-24
- Supersedes: an earlier draft that assumed a ref compare-and-swap that does not exist.

## Context

The merge engine needs an atomic commit/serialization primitive on GitHub-native pieces only.
The original design assumed "compare-and-swap on a git ref with an expected-old-OID precondition,
evaluated atomically server-side." **Adversarial verification proved this false:** GitHub's
Update-a-reference API takes only `{sha, force}`; `force:false` is a *fast-forward* (descendancy)
check, **not** an identity CAS. FF-acceptance silently accepts a stale-but-linear update, so the
claimed lost-update-free property did not hold.

## Decision

- **Durable state writes** = append-only, fast-forward-only push to an orphan ref. Invariant: the
  candidate commit's parent MUST be the tip read in the *same* attempt; if the read tip changed at
  push time, abort and rebuild. Single-lineage append logs make FF-only sufficient and lost-update-free.
- **Trunk advance** (the one place identity matters) = `git push --force-with-lease` from a runner
  whose tracking ref was just fetched, behind a Ruleset requiring **linear history** + a required
  `conductor-landed` check as the non-bypassable backstop.
- Actions `concurrency` groups are a **cost optimization** (coalesce redundant ticks), never a
  correctness boundary.

## Consequences

- Sound for single-lineage logs — so every genuinely concurrent structure must be sharded into
  independent single-lineage refs (we do this per lane).
- Trunk advance is best-effort lease, not perfect CAS; a perfectly-timed concurrent advance just
  wastes work and re-plans. The Ruleset guarantees it can never produce a non-linear or unverified
  trunk.
- All GitHub reads are treated as stale-until-proven; evidence SHAs are re-validated against the
  spec ref before any irreversible action.

## Alternatives considered

- **Ref CAS with expected-old-OID** — rejected: does not exist in the GitHub API.
- **External lock service (etcd/Chubby)** — rejected: no external infra; violates GitHub-native.
