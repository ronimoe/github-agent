---
description: Measure shared-file contention (f) on a repo to estimate Conductor's realistic merge parallelism ceiling.
argument-hint: "[path-to-repo]"
---

Measure how much of this repo's change traffic touches shared state (lockfiles, manifests,
generated code, CI) — the `f` that Amdahl-bounds Conductor's parallelism at `1/f`.

Run:

```
conductor measure-f $ARGUMENTS
```

(If `$ARGUMENTS` is empty, it measures the current directory.)

Then summarize for the user:
- the measured `f`, broken down by class (lockfile / manifest / generated / CI / source);
- the realistic effective-parallelism envelope it implies;
- a verdict — is this a good fit (multi-manifest, low `f` ⇒ ~6–12× at scale) or a near-serial
  case (single-manifest / poetry-pip / version-bump churn ⇒ ~2–3×)?

Be honest: Conductor's value is correctness (green linear trunk, provenance, real changelog/
semver); parallelism is bounded by the repo's dependency shape, which no merge engine can fix.
