# conductor — P0 + P1: serialization primitive + single-lane engine

The engine foundation (see [`../../DESIGN.md`](../../DESIGN.md) §5–6 and
[ADR-0003](../../docs/adr/0003-ff-push-serialization-primitive.md) /
[ADR-0004](../../docs/adr/0004-speculative-spec-linear-trunk.md)). It implements — and
proves on **real git** — the ref primitives Conductor relies on (P0) and the
reducer-driven tick that lands a green PR linearly with provenance (P1), with no external
service and no database: the git ref store *is* the database.

## Why a local bare repo is a faithful test

GitHub has **no** expected-old-OID compare-and-swap on refs (the original design assumed
one; it does not exist). What we actually have is git's own ref-update semantics, which
GitHub implements identically:

- **fast-forward-only push** — a refspec without a leading `+` updates a ref only if the
  new commit descends from the current value; otherwise the server rejects it.
- **`--force-with-lease=<ref>:<oid>`** — the update is applied only if the ref still
  equals `<oid>` (an identity assertion).

Because these are git's semantics, a `git init --bare` repo behaves exactly as GitHub
would. The same code, pointed at a GitHub remote URL, is the production path. `remote` is
a path or a URL; nothing here is GitHub-specific.

## The two primitives

### Durable state — append-only log on an orphan ref (`statelog.py`)

Every state mutation is an append-only commit on `refs/conductor/...` whose parent is the
**exact tip the writer just read**, pushed with an identity lease. The invariant that
closes the lost-update hole:

> A candidate commit's parent is the tip read in *this* attempt. The push succeeds only
> if the ref still equals that tip. If another writer advanced it, the push is rejected;
> we re-read, re-reduce against the new tip, and retry.

State is one JSON-Lines blob per commit; reading the state is reading the blob at the tip.

```bash
python3 statelog.py --remote /path/to/repo.git --ref refs/conductor/lane/auth append '{"type":"enqueue","pr":1}'
python3 statelog.py --remote /path/to/repo.git --ref refs/conductor/lane/auth read
```

### Trunk advance — identity-checked, linear-only (`trunk.py`)

Trunk advances only to an already-green spec that descends from the last-known-good tip,
via `--force-with-lease`. If trunk moved (another lane landed, or a human hotfix), the
lease fails and the engine rebuilds the spec onto the new trunk — a lost race wastes work,
it never corrupts trunk. The non-bypassable backstop is the ruleset
([`bootstrap/`](bootstrap/)): linear history + the required `conductor-landed` check.

## P1 — the engine tick (`reducer.py`, `scope.py`, `engine.py`)

A deterministic, reducer-driven step that lands a green PR:

1. **`reduce()`** folds the lane's event log into a `LaneState` (pending queue / landed /
   ejected). Being a pure function of the durable log, it is crash-replayable — a killed
   runner reconstructs identical state, and a recorded land never double-applies.
2. **scope** computes a PR's scope set from a name-only diff (CODEOWNERS-style globs) — no
   code parsing, language-agnostic. Soft batching hint, never a lease (ADR-0007).
3. **`Engine.tick()`** takes the head PR, builds its spec against the *current* trunk with an
   in-memory 3-way merge (`merge-tree`), ejects on textual conflict, asks a verdict fn (the
   per-PR CI workflow, abstracted) if it's green, then **lands linearly**: a single-parent
   commit carrying the merged tree + provenance trailers, authored to the agent, advanced via
   `--force-with-lease`. If trunk moved between build and land, the lease fails — the
   **speculation-validity guard** — and the next tick rebuilds on the new trunk.

```bash
python3 statelog.py --remote /repo.git --ref refs/conductor/lane/auth read   # inspect a lane
python3 -m unittest test_conductor test_engine                                # P0 + P1
```

## What the tests prove (against real git)

`test_conductor.py` (P0):
- `write_commit` is deterministic (fixed identity + date ⇒ stable OID);
- a **stale-tip update is rejected, not clobbered** (lost-update-free);
- the append retry loop **loses no event** under a mid-flight concurrent advance;
- trunk advance holds its lease, **rejects a concurrent advance**, and stays linear.

`test_engine.py` (P1):
- a clean green PR **lands linearly** (single parent) with `Closes #N` / `Agent-Id` provenance,
  authored to the agent;
- a conflicting PR is **ejected** (conflict-as-data);
- the **validity guard retries** when trunk moves under the tick, then lands on the new tip;
- a landed PR **never double-lands** (reducer idempotency / crash-replay).

## Governance bootstrap (`bootstrap/`)

- [`ruleset.json`](bootstrap/ruleset.json) — the GitHub ruleset that makes the primitive
  safe even when a client misbehaves: protect `main` (linear history, required
  `conductor-landed` check, no deletion / no non-fast-forward), so a lost lease race can
  never produce a non-linear or unverified trunk.
- [`governance-setup.sh`](bootstrap/governance-setup.sh) — an idempotent `gh`-based
  skeleton that applies the ruleset and provisions the bot App. Requires an authenticated
  `gh` and repo admin; it is a template, not run by the tests.

