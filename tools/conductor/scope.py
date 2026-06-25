"""scope — name-only-diff scope mapping (DESIGN.md §3, ADR-0007).

A PR's scope set is computed from a CODEOWNERS-style path-glob map over the *names* of
the files it changes — never by parsing code, so it works identically across languages.
Scopes drive lane assignment (P3). In P1 the scope set is recorded with the enqueue event;
the heuristic that batches likely-compatible PRs is a soft scheduling hint, never a lease.
"""

from __future__ import annotations

import fnmatch

ROOT_SCOPE = "<root>"


class ScopeMap:
    def __init__(self, rules):
        # rules: list of (glob, scope_id), most-specific-wins by declaration order.
        self.rules = list(rules)

    @classmethod
    def from_dict(cls, d: dict) -> "ScopeMap":
        return cls(list(d.items()))

    def scopes_for(self, paths) -> list:
        """Return the sorted set of scopes a change to these paths touches."""
        out = set()
        for p in paths:
            for glob, scope in self.rules:
                if fnmatch.fnmatch(p, glob):
                    out.add(scope)
        return sorted(out) if out else [ROOT_SCOPE]

    def overlap(self, a_paths, b_paths) -> bool:
        """Do two changes share any scope? (the disjointness signal for lane batching)."""
        return bool(set(self.scopes_for(a_paths)) & set(self.scopes_for(b_paths)))
