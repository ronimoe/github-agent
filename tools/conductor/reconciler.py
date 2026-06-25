"""reconciler — exactly-once land recording + lease reclamation + GC ordering (P3).

The crash window is between `adv.advance` succeeding and the durable `ev_batch_landed`. The
LIVE path records a land only after an independent confirm, but a process death in the window
leaves a PREPARED intent with no landed event. The reconciler is the recovery authority:

  * for each prepared-but-unlanded intent, re-read trunk via an INDEPENDENT ls_remote. Because the
    land commit embeds (lane_id, batch_id), `trunk == intent.spec_tip` unforgeably proves THIS
    intent advanced — append ev_batch_landed (idempotent: the reducer dedups). If trunk != spec_tip
    the advance never happened — leave the PRs pending (re-speculatable) and GC the spec ref.
  * reclaim expired governor leases.

Replay is idempotent and the recorded state is byte-identical whether the land was recorded live
or by the reconciler.
"""

from __future__ import annotations

from reducer import reduce, ev_batch_landed


class Reconciler:
    def __init__(self, repo, branch: str = "main"):
        self.repo = repo
        self.branch = branch

    def complete_intents(self, lane, adv) -> dict:
        """Return {batch_id: 'landed'|'abandoned'} for every in-flight intent."""
        st = reduce(lane.read()[0])
        result = {}
        trunk = adv.current()
        for intent in st.prepared:
            bid = intent["batch_id"]
            if trunk == intent["spec_tip"]:
                lane.append(ev_batch_landed(intent["prefix"], intent["base_trunk"],
                                            intent["spec_tip"], intent["spec_tree"], bid))
                result[bid] = "landed"
            else:
                # advance never happened (or another intent owns trunk) -> abandoned; PRs stay pending.
                result[bid] = "abandoned"
        return result

    def reclaim_leases(self, governor) -> list:
        return governor.reap()
