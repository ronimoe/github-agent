"""land — shared land message, tick result, and the exactly-once land-recorder (P3).

Extracted from engine.py / batch_engine.py to kill the duplicated `_land_message` + `TickResult`
and to give both engines (and the reconciler) ONE crash-safe advance+record sequence:

    append ev_intent_prepared(lane, batch, spec_tip, ...)    # durable BEFORE the advance
    adv.advance(spec_tip, expected_old=trunk)                # the lease (P0)
    if ls_remote(trunk) == spec_tip:                         # independent confirm
        append ev_batch_landed(..., batch_id)               # idempotent (reducer dedups)

The land commit message carries `Conductor-Batch: <lane>/<batch>` so two lanes building the same
tree on the same parent still produce DISTINCT spec OIDs — `trunk == spec_tip` then proves THIS
intent advanced (deterministic OIDs would otherwise be forgeable). On crash between advance and
ev_batch_landed, the reconciler replays the confirm+record step idempotently.
"""

from __future__ import annotations

from dataclasses import dataclass

from reducer import ev_intent_prepared, ev_batch_landed
from trailers import TRAILER_LABELS, format_closes


def land_message(rec, lane_id=None, batch_id=None) -> str:
    tr = rec.trailers
    lines = [f"Land #{rec.pr}: {tr.get('subject', 'change')}", ""]
    cl = tr.get("closes")
    closes = cl if isinstance(cl, (list, tuple)) else ([cl] if cl else [])
    lines += format_closes(closes)                          # ONE Closes line per issue (#9 fix)
    for key in ("change_id", "agent", "model"):
        if tr.get(key):
            lines.append(f"{TRAILER_LABELS[key]}: {tr[key]}")
    if lane_id is not None or batch_id is not None:
        lines.append(f"{TRAILER_LABELS['batch']}: {lane_id or 'main'}/{batch_id or '0'}")
    return "\n".join(lines)


@dataclass
class TickResult:
    action: str               # batch_landed|culprit_ejected|interaction_split|held|retry|
    pr: int | None = None     #   barrier_passed|barrier_red|ejected|idle|landed
    detail: str = ""

    def __bool__(self):
        return self.action in ("batch_landed", "landed")


def record_land(lane, adv, intent_args, prefix, base_trunk, spec_tip, spec_tree, batch_id,
                hermetic_digest=None, lane_id="main", _after_build=None, guard=None):
    """Crash-safe advance+record. Returns (ok, reason). Appends ev_intent_prepared before the
    advance; on a confirmed advance appends ev_batch_landed (idempotent). A killed runner leaves
    an intent the reconciler completes; a lost lease leaves nothing landed.

    `guard()` is the at-land gate (issue #4) — the LAST check before the lease, e.g. re-confirming
    every `Closes #N` is still OPEN. If it returns `(False, reason)` the land aborts (no advance) and
    the reason is prefixed `guard:` so the caller can hold rather than retry."""
    lane.append(ev_intent_prepared(lane_id, batch_id, prefix, base_trunk, spec_tip, spec_tree,
                                   hermetic_digest))
    if _after_build is not None:
        _after_build()
    if guard is not None:
        ok, reason = guard()
        if not ok:
            return False, f"guard:{reason}"
    ok, _ = adv.advance(spec_tip, expected_old=base_trunk)
    if not ok:
        return False, "trunk advanced under us"
    # Independent confirm: the unique spec_tip is on trunk == our advance landed.
    if adv.current() == spec_tip:
        lane.append(ev_batch_landed(prefix, base_trunk, spec_tip, spec_tree, batch_id))
        return True, "landed"
    return False, "advance unconfirmed"
