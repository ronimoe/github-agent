#!/usr/bin/env python3
"""ci_runner — per-PR hermetic CI runner (issues #1, #6). Runs the repo's test command `reps`
times under the frozen runtime, runs a drift probe, and writes a `conductor-spec` report the tick
reads via the verdict seam. The workflow posts the report as a check-run on the spec head SHA.

The repo declares its test command in `.conductor/test-cmd` (a shell line) or via --cmd.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hermetic_test import run_reps, drift_probe  # noqa: E402


def load_cmd(workdir: str, override: str | None) -> str:
    if override:
        return override
    path = os.path.join(workdir, ".conductor", "test-cmd")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    raise SystemExit("no test command: pass --cmd or add .conductor/test-cmd")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Conductor per-PR hermetic CI runner.")
    ap.add_argument("--cmd", help="repo test command (overrides .conductor/test-cmd)")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--out", default="conductor-spec.json")
    a = ap.parse_args(argv)

    cmd = ["bash", "-lc", load_cmd(a.workdir, a.cmd)]
    deterministic, digests = drift_probe(cmd, a.workdir, runs=2)   # hermeticity check
    suite_reps = run_reps(cmd, a.workdir, reps=a.reps)             # pass/fail per rep
    report = {"suite_reps": suite_reps, "deterministic": deterministic,
              "digests": digests, "reps": a.reps}
    with open(a.out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps(report))
    # Always exit 0: pass/fail/flake is DATA for the engine's verdict, not this runner's exit code.
    # (A non-zero exit is reserved for runner/infra failure, which argparse/IO already raise.)
    return 0


if __name__ == "__main__":
    sys.exit(main())
