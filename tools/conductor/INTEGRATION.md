# Conductor — GitHub/Actions integration (slice 1)

This wires the built engine to a real GitHub repo so a labelled PR lands end-to-end through
Actions. It covers issues
[#3](https://github.com/ronimoe/github-agent/issues/3) (bot App),
[#2](https://github.com/ronimoe/github-agent/issues/2) (Actions execution),
[#1](https://github.com/ronimoe/github-agent/issues/1) (real CI verdict), and
[#6](https://github.com/ronimoe/github-agent/issues/6) (hermetic test runtime).

## The loop

```
PR labelled conductor:ready
      │
      ▼
conductor-ci.yml ──► ci_runner.py  (run the repo's tests N× under the frozen runtime + drift probe)
      │                     │
      │                     ▼
      │              conductor-spec.json
      ▼
posts a `conductor-spec` check-run (output = the per-rep report)
      │
      ▼  (check_run completed / cron)
conductor-tick.yml ──► tick_runner.py  (App token, sole trunk writer)
      │                       │
      │                       ▼
      │                CheckRunVerdict reads the report ──► BatchVerdict ──► BatchEngine.tick
      ▼                                                                          │
trunk advances via force-with-lease to the green spec ◄───────────────────────────┘
reconciler completes any crashed land exactly-once
```

## One-time setup

1. **Create the bot App** (issue #3). In *Settings → Developer settings → GitHub Apps → New*,
   using the permissions in [`bootstrap/app-manifest.json`](bootstrap/app-manifest.json)
   (contents/pull_requests/checks/statuses/issues: write). Generate a private key.
2. **Install the App** on the target repo.
3. **Configure the repo**:
   - Actions **variable** `CONDUCTOR_APP_ID` = the App's id.
   - Actions **secret** `CONDUCTOR_APP_PRIVATE_KEY` = the App private key (PEM).
   - Commit `.conductor/test-cmd` — a one-line shell command that runs your tests
     (e.g. `pytest -q` or `python3 -m unittest`).
4. **Install the governance backstop** (ruleset + required `conductor-landed` check, sole-writer App):
   ```
   conductor setup <owner>/<repo> <app-installation-id>
   ```
5. **Use it**: label a PR `conductor:ready`. `conductor-ci` runs the hermetic reps and publishes
   the verdict; `conductor-tick` reads it and lands the PR linearly.

Locally (outside Actions) you can mint a token with `tools/conductor/ghapp.py`
(`installation_token(app_id, key_path, installation_id)` — RS256 via `openssl`, no Python deps).

## What's proven vs. what needs a live run

| Piece | Status |
|---|---|
| Hermetic runtime + drift probe (`hermetic_test.py`) | ✅ unit-tested (`test_integration.py`) |
| CI report → `BatchVerdict` (`ci_verdict.py`) | ✅ unit-tested |
| App JWT assembly + openssl sign/verify (`ghapp.py`) | ✅ unit-tested |
| Engine land/flake/exactly-once (`batch_engine`, `reconciler`, …) | ✅ 66 tests vs real git |
| `ci_runner.py` / `tick_runner.py` glue | ⚠️ real but **not** end-to-end verifiable without a live repo + installed App + Actions |
| The two workflows + App | ⚠️ correct artifacts; validate on a sandbox repo |

## Security notes (hardening, issues #2/#6)

- Workflows pass no untrusted input into `run:` lines (SHA via `env:`, report via `jq --rawfile`).
- `tick_runner` uses PR head refs from the API as **argv** to git (not shell), but a hostile branch
  name should still be validated (`^[A-Za-z0-9._/-]+$`) before use — tracked for hardening.
- The real hermetic runtime (issue #6) pins the runner image by digest and **denies network**
  during the test step (`--network none` / firewall). The `ubuntu-latest` template does not isolate
  network; the drift probe is the empirical backstop, not a substitute for isolation.
