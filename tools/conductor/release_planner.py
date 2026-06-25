"""release_planner — builds a ReleasePlan from changeset fragments, fenced against in-flight
consumption (P5-5). The plan is a pure cache of the consumed fragment set; the re-planning fence
EXCLUDES fragments already consumed by a landed-but-not-yet-done release (read from the release
journal), so no fragment is ever double-counted into two versions."""

from __future__ import annotations

import semver
from apidiff import NONE
from versionplan import fold, render_changelog, fragment_set_hash, ReleasePlan
from release_txn import reduce_release


def build_plan(fragments_with_ulids, current_versions, graph=None, single_version=False,
               zero_major=False, exclude_ulids=()) -> ReleasePlan:
    excl = set(exclude_ulids)
    active = [(u, f) for u, f in fragments_with_ulids if u not in excl]
    frags = [f for _, f in active]
    ulids = [u for u, _ in active]

    raw = fold(frags)
    if single_version:
        raw = {"_default": max(raw.values()) if raw else NONE}
    if graph is not None:
        raw = graph.propagate(raw)

    versions = {}
    for comp, lvl in raw.items():
        cur = current_versions.get(comp, "0.0.0")
        versions[comp] = semver.bump_version(cur, lvl, zero_major) if lvl > NONE else cur

    change_ids = {u: f.change_id for u, f in active if getattr(f, "change_id", None)}
    return ReleasePlan(versions=versions, bumps=raw, changelog=render_changelog(frags),
                       consumed=sorted(set(ulids)), fragment_set_hash=fragment_set_hash(ulids),
                       change_ids=change_ids)


def in_flight_consumed(release_log) -> set:
    """Ulids consumed by landed-but-not-yet-done releases — the fence for the next re-plan."""
    st = reduce_release(release_log.read()[0])
    out = set()
    for plan_hash, ulids in st["consumed_by_plan"].items():
        if plan_hash not in st["done"]:
            out.update(ulids)
    return out
