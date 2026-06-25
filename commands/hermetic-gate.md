---
description: Run Conductor's hermetic lockfile-resolution gate — asserts byte-identical lockfile regeneration and fails closed on drift.
---

Verify that lockfile regeneration in this repo is a deterministic function of the manifests
(so a speculative spec that's green can't land a different dependency closure on trunk).

Steps:

1. Record a resolution epoch (captures resolver + runtime versions, registry/snapshot, platform):
   ```
   conductor hermetic record . --epoch-id manual --out .conductor/epoch.json
   ```
2. Run the gate (regenerates twice in clean rooms and asserts byte-identical output):
   ```
   conductor hermetic gate . --epoch .conductor/epoch.json
   ```

Report PASS/FAIL per detected ecosystem. If it fails closed, explain the exact reason (resolver
or runtime version drift, a relative time-freeze that should be absolute, lockfile-format drift,
or a non-deterministic resolver like poetry/pip that must be quarantined to a serial lane). Note
that the real freeze (an immutable registry mirror) is an operational prerequisite the gate
assumes — it verifies determinism, it cannot manufacture it.
