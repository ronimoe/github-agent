#!/usr/bin/env python3
"""engine — the P1 single-PR engine, now a thin wrapper over the verified P2 BatchEngine at
window m=1 (the m=1 path IS the P1 land path). This removes the duplicated land/advance/record
logic and gives P1 the same crash-safe `record_land` (intent → advance → confirm). The P1 seam
`green(pr, tree) -> bool` is preserved via the `from_green` adapter.
"""

from __future__ import annotations

from batch_engine import BatchEngine
from land import TickResult
from statelog import StateLog
from verdict import from_green

# Map BatchEngine's richer outcomes back onto the P1 vocabulary.
_ACTION_MAP = {
    "batch_landed": "landed", "culprit_ejected": "ejected", "interaction_split": "ejected",
    "ejected": "ejected", "held": "held", "retry": "retry", "idle": "idle",
    "barrier_red": "barrier_red", "barrier_passed": "barrier_passed",
}


class Engine:
    def __init__(self, repo, lane, branch: str = "main"):
        # A dedicated flaky log in a sibling namespace (avoids a ref D/F conflict with the lane ref).
        flaky_ref = "refs/conductor/p1flaky/" + lane.ref.rstrip("/").split("/")[-1]
        flaky = StateLog(repo.remote, flaky_ref)
        self._be = BatchEngine(repo, lane, flaky, branch=branch, budget=1, reps=1)
        self.lane = lane
        self.adv = self._be.adv

    def enqueue(self, pr, branch, head, base, scopes, trailers=None) -> None:
        self._be.enqueue(pr, branch, head, base, scopes, trailers)

    def tick(self, green, _after_build=None) -> TickResult:
        res = self._be.tick(from_green(green), _after_build=_after_build)
        return TickResult(_ACTION_MAP.get(res.action, res.action), res.pr, res.detail)
