# ADR-0004: Speculative spec model + linear-history-only trunk

- Status: Accepted
- Date: 2026-06-24

## Context

Native GitHub Merge Queue silent-reverts: it optimistically lands a batch onto the protected
branch, then must remove a failing member after the fact — dropping siblings and rewriting history.
For an unattended fleet we need an always-green trunk with no after-the-fact reverts.

## Decision

Build and test each batch entirely as a **speculative spec ref** (`trunk + ordered prefix`) *off*
trunk. Advance trunk **only** by a linear fast-forward to an already-verified-green spec tip. A red
batch is isolated by log₂(N) bisection on the spec refs and ejected; trunk is simply never advanced
to a bad tip. Trunk is **linear-history-only**. Landing is therefore a **linear pushrebase**: a
single-parent commit carrying the merged tree (computed by an in-memory 3-way merge / create-tree)
plus the PR's authorship and provenance trailers, created via the Git Database REST API
(create-tree + single-parent create-commit + ref update).

> **Consistency note:** an earlier draft said "2-parent merge commit." That is wrong — a 2-parent
> merge commit makes trunk non-linear, which the `required_linear_history` ruleset forbids. The
> owner's linear-history lock resolves it to a single-parent pushrebase/squash land. (`tools/conductor`
> implements this.)

## Consequences

- Silent-revert is **structurally impossible** — there is never a speculative trunk write to revert.
- Requires linear-history-only trunk (a locked decision); needing real merge-commit *history* on
  trunk would require redesigning the land step.
- A green spec is landable only if its realized parent OID == its speculated parent OID (the
  speculation-validity guard); otherwise rebuild and re-test (enforced by `--force-with-lease`).
- `createCommitOnBranch` is unusable (cannot land arbitrary trees; corrupts binaries/submodules/modes).

## Alternatives considered

- **Native GitHub Merge Queue** — rejected: silent-revert, no flaky intelligence, no scope lanes.
- **Optimistic land-then-revert** — rejected: that is the failure mode we are eliminating.
