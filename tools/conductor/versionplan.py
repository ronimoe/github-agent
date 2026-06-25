"""versionplan — the commutative changeset fold + dependent-bump version graph (P5, ADR-0005).

The fold is a per-component max over the semver semilattice (none<patch<minor<major): commutative,
associative, idempotent — so the plan is a pure recomputable CACHE of the consumed fragment set and
replays byte-identically. The dependent-bump graph propagates a bump to dependents (>= patch) via a
fixpoint with an SCC/cycle detector that SURFACES an error (never loops). Single-version mode
collapses to one node = join of all bumps.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import semver
from apidiff import NONE, PATCH


def fold(fragments) -> dict:
    """list[Fragment] -> {component: max level}. Commutative + idempotent."""
    bumps = {}
    for f in fragments:
        for comp, lv in f.bumps.items():
            bumps[comp] = max(bumps.get(comp, NONE), lv)
    return bumps


class VersionGraph:
    def __init__(self, versions: dict, deps: dict | None = None):
        self.versions = versions            # comp -> "1.2.3"
        self.deps = deps or {}              # comp -> [components it depends on]

    def _assert_acyclic(self):
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {c: WHITE for c in self.versions}

        def dfs(u):
            color[u] = GRAY
            for v in self.deps.get(u, []):
                if v not in color:
                    continue
                if color[v] == GRAY:
                    raise ValueError(f"dependency cycle through {u} -> {v}")
                if color[v] == WHITE:
                    dfs(v)
            color[u] = BLACK

        for c in list(self.versions):
            if color[c] == WHITE:
                dfs(c)

    def propagate(self, bumps: dict) -> dict:
        """Fixpoint: a bumped dependency forces each dependent to >= PATCH."""
        self._assert_acyclic()
        bumps = dict(bumps)
        dependents: dict = {}
        for comp, ds in self.deps.items():
            for d in ds:
                dependents.setdefault(d, []).append(comp)
        changed = True
        while changed:
            changed = False
            for dep, comps in dependents.items():
                if bumps.get(dep, NONE) > NONE:
                    for c in comps:
                        if bumps.get(c, NONE) < PATCH:
                            bumps[c] = PATCH
                            changed = True
        return bumps


def fragment_set_hash(ulids) -> str:
    return hashlib.sha256(json.dumps(sorted(set(ulids))).encode()).hexdigest()[:16]


def render_changelog(fragments) -> str:
    parts = []
    for f in sorted(fragments, key=lambda f: (sorted(f.bumps), f.body)):
        comp = ",".join(sorted(f.bumps)) or "_default"
        parts.append(f"- ({comp}) {f.body.splitlines()[0] if f.body else ''}")
    return "\n".join(parts)


@dataclass
class ReleasePlan:
    versions: dict = field(default_factory=dict)   # comp -> new version string
    bumps: dict = field(default_factory=dict)
    changelog: str = ""
    consumed: list = field(default_factory=list)   # fragment ulids (deduped, sorted)
    fragment_set_hash: str = ""
    change_ids: dict = field(default_factory=dict)  # ulid -> change_id (the ledger join key, #9)
