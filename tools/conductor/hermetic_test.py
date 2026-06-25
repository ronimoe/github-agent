"""hermetic_test — run a repo's tests under a frozen runtime (issue #6).

P2 flake-soundness is only valid if the TEST runtime is hermetic: a result that varies across
content-identical runs must mean a genuine flake, not an un-frozen clock / network / scheduler.
This module pins what the process layer can (env: clock, locale, hash seed, a fixed RNG seed) and
provides a DRIFT PROBE — run a known command twice under the frozen env and diff — as the empirical
check that the runtime really is deterministic. The strongest freezes (network denial, pinned
runner image, bounded concurrency) are enforced at the container/Actions layer; see
`.github/workflows/conductor-ci.yml`. This module is the process-side half + the probe.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass

# The process-layer freeze. The runner enforces network denial + the pinned image.
FROZEN_ENV = {
    "TZ": "UTC",
    "SOURCE_DATE_EPOCH": "1750000000",
    "PYTHONHASHSEED": "0",
    "LC_ALL": "C",
    "LANG": "C",
    "PYTHONDONTWRITEBYTECODE": "1",
    "CONDUCTOR_SEED": "0",
    "CONDUCTOR_NETWORK": "deny",   # advisory; the runner/firewall enforces it
}


def frozen_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update(FROZEN_ENV)
    env.update(extra or {})
    return env


@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    code: int

    def digest(self) -> str:
        return hashlib.sha256(self.stdout.encode()).hexdigest()


def run_once(cmd, workdir: str = ".", env_extra: dict | None = None, timeout: int = 1800) -> RunResult:
    p = subprocess.run(cmd, cwd=workdir, env=frozen_env(env_extra),
                       capture_output=True, text=True, timeout=timeout)
    return RunResult(p.returncode == 0, p.stdout, p.stderr, p.returncode)


def run_reps(cmd, workdir: str = ".", reps: int = 3, env_extra: dict | None = None) -> list[bool]:
    """Run the test command `reps` times under the frozen env; return pass/fail per rep. The engine
    classifies all-pass => DET_PASS, all-fail => DET_FAIL, mixed => UNRESOLVED (fail closed)."""
    return [run_once(cmd, workdir, env_extra).ok for _ in range(max(1, reps))]


def drift_probe(cmd, workdir: str = ".", runs: int = 2, env_extra: dict | None = None):
    """Run `cmd` `runs` times under the frozen env and compare stdout digests. Returns
    (deterministic, digests). Non-identical output means the runtime is NOT hermetic for this
    command — the precondition for flake-soundness is violated, so the engine must fail closed."""
    digs = [run_once(cmd, workdir, env_extra).digest() for _ in range(max(2, runs))]
    return (len(set(digs)) == 1, digs)
