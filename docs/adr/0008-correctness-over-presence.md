# ADR-0008: Correctness over presence; cross-model reviewer is advisory

- Status: Accepted
- Date: 2026-06-24

## Context

Governance can enforce that an artifact is *present* (a changeset exists, CI is green, someone
approved) but not that it is *correct* (the semver bump matches the diff, the changelog is honest,
the issue is real). A fleet optimizing to pass a presence gate emits plausible-but-wrong artifacts.
Separately, putting required LLM review in the hard merge path is a vendor-inference dependency that
can stall the whole org on an outage.

## Decision

Three layers:

1. **Mechanical verifier (required, deterministic, no LLM):** semver bump ≥ the bump inferred from a
   per-language public-API surface diff; changeset well-formed; `Closes #N` re-checked **OPEN at the
   merge moment**; footprint corroborated by the diff. `Closes #N` and footprint are untrusted author input.
2. **Cross-model reviewer + normalizer (advisory, non-blocking):** a different-model-family reviewer
   decorrelates blind spots; the normalizer rewrites changelog prose. Neither blocks.
3. **Human gate on risk-tier paths** (auth/security/migrations/CI/lockfile/public-API): required
   CODEOWNERS approval. On LLM inference outage the system **fails closed to human review**, never an
   org-wide merge stall.

## Consequences

- No vendor SaaS in the blocking merge path (a locked decision).
- The strongest semantic-review signal is non-blocking; risk-tier paths add human latency.
- A sound public-API differ per language is a multi-quarter effort and degrades safely (over-bump +
  human flag) — the part most likely to regress toward a presence gate.

## Alternatives considered

- **Required LLM review gate** — rejected: vendor dependency in the critical path; org-wide stall on outage.
- **Presence-only gates** — rejected: spoofable by an artifact-optimizing fleet.
