# Conductor — Design

> Status: **design + decision-gate**. This document is the single source of truth for
> the architecture. Discrete decisions are recorded as ADRs in [`docs/adr/`](docs/adr/);
> this doc is the narrative that ties them together. It consolidates four design passes
> (control-plane architecture → self-built core → quantitative contention model →
> hermetic resolution gate), each adversarially stress-tested.

---

## 1. Problem

Unattended, production-scale agentic development: tens of AI coding agents work
concurrently, each in an isolated git worktree. Isolation is solved. The unsolved
problem is **integration and governance** of all that work back to one mainline while
preserving — automatically, at scale — a clean auditable history, a CHANGELOG, GitHub
issue traceability, semantic versioning/releases, and a genuine single source of truth
(issue → branch → PR → commit → changeset → version → release, all linked).

"Just rebase to main" fails in production: no review gate, races between agents, no
atomic change units, history becomes a mess. Many agents *will* touch overlapping code,
so conflicts must not halt the pipeline.

## 2. The reframe

Three insights determine everything that follows.

1. **It's a distributed-systems serialization problem, not a git problem.** Worktrees
   solved file isolation. The remaining contention is over *shared mainline state* and
   *shared dependency closure*. The fix is a transactional integration layer in front of
   git, not a new VCS. See [ADR-0001](docs/adr/0001-reinvent-control-plane-not-git.md).

2. **Changelog/version conflicts are self-inflicted — remove them by construction.**
   N agents editing one `CHANGELOG.md`/version field guarantees conflicts. One *uniquely
   named file per change* (the Changesets pattern, reimplemented) makes aggregation
   commutative and conflict-free. See [ADR-0005](docs/adr/0005-conflict-free-changesets.md).

3. **Conflicts must be data, not blockers — but naive conflict-as-data livelocks.** A true
   conflict becomes a structured artifact routed to a resolver agent while the queue keeps
   flowing; a starvation-aware priority-aging guard bounds the eject/re-enter loop.

## 3. Architecture overview

A **GitHub-native serialization + governance layer.** Agents never push trunk and never
self-rebase. All work flows through one substrate: a **scope-partitioned speculative merge
engine.** GitHub is the only source of truth ([ADR-0002](docs/adr/0002-github-is-single-source-of-truth.md));
nothing vendor-built or off-the-shelf sits in the critical path ([ADR-0009](docs/adr/0009-no-third-party-critical-path.md)).

| Component | Role | Delivered as |
|---|---|---|
| `conductor` CLI | The entry point agents use instead of `git push`: builds the change, generates the changeset from issue+diff, stamps trailers, opens the linked draft PR, submits to the queue. Stateless. | CLI |
| Local guardrail hooks | In-session conformance (deny direct-push, require changeset/issue link before "done"). A guardrail, not a security boundary. | Claude Code hooks |
| Server-side governance gate | Authoritative enforcement that holds when hooks are bypassed: ruleset (PR required, linear history, required checks, merge queue, code-owner review on risk-tier paths). | GitHub Ruleset |
| Speculative merge engine | The core: scope lanes, speculative spec validation, batching + log₂(N) bisection, trunk advance as sole writer. | GitHub Actions running the reducer under the App token |
| Conflict router + starvation guard | Conflict-as-data, eject + resolver agent, priority aging, Nth-eviction scope-hold. | GitHub Action + resolver skill |
| Mechanical correctness verifier | Deterministic required checks: semver-vs-diff, changeset validity, issue-open-at-merge, footprint corroboration. | Required Actions check |
| Advisory cross-model reviewer | Non-blocking decorrelated review + changelog normalization; human gate on risk-tier paths. | Non-required Actions check |
| Versioning/changelog backend | Commutative changeset fold, standing release PR, atomic release transaction. | GitHub Action |
| Hermetic resolution gate | Makes lockfile regeneration deterministic; fails closed otherwise. **Built** — `tools/hermetic`. | CLI + CI probe |
| Provenance ledger | Rebuildable JSONL projection of agent identity/model/params; never authoritative. | committed JSONL + CLI |
| Bootstrap | One idempotent `/governance-setup` creates the App, ruleset, merge queue, workflows. | plugin + slash command |

