# hermetic — hermetic resolution gate

Guarantees that a lockfile the merge engine regenerates inside a speculative spec
is a **deterministic function of the manifest prefix** — so a green spec lands the
*exact* dependency closure it tested, and never silently breaks the linear-history
trunk.

> No third-party dependencies — Python 3.11+ stdlib only.

## The problem it closes

Conductor's engine treats `regenerate(prefix) → lockfile` as pure. It isn't, unless
the resolver is hermetic. Without pinning, any of these makes a green spec land a
**different** closure than it validated:

- a patch version published to the registry mid-pipeline,
- the package-manager (npm/cargo/go) version drifting between runs,
- a platform/arch difference for lockfiles that embed platform-conditional resolutions.

The gate forces hermeticity and **fails closed** on anything it cannot prove.

## What it does

1. **Pin + record** the exact resolver version per ecosystem (a `ResolutionEpoch`).
2. **Freeze the registry** for the whole merge cycle (npm `--before` time-pin; cargo
   pinned index commit; Go `GOPROXY` snapshot; pnpm frozen mirror).
3. **Record platform/arch.**
4. **Assert byte-identical regeneration** — regenerate the lockfile N≥2 times in clean
   copies of the tree and compare digests; any mismatch ⇒ fail closed.
5. **CI probe** — re-resolve a known prefix twice and diff, catching resolver/registry
   drift before it can corrupt trunk.

## The `ResolutionEpoch`

One frozen resolution environment per merge cycle, committed for reproducibility:

```json
{
  "epoch_id": "2026-06-24",
  "platform": { "os": "linux", "arch": "x86_64", "libc": "glibc" },
  "resolvers": { "npm": { "tool": "npm", "version": "11.13.0" } },
  "registry_snapshot": {
    "npm": { "before": "2026-06-24T00:00:00Z", "registry": "https://frozen-mirror/npm" }
  }
}
```

## Usage

```bash
# 1. Capture the epoch (resolver versions + platform + snapshot refs)
python3 hermetic.py record . --epoch-id 2026-06-24 \
    --before 2026-06-24T00:00:00Z --registry https://frozen-mirror/npm \
    --goproxy https://goproxy.snapshot --index-commit <cargo-index-sha> \
    --out epoch.json

# 2. Inspect the exact frozen invocation without running it
python3 hermetic.py plan . --epoch epoch.json

# 3. Check fail-closed preconditions (versions present, snapshot frozen)
python3 hermetic.py verify . --epoch epoch.json

# 4. The gate: assert byte-identical regeneration (exit 1 = fail closed)
python3 hermetic.py gate . --epoch epoch.json

# 5. CI drift probe (re-resolve twice, diff)
python3 hermetic.py probe . --epoch epoch.json --runs 2
```

In the engine, the gate runs **before every green verdict**; the probe runs in CI on
each epoch change. Exit code `1` means "not hermetic — do not land."

## Fail-closed triggers (all → exit 1, no green)

- A required frozen field is missing from the epoch (e.g. no `before` / `index_commit`).
- Installed resolver version ≠ the pinned version.
- Host platform ≠ the epoch platform (for platform-sensitive ecosystems).
- The resolver errors, or the lockfile isn't produced.
- Digests differ across runs (the core nondeterminism signal).

## Gate dimensions

Beyond the byte-identical re-resolve, the gate asserts (all fail closed):

- **Resolver + host-runtime version** read *live* (never trusted from an image label) —
  e.g. npm **and** node, cargo **and** rustc.
- **Lockfile format version** emitted by the run (npm `lockfileVersion`, cargo `version = N`,
  uv `version`+`revision`, …) — catches a stealth resolver-major swap even when resolution
  is otherwise identical.
- **Absolute time-freeze only** — relative cutoffs (npm `--min-release-age`, ISO-8601
  durations, poetry `min-release-age`) are rejected; they drift between specs.
- **Platform** for platform-sensitive ecosystems; `normalize()` strips only documented
  volatile bytes (pip-compile header); Go diffs **both** `go.sum` and `go.mod`.

## Status of the ecosystem adapters

The **gate, fail-closed logic, and probe are final** and unit-tested (`test_hermetic.py`,
18 tests, fake adapters — no package managers required):

```bash
python3 test_hermetic.py
```

The per-ecosystem freeze facts have been **verified** (by the `hermetic-resolution-facts`
research pass) and folded in. Highlights:

- **npm** — `--before` is absolute-only (`--min-release-age` rejected); the `resolved` field
  bakes the registry host into lock bytes (pin one canonical `--registry` or set
  `omit-lockfile-registry-resolved`); a truly immutable mirror is required, not `--before` alone.
- **pnpm** — `pnpm install --lockfile-only` (never `pnpm update --lockfile-only`, which
  re-resolves the whole file since v10.9.0, and never `--force`); immutable mirror required.
- **yarn-berry** — `yarnPath` **wins** over `packageManager`; `yarn --version` is source of
  truth; needs `supportedArchitectures=all` for a host-independent lock; immutable mode off.
- **cargo** — `cargo generate-lockfile --frozen` + pinned index commit / vendor; assert with
  `--locked`; resolver-v3 / `incompatible-rust-version` and the `version = N` header are gated.
- **go** — `GOTOOLCHAIN=local` is the **primary** gate condition; `GOPROXY` snapshot with no
  `,direct`; diffs both `go.sum` and `go.mod`. (The earlier `GOFLAGS=-mod=mod` requirement and
  `GONOSUMCHECK` were wrong and have been removed.)
- **python** — `uv lock --exclude-newer <absolute>` (assert `uv lock --check`) is the
  deterministic path; **poetry** `min-release-age` fails open and is quarantined to a serial
  lane; **pip-tools** output is per `(os, arch, cpython)` and must regenerate on the identical
  target or fail closed.

Adding/refining an ecosystem = one `Adapter` subclass (declare `required_epoch_paths`,
`build_invocation`, `format_version`, `platform_sensitive`, `time_freeze_field`); the gate and
probe are unchanged.
