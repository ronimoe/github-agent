"""lane_engine — one lane = one BatchEngine + governor budget + (global lane) hermetic land-gate.

Wraps the verified BatchEngine. The global lane gets a `regenerator` so lockfiles are regenerated
in-spec and hermetically gated before a land (a blocked gate holds the batch). All lanes acquire a
spec_ci token from the shared WriteGovernor before ticking, so physical CI concurrency stays under
budget. Lanes land onto the ONE linear trunk via the same lease — the trunk lease is the cross-lane
serialization point; a loser re-speculates.
"""

from __future__ import annotations

from batch_engine import BatchEngine
from hermetic_land import HermeticBlocked, make_prebuild
from land import TickResult
from reducer import ev_held


class LaneEngine:
    def __init__(self, repo, lane, flaky, lane_id, branch="main", budget=8,
                 governor=None, regenerator=None, is_global=False):
        prebuild = make_prebuild(regenerator) if (is_global and regenerator) else None
        self.engine = BatchEngine(repo, lane, flaky, branch=branch, budget=budget,
                                  lane_id=lane_id, prebuild_tree=prebuild)
        self.governor = governor
        self.lane = lane
        self.lane_id = lane_id
        self.is_global = is_global
        self.adv = self.engine.adv

    def enqueue(self, *a, **k):
        self.engine.enqueue(*a, **k)

    def tick(self, verdict, _after_build=None) -> TickResult:
        tok = None
        if self.governor is not None:
            tok = self.governor.acquire(self.lane_id, "spec_ci")
            if tok is None:
                return TickResult("throttled", None, "governor budget exhausted")
        try:
            return self.engine.tick(verdict, _after_build=_after_build)
        except HermeticBlocked as e:
            # No ev_batch_formed was appended (regen raised during _spec_for) -> pending unchanged,
            # re-speculatable on a later green gate. Record the block for audit.
            self.lane.append(ev_held("batch", "hermetic-blocked", "", []))
            return TickResult("held", None, str(e))
        finally:
            if tok is not None:
                self.governor.release(tok)
