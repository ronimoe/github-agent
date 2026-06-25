---
description: Use when working with Conductor — the GitHub-native integration & governance layer for fleets of AI coding agents. Trigger when landing many agent PRs onto a single linear trunk safely, measuring merge parallelism, gating lockfile determinism, running the mechanical correctness verifier, driving the speculative merge engine, or cutting exactly-once releases. Also when the user mentions Conductor, the merge engine, flaky-quarantine, or the `conductor` CLI.
---

# Conductor

Conductor is a self-built, **GitHub-native** control plane that lets a fleet of unattended AI
coding agents — each in an isolated git worktree — land work onto a single mainline while keeping
a clean **linear history**, an accurate **CHANGELOG**, **GitHub-issue traceability**, **semantic
versioning**, and an auditable single source of truth. It reinvents the *control plane* around git,
not git itself: no vendor merge-queue and no off-the-shelf release package in the critical path.

## Core model (explain this when asked how it works)

- Agents **never push trunk and never self-rebase**. Each work unit is a GitHub issue → an isolated
  worktree branch → a PR with a unique changeset fragment + provenance trailers.
- A **scope-partitioned speculative merge engine** builds each candidate as a spec (`trunk + ordered
  prefix`) *off* trunk, tests it, and advances trunk only by a linear fast-forward to an
  already-green spec — so the native merge-queue **silent-revert mode is structurally impossible**.
- Durable state lives in orphan refs (`refs/conductor/*`) via identity-leased fast-forward push;
  trunk advances via `git push --force-with-lease` behind a linear-history Ruleset. **GitHub is the
  single source of truth**; the provenance ledger is a rebuildable projection.
- Conflicts become data (eject / hold / bisect, with aging to prevent starvation). A result that
  varies across content-identical hermetic runs is **unresolved → fail closed**, never an
  auto-flake; a real race is never masked.

## The `conductor` CLI (available on PATH once this plugin is enabled)

```
conductor measure-f [PATH]              # shared-file contention f → realistic parallelism ceiling
conductor hermetic record|gate|probe …  # deterministic lockfile gate (fails closed on drift)
conductor setup <owner/repo> [app-id]   # idempotent governance bootstrap (ruleset + required check)
conductor test                          # run the full engine + hermetic test suite
```

Prefer the slash commands `/conductor:measure-f`, `/conductor:hermetic-gate`,
`/conductor:setup-governance` for the common workflows.

## Driving the engine (it is a stdlib Python library; `remote` is a local path or a GitHub URL)

```python
from gitutil import WorkRepo
from statelog import StateLog
from batch_engine import BatchEngine
from verdict import from_green

repo  = WorkRepo("https://github.com/you/your-repo.git")
lane  = StateLog(repo.remote, "refs/conductor/lane/auth")
flaky = StateLog(repo.remote, "refs/conductor/flaky/auth")
engine = BatchEngine(repo, lane, flaky, lane_id="auth")

engine.enqueue(pr=42, branch="refs/heads/agent/42-add-login", head="<sha>", base="<trunk-sha>",
               scopes=["auth"], trailers={"closes": 42, "agent": "claude-agent-3", "change_id": "I7f3a"})

result = engine.tick(from_green(lambda pr, tree: run_my_ci(tree)))  # verdict fn = your hermetic per-PR CI
# result.action ∈ {batch_landed, culprit_ejected, interaction_split, held, retry, idle}
```

The Python modules live under `tools/conductor/` in the plugin root. P4 adds the mechanical
verifier (`verifier.py`: semver-vs-API, verified+open issue, realized-tree footprint); P5 adds the
changeset/version/release backend (`release_txn.py`: nonce-tag mutex, crash-recoverable).

## Honest limits (always surface these)

- **Parallelism is Amdahl-bounded** by the repo's shared dependency closure (`1/f`). Run
  `/conductor:measure-f` first; expect ~6–12× on clean multi-manifest monorepos, ~2–3× on
  single-manifest / poetry-pip / churn-heavy repos. Correctness is the durable value, not raw speed.
- The current build is validated against **real git** (whose ref semantics equal GitHub's); wiring
  the real GitHub REST/GraphQL + Actions behind the verdict/oracle/registry seams is operational
  hardening, not new design.
- The public-API differ is a sound **syntactic lower bound** (unsupported language or re-export ⇒
  over-bump + human gate), not a semantic analyzer.

Design rationale is in `DESIGN.md` and the ADRs under `docs/adr/` at the plugin root.