## 4. The work-unit lifecycle

```
issue ──(Closes #N, verified via GraphQL closingIssuesReferences)──▶ PR
   └─(merge by engine)─▶ commit  [Conventional Commit + Change-Id + Agent-Id + Model trailers]
        └─(.conductor/changesets/<ULID>.cset.yaml)─▶ aggregated ─▶ release PR
             └─(merge)─▶ version bump ─▶ CHANGELOG ─▶ tag ─▶ GitHub Release ─▶ auto-close issue
```

1. **Spec → issue.** Orchestrator decomposes work into GitHub issues (the SSOT anchor) with
   a declared footprint used only as a scheduling hint.
2. **Dispatch.** One agent, one isolated worktree, branch `agent/<issue#>-<slug>`. No lease.
3. **Work + intent.** Agent writes code; `conductor` generates a uniquely-named changeset and
   stamps trailers. Agent never edits CHANGELOG, version fields, or lockfiles.
4. **Local gate.** Hooks block direct-to-main pushes and refuse completion without issue link
   + changeset + draft PR.
5. **Admission.** Deterministic checks → advisory cross-model reviewer → required human approval
   *iff* the diff touches a risk-tier path → approval-gated queue insertion.
6. **Scope routing.** Engine computes scopes; disjoint scopes run as parallel mini-queues,
   shared scopes serialize.
7. **Speculative validation.** PR tested as a spec (trunk + ordered prefix) *off* trunk; the
   hermetic gate ensures regenerated lockfiles are deterministic.
8. **Land or conflict.** Green + linear ⇒ trunk fast-forwards to the proven spec. True conflict
   ⇒ conflict-as-data + resolver, queue keeps flowing.
9. **Aggregate + release.** Each land re-aggregates changesets into the standing release PR;
   merging it is the atomic, gated release.
10. **SSOT close.** Released commits auto-close their issues; `conductor ssot verify` asserts the
    full chain.

## 5. The serialization core

A speculative merge queue is intrinsically a **stateful serialization service** — durable
queue order, an in-flight spec DAG, leases, flaky history, one atomic commit point. We refuse
both a vendor engine and an external server, and insist GitHub stays the SSOT. The resolution:

**Every durable state mutation is an append-only commit on an orphan ref, committed by a
fast-forward-only push whose parent is the exact tip the writer just read.** That FF-push is the
*only* serialization/commit primitive — Actions `concurrency` is a cost optimization, never a
correctness boundary.

> **Critical correction (caught in adversarial review):** GitHub has **no** identity
> compare-and-swap on refs. Update-a-reference takes only `{sha, force}`; `force:false` is a
> *fast-forward* check, not an expected-old-OID CAS. The original design leaned on a primitive
> that does not exist. See [ADR-0003](docs/adr/0003-ff-push-serialization-primitive.md).

```text
# durable state write — the only commit primitive
loop:
    S0   = read_tip(refs/conductor/lane/<id>)
    next = reduce(load(S0), events)            # pure, deterministic
    C    = commit(parent = S0, payload = next) # parent is EXACTLY what we read
    if push(C, ref, force=false):  break       # FF-only; GitHub serializes ref updates
    else:                          backoff      # someone advanced past S0 → re-read, re-reduce
# INVARIANT: candidate parent MUST come from a tip read in THIS attempt; if read-tip != S0
#            at push time → abort & rebuild. Never FF-accept a transition computed on old state.
```

**State model** (sharded so nothing global gets hot — see [ADR-0002](docs/adr/0002-github-is-single-source-of-truth.md)):

