# measure-f

Measure **shared-file contention (`f`)** in a git repo's history to predict, *before
building anything*, how much real parallelism Conductor's merge engine can recover on
that repo.

> No third-party dependencies — Python 3.11+ stdlib (`tomllib`) + the `git` CLI.

## Why this exists

Conductor lets a fleet of AI agents land work concurrently, but the ceiling on parallelism
is **not** set by git — it's set by the repo's *shared resolved state*. Every PR that
touches a lockfile, a dependency manifest, generated code, or CI config must serialize
through one lane. That makes effective parallelism obey Amdahl's law:

```
S(N, f) = 1 / (f + (1 - f) / N)        hard ceiling = 1 / f
```

With realistic `f` of 0.2–0.6, the ceiling is **1.7×–5× no matter how many agents you add**.
The mitigations (lockfile-as-derived-artifact, regenerate-not-merge, manifest-key lanes)
shrink `f` — but only as far as the repo's genuine dependency coupling allows. This tool
measures where *your* repo actually sits, so the build/no-build decision is grounded in
data instead of hope.

## Usage

```bash
python3 measure_f.py /path/to/repo                 # human report
python3 measure_f.py /path/to/repo --json          # machine-readable
python3 measure_f.py /path/to/repo --ref main --since "12 months ago"
python3 measure_f.py /path/to/repo --max-commits 0 # analyze ALL history
python3 measure_f.py /path/to/repo --config rules.json
```

It analyzes **first-parent history** (each first-parent commit ≈ one merged PR), which
matches squash-merge, rebase-merge, and merge-commit workflows.

## The three `f` regimes it reports

| Regime | Meaning | What still serializes |
|---|---|---|
| `f_naive` | status quo: file-path lanes | any shared file (lockfile, manifest, generated, CI) |
| `f_derived` | lockfiles + generated become engine-owned outputs | manifests + CI config |
| `f_floor` | also use manifest-**key** lanes | CI config + genuine same-dependency-key conflicts |

The gap from `f_naive` to `f_floor` is the parallelism Conductor can recover. `f_floor` is
the number that matters — it's the irreducible serial fraction no merge engine can remove.

### Reading the verdict

- `f_floor ≤ 0.08` → **GOOD** (~8–12× at N=50). Build it.
- `0.08 < f_floor ≤ 0.20` → **MARGINAL** (~3–6×). Worth it for the *correctness* guarantees
  (always-green linear trunk, provenance, real changelog), less so for raw throughput.
- `f_floor > 0.20` → **COLLAPSE** (<3×). Adding agents past ~10 buys almost nothing;
  re-scope or restructure (e.g. split a single manifest into per-package workspaces).

## What it also surfaces

- **Per-class touch rate** — exactly how often PRs hit lockfiles vs manifests vs generated vs CI.
- **Global-lane saturation `N_max`** — the agent count beyond which the serial lane's queue
  grows unbounded (M/M/1; tune with `--tv-min` and `--pr-per-agent-hr`).
- **Hottest dependency keys** — the packages most often changed, i.e. where same-key
  arbitration (the irreducible conflict) will concentrate.

## Accuracy notes

- **File-class metrics are exact** (lockfile/manifest/generated/CI/source touch rates).
- **Dependency-key analysis is a heuristic estimate**, parsed from manifest diffs for
  **npm / cargo / go** (and a rough pyproject pass). Known biases, all *conservative*
  (they nudge `f_floor` up, predicting *less* parallelism):
  - JSON trailing-comma churn can flag an unchanged `package.json` dep line as changed.
  - The `generated` class is a path heuristic — extend it for your repo via `--config`.
- The root commit is counted as one "PR" (negligible on real histories).

## `--config` format

Extends (does not replace) the built-in classification:

```json
{
  "lockfiles": ["custom.lock"],
  "manifests": ["BUILD.bazel"],
  "ci_globs": [".buildkite/", "ci/"],
  "generated_suffixes": [".sql.go"],
  "generated_substrings": ["/proto-gen/"]
}
```
