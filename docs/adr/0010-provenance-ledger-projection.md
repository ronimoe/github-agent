# ADR-0010: Provenance ledger is a rebuildable projection, not authoritative

- Status: Accepted
- Date: 2026-06-24

## Context

We need to record what GitHub cannot store natively about machine-authored changes: agent identity,
model + version, invocation params, and a plan-hash → code-hash binding (the same plan + model can
yield different code). But a second authoritative store would create a dual-source-of-truth that
fights GitHub ([ADR-0002](0002-github-is-single-source-of-truth.md)).

## Decision

Keep a thin, append-only, **committed JSONL provenance ledger** that is a **projection/cache** —
fully rebuildable from GitHub metadata + commit trailers. If it ever disagrees with GitHub, GitHub
wins and we regenerate it. Authority for any action is always the head-SHA required-check status,
**never** ledger content. Verdict records may be bound to GitHub OIDC run claims (run id, repo, SHA,
workflow); full Sigstore/Rekor attestation is an opt-in for high-assurance repos.

## Consequences

- Queryable provenance and audit (`conductor ssot trace/verify`, `agent --id`) with no split-brain.
- The ledger can lag or be rebuilt at any time; nothing breaks if it is deleted.
- Push access to `refs/conductor/verdicts/*` and the ledger must be restricted; the ledger is never
  read as an authorization token.

## Alternatives considered

- **Authoritative ledger / event log** — rejected: dual-source-of-truth; the deepest flaw found in review.
- **No provenance at all** — rejected: loses agent attribution and the audit trail production governance needs.