| Ref / file | Holds |
|---|---|
| `refs/conductor/order` | monotonic enqueue sequence + trunk last-known-good OID (written rarely) |
| `refs/conductor/lane/<id>` | per-lane queue, spec DAG, leases (logical-clock expiry), batches |
| `refs/conductor/spec/<hash>` | disposable speculative branches (trunk + prefix), GC-able |
| `refs/conductor/flaky/<shard>` | test outcomes keyed by `(test-id, spec-content-hash)` |
| `refs/conductor/verdicts/<pr>/<sha>` | governance audit log only — never an authorization token |
| `.conductor/changesets/<ULID>.cset.yaml` | per-work-unit changeset (unique path ⇒ no collisions) |
| git tags + GitHub Releases | write-once facts — their uniqueness is the release mutex |

**Trunk advance** is the one place identity (not FF) matters:

```text
git fetch origin main                                       # tracking ref = T
assert T == last_known_good
git push --force-with-lease=refs/heads/main:T  <spec_tip>    # concurrent advance fails the lease
# BACKSTOP (non-bypassable): Ruleset requires linear history + required check 'conductor-landed'.
#   A lost lease race can never write a non-linear or unverified trunk — worst case = wasted work.
```

## 6. Silent-revert elimination

GitHub's native Merge Queue silent-reverts because it optimistically lands a batch onto the
protected branch, then removes a failing member after the fact. Conductor **never writes trunk
speculatively** — a batch is built and tested entirely as a spec ref *off* trunk, and trunk only
fast-forwards to an already-green spec. A red batch is isolated by log₂(N) bisection on the spec
refs; trunk is simply never advanced. There is no trunk write to revert. Landing is a **linear
pushrebase** — a single-parent commit carrying the merged tree + the PR's authorship and provenance,
via the Git DB API (not a 2-parent merge commit, which `required_linear_history` forbids, and not
`createCommitOnBranch`, which cannot land arbitrary trees and corrupts binaries/submodules). Trunk is
linear-history-only — [ADR-0004](docs/adr/0004-speculative-spec-linear-trunk.md).

## 6.5 Batched landing, flaky-quarantine, and the soundness barrier — **built**

The tick batches a conflict-free prefix of `m` PRs into one spec; a green batch lands all `m` in one
linear advance (atomic — a lost lease lands none). On red, isolation judges each member *alone on
pristine trunk*: a standalone `DET_FAIL` is a culprit; a member green-alone but red-in-batch is an
**interaction**, ejected as "incompatible-with-#k" (keep lowest seq), never tagged a regression.

The soundness pivot (the adversarial pass caught this): a spec's tree hashes *source*, not the
execution environment, so **"same tree, different outcome" is not a flake — it is unresolved and fails
closed.** Landing requires a clean `DET_PASS` across repeated hermetic runs, or excusal by an
**independent signal** — a test's flip history on *PR-absent* trees (trunk / prior landed greens). A
flip seen only with a candidate present is attributed to that candidate, never used to excuse it — so a
real race can never be masked as a flake, and quarantine can only *downgrade* an eject to a hold, never
*upgrade* an intermittent red into a land. Held PRs are re-enqueueable (never permanently lost). The
window sizes `m` by AIMD; PRs age into a forced solo batch to prevent starvation; and every `N` at-risk
PRs a **full-suite barrier** (no quarantine excusal, repeated) bounds the blast radius of a
quarantined-test-gone-real or a test-impact miss. Built in `tools/conductor/{verdict,flaky,batch_engine}.py`.

## 7. Correctness governance

Presence is enforceable; correctness is partially enforceable. Three layers, LLM kept out of the
hard path ([ADR-0008](docs/adr/0008-correctness-over-presence.md)):

1. **Mechanical verifier (required, deterministic, no LLM):** semver bump ≥ the bump inferred from
   a public-API surface diff; changeset well-formed; `Closes #N` re-checked **OPEN at merge moment**;
   footprint corroborated by the diff. `Closes #N`/footprint are untrusted author input.
