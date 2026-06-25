"""dispatcher — lane routing + the global shared-file lane (P3, ADR-0007).

Each PR is routed to ONE lane. Two safety/throughput rules:

  * GLOBAL LANE owns ALL shared/derived paths (lockfiles, root manifests, generated, CI). A PR
    touching any of them goes to the single serialized global lane; scoped lanes' specs EXCLUDE
    those paths, so safety for shared state does NOT depend on routing being perfect — a scoped
    PR that edits a global path is rejected and re-routed to global.
  * SCOPED LANES are connected components over the scope-overlap graph (union-find on the routed
    PRs' scope-sets). A bridging PR merges components; the lane id is the lexicographically-smallest
    scope in the component, so merges/routes are replay-stable and a stranded PR re-resolves to the
    surviving lane by recomputing find().

Routing is a parallelism HINT; the trunk lease is the safety serialization point (P0).
"""

from __future__ import annotations

import fnmatch

GLOBAL_LANE = "global"

# Shared/derived paths the global lane exclusively owns.
GLOBAL_BASENAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum", "go.mod",
    "uv.lock", "poetry.lock", "package.json", "Cargo.toml", "pyproject.toml",
}


def ev_pr_routed(pr, lane_id, scopes):
    return {"type": "pr_routed", "pr": pr, "lane_id": lane_id, "scopes": list(scopes)}


def is_global_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (base in GLOBAL_BASENAMES or path.startswith(".github/")
            or fnmatch.fnmatch(base, "*.lock") or "generated/" in path)


def touches_global(paths) -> bool:
    return any(is_global_path(p) for p in paths)


class _UF:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        lo, hi = sorted((ra, rb))        # lexicographically-smallest scope is the root => stable
        self.parent[hi] = lo


class Dispatcher:
    """Reduces the routing log into a lane assignment. Stateless beyond its registry StateLog."""

    def __init__(self, registry_log):
        self.log = registry_log

    def _uf(self, events):
        uf = _UF()
        for e in events:
            if e.get("type") == "pr_routed" and e["lane_id"] != GLOBAL_LANE:
                scopes = e["scopes"] or ["<root>"]
                for s in scopes[1:]:
                    uf.union(scopes[0], s)
        return uf

    def lane_for_scopes(self, scopes) -> str:
        scopes = list(scopes) or ["<root>"]
        uf = self._uf(self.log.read()[0])
        for s in scopes[1:]:
            uf.union(scopes[0], s)
        return "L:" + uf.find(scopes[0])

    def route(self, pr, scopes, paths) -> str:
        """Assign and durably record the lane for `pr`."""
        if touches_global(paths):
            lane = GLOBAL_LANE
        else:
            lane = self.lane_for_scopes(scopes)
        self.log.append(ev_pr_routed(pr, lane, scopes))
        return lane

    def current_lane(self, pr) -> str | None:
        """The CURRENT lane for an already-routed PR, following merge chains (re-resolve find())."""
        events = self.log.read()[0]
        rec = next((e for e in reversed(events) if e.get("type") == "pr_routed" and e["pr"] == pr), None)
        if rec is None:
            return None
        if rec["lane_id"] == GLOBAL_LANE:
            return GLOBAL_LANE
        uf = self._uf(events)
        scopes = rec["scopes"] or ["<root>"]
        return "L:" + uf.find(scopes[0])