## P2 — batching, flaky-quarantine, soundness barrier (`verdict.py`, `flaky.py`, `batch_engine.py`)

`BatchEngine.tick()` batches a conflict-free prefix of `m` PRs into one spec and lands all `m`
atomically on green. The verdict seam evolves from `green(pr,tree)->bool` to
`verdict(spec_tree, members, reps)->BatchVerdict` (per-test outcomes across hermetic reps); a
`from_green` adapter keeps the m=1 path byte-for-byte P1.

The soundness rule (forced by the adversarial design pass): a spec's tree hashes *source*, not the
execution environment, so an outcome that **varies across content-identical reps is unresolved and
fails closed** — landing needs a clean `DET_PASS`, or excusal by an **independent signal** (a test's
flip history on *PR-absent* trees, in `flaky.py`). A flip seen only with a candidate present is
attributed to that candidate, never used to excuse it — so a real race is never masked as a flake, and
quarantine can only downgrade an eject to a hold. Isolation judges singletons on *pristine* trunk so a
green-alone/red-together pair is an interaction (`ev_incompatible_ejected`, keep lowest seq), not a
culprit. Held PRs are re-enqueueable; AIMD sizes `m`; aging forces a starved PR solo; a full-suite
barrier bounds blast radius.

```bash
python3 -m unittest test_conductor test_engine test_p2     # P0 + P1 + P2 (27 tests)
```

`test_p2.py` proves, against real git: atomic batch land, validity guard on a batch, deterministic-red
ejection, **real-race-not-masked**, flake-excused-only-from-independent-signal, thin-evidence-holds,
**interaction-not-misattributed**, no-permanent-loss requeue, aging-forces-solo, AIMD, and the barrier.

## Honest residuals (P2)

- **Hermeticity is a precondition, not a proof.** The flake/real distinction holds only to the degree
  the *test* runtime is hermetic (frozen clock, no network, fixed seed, bounded concurrency, pinned
  image — DESIGN.md §9 extended to tests). A leak reopens the masking window for that leak.
- **Perturbation is probabilistic.** Repetition + seed/thread/clock perturbation raises the odds of
  surfacing a candidate-introduced race but can't guarantee it; the bound is blast-radius (N at-risk
  PRs to the next barrier), not prevention.
- **Cold-start fails closed → triage cost.** Until a test has ≥ N PR-absent observations, an
  intermittent red *holds* rather than guesses — trading throughput for soundness.

## P3 — multi-lane + global lane + governor + reconciler

`dispatcher.py` routes each PR to a scope-component lane (or the single global lane for shared/derived
paths it exclusively owns); `governor.py` caps concurrent CI/git-writes (the real ceiling) with TTL'd
leases; `lane_engine.py` wraps a per-lane `BatchEngine` and runs the global lane's in-spec lockfile
regen through the hermetic gate (`hermetic_land.py`) *before* the verdict; `reconciler.py` is the
exactly-once authority — it completes a prepared-but-unrecorded land (`land.py`'s intent → advance →
confirm) using the unforgeable `(lane,batch)`-stamped commit, and reclaims dead leases. Lanes serialize
only at the trunk lease; a loser re-speculates.

## P4 — mechanical correctness verifier (ADR-0008)

`verifier.py` folds V1–V5 on the **realized merge tree** (not a 2-dot author diff): V1 semver ≥ the bump
inferred by `apidiff.py` (per-language exported-symbol scan; unsupported/​re-export ⇒ UNCERTAIN ⇒
over-bump + human); V2 `changeset.py` strict parse; V3 `Closes #N` verified-linked **and** open
(`issues.py`, re-checked at land); V4 footprint corroboration; V5 conflict-markers + trailers. A FAIL
injects a `_mechanical` DET_FAIL into the verdict seam so it rides the same fail-closed path (quarantine
can't excuse it). The cross-model reviewer (`advisory.py`) is comment-only; human approval is bound to
the head SHA.

## P5 — changeset / version / release backend (ADR-0005)

`changeset.py` strict fragments (unique paths, no collisions) → `versionplan.py` commutative max-fold +
dependent-bump graph with cycle detection → `adapters.py` pure-text version writers (npm/cargo/pep621/
gomod/plain) → `release_txn.py` exactly-once release: a **nonce annotated-tag mutex** elects one winner,
side effects are gated on winning and journalled, create-if-absent + close-if-open make it
crash-recoverable; `release_planner.py` fences re-planning against in-flight consumed fragments.

```bash
python3 -m unittest test_conductor test_engine test_p2 test_p3 test_p4 test_p5   # 66 tests, real git
```

## Scope — P0–P5 all built

The full pipeline is implemented and tested against real git. What remains is operational hardening
(real GitHub API/CI wiring behind the seams, the real per-language API differ, fleet-scale soak tests)
— see DESIGN.md §13 risks and the residuals in each phase's design.