2. **Cross-model reviewer + normalizer (advisory, non-blocking):** a different-model-family reviewer
   decorrelates blind spots; the real block on risk-tier paths (auth/security/migrations/CI/lockfile/
   public-API) is a required human CODEOWNERS approval. On inference outage → fail closed to human
   review, never an org-wide stall.
3. **Gate binds to verified content:** strict "up to date with base" + verdicts addressed to head AND
   base SHA. Authority is the head-SHA check status; the verdict ref is audit-only.

## 8. Versioning / changelog backend

Self-built, language-agnostic ([ADR-0005](docs/adr/0005-conflict-free-changesets.md)). Each work-unit
drops a uniquely-named changeset declaring semver intent + changelog body + affected components + issue
link. Aggregation is a commutative semilattice fold (flatten-by-max, delete-on-consume) maintained as a
standing "Version Packages" release PR (a recomputable cache). Merging it is the atomic release: bump,
CHANGELOG, tag, Release, idempotent issue close — with tag-creation as the election mutex and the
consumed-fragment list persisted in a commit trailer (squash/rebase-safe). Per-ecosystem `VersionAdapter`
plugins (npm/cargo/gomod/pep621/gradle/maven/plain) keep the engine from ever parsing code.

## 9. Hermetic resolution gate — **built**

Lockfiles and generated code are **engine-owned derived artifacts**, not contended source files
([ADR-0006](docs/adr/0006-lockfiles-as-derived-artifacts.md)). Agents edit only manifest declarations;
the engine regenerates the lockfile *inside each spec*. That is sound only if regeneration is
deterministic, which requires a hermetic resolver: pinned package-manager version + frozen registry
snapshot + recorded platform. The gate asserts byte-identical regeneration before a green verdict and
**fails closed** otherwise; a CI probe re-resolves a known prefix twice and diffs. Implemented and
unit-tested in [`tools/hermetic`](tools/hermetic/).

> **Hermeticity extends to the test runtime (required by P2).** A spec's tree hashes *source*,
> not the execution environment. So flake-vs-real soundness requires the *tests* to run hermetically
> too — frozen clock, denied network, fixed RNG seed, bounded concurrency, pinned runner image. Only
> then does "same tree, different outcome" mean a genuine flake rather than an unresolved result that
> must fail closed. See §6.5 and `tools/conductor/verdict.py`.

## 10. The contention reality (honest)

Parallelism is **Amdahl-bounded by `f`** = the fraction of PRs touching shared resolved state
(lockfile/manifest/generated/CI), with ceiling `1/f` — independent of agent count. This is the most
important honest finding in the whole design:

```
S(N, f) = 1 / (f + (1 - f) / N)        ceiling = 1 / f
```

- **Unmitigated** (`f` ≈ 0.2–0.6 realistic): effective parallelism **1.6×–5×**, regardless of N. The
  global serial lane saturates (unbounded queue) at `f ≥ 0.06` by N=50.
- **Mitigated** (lockfile-as-derived + manifest-key lanes + dispatch steward): collapse → attenuation.
  Realistic delivered **~6–12×** at N=20–50 on a clean multi-manifest deterministic-resolver monorepo;
  **~2–3×** on single-manifest, poetry/pip, or version-bump-churn repos.

**Conductor's durable value is correctness** (always-green linear trunk, provenance, real changelog/
semver), with parallelism a bounded bonus set by the repo's dependency shape — which no merge engine
can fix. The lanes key on dependency-keys, and footprint-disjointness is a *soft scheduling heuristic*,
never a hard pre-dispatch lease ([ADR-0007](docs/adr/0007-scope-lanes-not-leases.md)). Run
[`tools/measure-f`](tools/measure-f/) on a target repo to get its real `f` before building the engine.

## 11. Packaging / install

One project-scope Claude Code plugin + a GitHub Actions bundle, pinned via `marketplace.json` (subdir +
SHA) for reproducible enforcement. Defense in depth across two planes: local hooks (bypassable, in-session)
and server-side Actions + Rulesets (authoritative, run regardless of who pushes). One idempotent
`/governance-setup` provisions everything on any repo. A least-privilege GitHub App is the sole privileged
identity and sole trunk writer.

