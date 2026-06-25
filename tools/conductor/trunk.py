#!/usr/bin/env python3
"""trunk — the trunk-advance primitive (ADR-0003, ADR-0004).

Trunk is the one place identity (not just fast-forward) matters. The engine advances
trunk only to an already-verified-green spec that is a linear descendant of the
last-known-good tip, using `git push --force-with-lease=<branch>:<expected>`:

  * If trunk still equals `expected`, the advance succeeds (and, being a descendant,
    it is a clean fast-forward — trunk stays linear).
  * If a concurrent advance (or a human hotfix) moved trunk, the lease fails and the
    push is rejected. The engine then rebases the spec onto the new trunk and re-tests
    rather than clobbering — a lost race wastes work, it never corrupts trunk.

This is best-effort lease, not perfect CAS; the non-bypassable backstop is a GitHub
Ruleset requiring linear history + the required `conductor-landed` check (see
bootstrap/ruleset.json). A lost race can therefore never write a non-linear or
unverified trunk.
"""

from __future__ import annotations

from gitutil import WorkRepo


class TrunkAdvancer:
    def __init__(self, remote: str, branch: str = "main", repo: WorkRepo | None = None):
        self.branch = branch
        self.ref = f"refs/heads/{branch}"
        self.repo = repo or WorkRepo(remote)
        self._owns = repo is None

    def current(self) -> str | None:
        return self.repo.ls_remote(self.ref)

    def advance(self, new_tip: str, expected_old: str | None) -> tuple[bool, str]:
        """Advance trunk to `new_tip` iff it still equals `expected_old`. Returns
        (ok, reason). A False result means trunk moved — rebuild the spec and retry."""
        ok, err = self.repo.lease_push(self.ref, new_tip, expected_old)
        return ok, ("advanced" if ok else f"lease failed (trunk moved): {err}")

    def close(self):
        if self._owns:
            self.repo.close()
