#!/usr/bin/env python3
"""batch_engine — P2: batched speculation, interaction-aware isolation, fail-closed flake
handling, AIMD window, aging, and the soundness barrier (DESIGN.md §5–7). See the module-level
docstring history in git; behavioural invariants are unchanged from the verified P2 spec.

P3-era refinements folded in: the land step goes through the shared crash-safe `record_land`
(intent-prepared → advance → confirm → batch_landed); `batch_id` is derived from the durable log
(replay-stable, globally unique with `lane_id`); a `prebuild_tree` hook lets the global LaneEngine
regenerate lockfiles in-spec BEFORE the verdict so the verdicted tree is the landed tree.
"""

from __future__ import annotations

from gitutil import WorkRepo
from land import TickResult, land_message, record_land
from reducer import (reduce, ev_enqueue, ev_batch_formed, ev_batch_retried,
                     ev_culprit_ejected, ev_incompatible_ejected, ev_held, ev_aged,
                     ev_ejected, ev_window_tuned, ev_barrier_passed, ev_barrier_failed)
from flaky import reduce_flaky, ev_test_outcome
from verdict import decide, BatchVerdict
from trunk import TrunkAdvancer


class BatchEngine:
    def __init__(self, repo: WorkRepo, lane, flaky, branch: str = "main",
                 budget: int = 8, n_barrier: int = 10, reps: int = 3,
                 lane_id: str = "main", prebuild_tree=None):
        self.repo = repo
        self.lane = lane
        self.flaky = flaky
        self.adv = TrunkAdvancer(repo.remote, branch, repo=repo)
        self.budget = budget
        self.n_barrier = n_barrier
        self.reps = reps
        self.lane_id = lane_id
        self.prebuild_tree = prebuild_tree     # (tree)->(tree', digest) — global-lane lockfile regen

    # --- intake --------------------------------------------------------------

    def enqueue(self, pr, branch, head, base, scopes, trailers=None) -> None:
        seq = len(reduce(self.lane.read()[0]).pending) + 1
        self.lane.append(ev_enqueue(pr, branch, head, base, scopes, seq, trailers))

    # --- helpers -------------------------------------------------------------

    def _tune(self, m_new):
        self.lane.append(ev_window_tuned(max(1, min(self.budget, m_new))))

    def _spec_for(self, base, members, batch_id=None):
        """Build a linear spec = base + members (conflict-free prefix). Optionally regenerate
        lockfiles per member (global lane) via prebuild_tree BEFORE commit_onto, so the verdict
        runs on the tree that lands. Returns (tip, tree, built_members, conflict_or_None)."""
        tip, built, digest = base, [], None
        for rec in members:
            clean, tree = self.repo.merge_tree(tip, rec.head)
            if not clean:
                return tip, (self.repo.tree_of(tip) if built else None), built, rec
            if self.prebuild_tree is not None:
                tree, digest = self.prebuild_tree(tree)
            tip = self.repo.commit_onto(tree, tip, land_message(rec, self.lane_id, batch_id),
                                        author={"name": rec.trailers.get("agent")})
            built.append(rec)
        return tip, self.repo.tree_of(tip), built, None

    def _verdict_classify(self, verdict, spec_tree, members, pr_absent=False):
        from verdict import classify
        bv = verdict(spec_tree, [m.pr for m in members], self.reps, pr_absent)
        if not isinstance(bv, BatchVerdict):
            bv = BatchVerdict(bv)
        return bv, classify(bv.per_test)

    def _batch_id(self, events):
        return f"{self.lane_id}-b{sum(1 for e in events if e.get('type') == 'batch_formed') + 1}"

    # --- one tick ------------------------------------------------------------

    def tick(self, verdict, _after_build=None) -> TickResult:
        events = self.lane.read()[0]
        lstate = reduce(events)
        fstate = reduce_flaky(self.flaky.read()[0])
        if not lstate.pending:
            return TickResult("idle")
        trunk = self.adv.current()

        if lstate.at_risk >= self.n_barrier:
            res = self._barrier(verdict, trunk, fstate)
            if res is not None:
                return res

        m = lstate.window.get("m", 1)
        head = lstate.pending[0]
        target = 1 if head.forced_solo else max(1, min(m, self.budget, len(lstate.pending)))
        members = lstate.pending[:target]
        batch_id = self._batch_id(events)

        spec_tip, spec_tree, built, conflict = self._spec_for(trunk, members, batch_id)
        if not built:
            self.lane.append(ev_ejected(conflict.pr, "merge conflict"))
            return TickResult("ejected", conflict.pr, "merge conflict")
        members = built
        self.lane.append(ev_batch_formed(batch_id, [r.pr for r in members], trunk, spec_tree, len(members)))

        _, classified = self._verdict_classify(verdict, spec_tree, members)
        dec = decide(classified, fstate)

        if dec.kind == "green":
            ok, reason = record_land(self.lane, self.adv, None, [r.pr for r in members], trunk,
                                     spec_tip, spec_tree, batch_id, lane_id=self.lane_id,
                                     _after_build=_after_build)
            if not ok:
                self.lane.append(ev_batch_retried(batch_id, reason))
                return TickResult("retry", None, reason)
            self._tune(m + 1)
            return TickResult("batch_landed", None, spec_tip)

        # not green: nobody lands this tick; age participants, shrink m.
        self.lane.append(ev_aged([r.pr for r in members]))
        self._tune(max(1, m // 2))
        if dec.kind == "hold":
            self.lane.append(ev_held("batch", dec.detail, spec_tree, [r.pr for r in members]))
            return TickResult("held", None, dec.detail)
        return self._isolate(members, trunk, spec_tree, verdict, fstate)

    # --- isolation (correctness-first linear scan) ---------------------------

    def _isolate(self, members, base_trunk, batch_spec_tree, verdict, fstate) -> TickResult:
        # Phase 1: judge each member ALONE on PRISTINE trunk.
        for rec in members:
            tip, tree, built, _ = self._spec_for(base_trunk, [rec])
            if not built:
                continue
            _, c = self._verdict_classify(verdict, tree, [rec])
            det_fail = [t for t, k in c.items() if k == "DET_FAIL"]
            if det_fail:
                self.lane.append(ev_culprit_ejected(rec.pr, det_fail, tree))
                return TickResult("culprit_ejected", rec.pr, "deterministic regression")
            unresolved = [t for t, k in c.items() if k == "UNRESOLVED"]
            unexcused = [t for t in unresolved if not fstate.established_flaky(t)]
            if unexcused:
                self.lane.append(ev_held("pr", "candidate-introduced-nondeterminism", tree, [rec.pr]))
                return TickResult("held", rec.pr, "candidate non-determinism")

        # Phase 2: no standalone culprit, yet the batch is red => an INTERACTION. Find the
        # smallest order-respecting boundary where adding a member turns the prefix red.
        tip, acc = base_trunk, []
        for rec in members:
            clean, tree = self.repo.merge_tree(tip, rec.head)
            if not clean:
                break
            tip = self.repo.commit_onto(tree, tip, f"iso #{rec.pr}")
            acc.append(rec)
            if len(acc) < 2:
                continue
            _, c = self._verdict_classify(verdict, self.repo.tree_of(tip), acc)
            if any(k == "DET_FAIL" for k in c.values()):
                kept = acc[0].pr
                self.lane.append(ev_incompatible_ejected([r.pr for r in acc], kept, rec.pr,
                                                         self.repo.tree_of(tip)))
                return TickResult("interaction_split", rec.pr, f"incompatible-with-#{kept}")

        self.lane.append(ev_held("batch", "unisolated-red", batch_spec_tree, [r.pr for r in members]))
        return TickResult("held", None, "unisolated red")

    # --- soundness barrier ---------------------------------------------------

    def _barrier(self, verdict, trunk, fstate) -> TickResult | None:
        tree = self.repo.tree_of(trunk)
        bv, c = self._verdict_classify(verdict, tree, [], pr_absent=True)
        for tid, reps in bv.per_test.items():           # feed the PR-absent independent signal
            for i, ok in enumerate(reps):
                self.flaky.append(ev_test_outcome(tid, tree, "pass" if ok else "fail", True, i))
        det_fail = [t for t, k in c.items() if k == "DET_FAIL"]
        if det_fail:
            self.lane.append(ev_barrier_failed(det_fail, trunk))
            return TickResult("barrier_red", None, "full-suite barrier failed")
        fresh = reduce_flaky(self.flaky.read()[0])      # one consistent snapshot, not per-test reads
        unresolved = [t for t, k in c.items() if k == "UNRESOLVED" and not fresh.established_flaky(t)]
        if unresolved:
            self.lane.append(ev_barrier_failed(unresolved, trunk))
            return TickResult("barrier_red", None, "barrier unresolved -> triage")
        self.lane.append(ev_barrier_passed(tree, self.reps))
        return None
