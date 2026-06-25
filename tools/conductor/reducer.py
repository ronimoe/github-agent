"""reducer — the deterministic lane reducer (DESIGN.md §5, P1 + P2).

A pure fold over a lane's append-only event log into a `LaneState`. Crash-replayable:
a killed runner re-reads the log and reconstructs identical state, and a recorded
land/eject removes its PR from the queue so work is never double-applied.

P2 adds batched landing, conflict-as-data ejection variants, the fail-closed `held`
bucket (re-enqueueable — a flake-misclassified good PR is never permanently lost),
PR aging (anti-starvation), the AIMD window, and the soundness-barrier counters. The
window `m` and the at-risk counter are pure left-folds over the durable log, so they
are replay-deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FORCE_SOLO_K = 3   # after K non-landing participations a PR is forced to a solo (m=1) batch


# --- P1 events ---------------------------------------------------------------

def ev_enqueue(pr, branch, head, base, scopes, seq, trailers=None):
    return {"type": "enqueue", "pr": pr, "branch": branch, "head": head, "base": base,
            "scopes": list(scopes), "seq": seq, "trailers": trailers or {}}


def ev_landed(pr, merge, trunk_before, trunk_after):
    return {"type": "landed", "pr": pr, "merge": merge,
            "trunk_before": trunk_before, "trunk_after": trunk_after}


def ev_ejected(pr, reason):
    return {"type": "ejected", "pr": pr, "reason": reason}


# --- P2 events ---------------------------------------------------------------

def ev_batch_formed(batch_id, prefix, base_trunk, spec_tree, m):
    return {"type": "batch_formed", "batch_id": batch_id, "prefix": list(prefix),
            "base_trunk": base_trunk, "spec_tree": spec_tree, "m": m}


def ev_batch_landed(prefix, trunk_before, trunk_after, spec_tree, batch_id=None):
    return {"type": "batch_landed", "prefix": list(prefix), "trunk_before": trunk_before,
            "trunk_after": trunk_after, "spec_tree": spec_tree, "batch_id": batch_id}


def ev_intent_prepared(lane_id, batch_id, prefix, base_trunk, spec_tip, spec_tree, hermetic_digest=None):
    return {"type": "intent_prepared", "lane_id": lane_id, "batch_id": batch_id,
            "prefix": list(prefix), "base_trunk": base_trunk, "spec_tip": spec_tip,
            "spec_tree": spec_tree, "hermetic_digest": hermetic_digest}


def ev_batch_retried(batch_id, reason):
    return {"type": "batch_retried", "batch_id": batch_id, "reason": reason}


def ev_culprit_ejected(pr, failing_test_ids, spec_tree):
    return {"type": "culprit_ejected", "pr": pr, "failing": list(failing_test_ids),
            "spec_tree": spec_tree, "reason": "deterministic-regression"}


def ev_incompatible_ejected(minimal_set, kept_pr, ejected_pr, spec_tree):
    return {"type": "incompatible_ejected", "minimal_set": list(minimal_set),
            "kept": kept_pr, "pr": ejected_pr, "spec_tree": spec_tree,
            "reason": f"incompatible-with-#{kept_pr}"}


def ev_held(scope, reason, spec_tree, prs):
    return {"type": "held", "scope": scope, "reason": reason, "spec_tree": spec_tree,
            "prs": list(prs)}


def ev_unhold(pr):
    return {"type": "unhold", "pr": pr}


def ev_aged(prs):
    return {"type": "aged", "prs": list(prs)}


def ev_window_tuned(m):
    return {"type": "window_tuned", "m": m}


def ev_barrier_required(at_risk):
    return {"type": "barrier_required", "at_risk": at_risk}


def ev_barrier_passed(spec_tree, reps):
    return {"type": "barrier_passed", "spec_tree": spec_tree, "reps": reps}


def ev_barrier_failed(culprit_test_ids, last_known_good):
    return {"type": "barrier_failed", "culprit": list(culprit_test_ids),
            "last_known_good": last_known_good}


@dataclass
class PRRecord:
    pr: int
    branch: str
    head: str
    base: str
    scopes: tuple
    seq: int
    trailers: dict = field(default_factory=dict)
    age: int = 0
    forced_solo: bool = False


@dataclass
class LaneState:
    pending: list = field(default_factory=list)    # PRRecord, enqueue order
    landed: list = field(default_factory=list)     # {pr, merge}
    ejected: list = field(default_factory=list)    # {pr, reason}
    held: list = field(default_factory=list)       # PRRecord — out of queue, re-enqueueable
    at_risk: int = 0                               # at-risk PRs since the last barrier
    window: dict = field(default_factory=lambda: {"m": 1})
    last_good: str | None = None                   # last barrier/landed spec_tree
    prepared: list = field(default_factory=list)   # in-flight intents (prepared, not yet landed)

    def head(self):
        return self.pending[0] if self.pending else None


def reduce(events) -> LaneState:
    enq: dict[int, PRRecord] = {}
    order: list[int] = []
    landed, ejected, done, held_set = [], [], set(), set()
    age: dict[int, int] = {}
    prepared: dict[str, dict] = {}
    m, at_risk, last_good = 1, 0, None

    for e in events:
        t = e.get("type")
        if t == "enqueue":
            pr = e["pr"]
            if pr not in enq:                          # first enqueue wins; a duplicate is a no-op
                order.append(pr)                       # (never silently mutate a queued PR's head/scopes)
                enq[pr] = PRRecord(pr=pr, branch=e["branch"], head=e["head"], base=e["base"],
                                   scopes=tuple(e.get("scopes", [])), seq=e.get("seq", 0),
                                   trailers=e.get("trailers", {}))
        elif t == "landed":
            if e["pr"] not in done:
                landed.append({"pr": e["pr"], "merge": e["merge"]})
                done.add(e["pr"])
        elif t == "ejected":
            ejected.append({"pr": e["pr"], "reason": e["reason"]})
            done.add(e["pr"])
        elif t == "intent_prepared":
            prepared[e["batch_id"]] = e
        elif t == "batch_landed":
            fresh = [pr for pr in e["prefix"] if pr not in done]   # idempotent: dedup a replayed land
            for pr in fresh:
                landed.append({"pr": pr, "merge": e["trunk_after"]})
                done.add(pr)
            at_risk += len(fresh)
            last_good = e.get("spec_tree", last_good)
            prepared.pop(e.get("batch_id"), None)
        elif t in ("culprit_ejected", "incompatible_ejected"):
            ejected.append({"pr": e["pr"], "reason": e.get("reason", t)})
            done.add(e["pr"])
        elif t == "held":
            held_set.update(e.get("prs", []))
        elif t == "unhold":
            held_set.discard(e["pr"])
        elif t == "aged":
            for pr in e["prs"]:
                age[pr] = age.get(pr, 0) + 1
        elif t == "window_tuned":
            m = e["m"]
        elif t == "barrier_passed":
            at_risk = 0
            last_good = e.get("spec_tree", last_good)
        # batch_formed / batch_retried / barrier_required / barrier_failed: non-terminal

    pending = []
    for p in order:
        if p in done or p in held_set:
            continue
        rec = enq[p]
        rec.age = age.get(p, 0)
        rec.forced_solo = rec.age >= FORCE_SOLO_K
        pending.append(rec)
    held = [enq[p] for p in held_set if p in enq and p not in done]
    return LaneState(pending=pending, landed=landed, ejected=ejected, held=held,
                     at_risk=at_risk, window={"m": m}, last_good=last_good,
                     prepared=list(prepared.values()))