## 12. Build plan (engine-first, no vendor stopgap)

| Phase | Deliverable | Proves |
|---|---|---|
| **Gate** | Measure `f` on target repos (`tools/measure-f`) | whether the engine delivers ~8× or ~2× here |
| **P0 ✅ built** | Serialization primitive + safety substrate: FF-push/lease append lib with the parent invariant, force-with-lease trunk advance, ruleset; tests proving stale-write **rejection** ([`tools/conductor`](tools/conductor/)) | the primitive works on real git = GitHub ref semantics |
| **P1 ✅ built** | Engine core: reducer + single-lane speculation, linear pushrebase landing, speculation-validity guard, crash-replay ([`tools/conductor`](tools/conductor/): `reducer.py`, `scope.py`, `engine.py`) | a green PR lands with provenance, no silent revert, survives a killed runner |
| **P2 ✅ built** | Batching + interaction-aware isolation + content-hash flaky-quarantine + AIMD window + soundness barrier ([`tools/conductor`](tools/conductor/): `verdict.py`, `flaky.py`, `batch_engine.py`) | flakes don't falsely eject **and a real race is never masked as a flake**; a poison PR isolates to itself; interactions aren't misattributed |
| **P3 ✅ built** | Multi-lane + global lane + write-API governor + reconciler ([`dispatcher.py`, `governor.py`, `lane_engine.py`, `hermetic_land.py`, `reconciler.py`]) | exactly-once land recovery; global lane owns shared state; near-linear on disjoint scopes |
| **P4 ✅ built** | Correctness governance ([`apidiff.py`, `changeset.py`, `issues.py`, `risktier.py`, `advisory.py`, `verifier.py`]) | gates are correctness gates (realized-tree diff, semver-vs-API, verified+open issue), not spoofable presence gates |
| **P5 ✅ built** | Version/changeset/release backend ([`semver.py`, `versionplan.py`, `adapters.py`, `release_planner.py`, `release_txn.py`]) | concurrent agents version + release + changelog **exactly once** (nonce-tag mutex, crash-recoverable) |

The **hermetic gate** (built) is a cross-cutting component the engine calls from P1 onward. P0–P1 are
independently shippable.

## 13. Risks & open decisions

- **Shared-file contention is the common case at tens of agents** — the parallelism ceiling is the repo's
  dependency shape. Mitigated, not removed.
- **A sound public-API differ per language is a multi-quarter effort**; degrades safely (over-bump + human flag).
- **Write-API / abuse limits are undocumented and dynamic**; the governor is tuned empirically.
- **`force-with-lease` is best-effort** (Ruleset-backstopped against incorrectness; pathological churn = wasted replans).
- **Python/poetry/pip and single-manifest repos collapse** to ~2–3×; treat as a precondition, not a hope.

Open decisions are tracked per-ADR and in the pass-3/4 outputs (risk-tier path policy, frozen-index mirror
budget, manifest-first enforcement, version-conflict arbitration policy, CI-fleet sizing).

## 14. Decision log

See [`docs/adr/`](docs/adr/) — ADRs 0001–0010 record each load-bearing decision, its alternatives, and
consequences.

## 15. Tools in this repo

- [`tools/measure-f`](tools/measure-f/) — shared-file contention measurement (the pre-P0 gate).
- [`tools/hermetic`](tools/hermetic/) — the hermetic resolution gate (built + tested).
- [`tools/conductor`](tools/conductor/) — **the engine, P0–P5 built** (84 tests vs real git):
  the serialization primitive + single-lane land (P0/P1), batching/flaky-quarantine/barrier (P2),
  multi-lane + global lane + governor + reconciler (P3), the mechanical correctness verifier (P4),
  and the changeset/version/release backend (P5). All Python stdlib-only; governance bootstrap in
  [`bootstrap/`](tools/conductor/bootstrap/).
