"""flaky — the independent flake signal (P2).

Lives on a SEPARATE state log (`refs/conductor/flaky/<shard>`) from the lane ref — same
identity-leased FF-append, same lost-update-freedom. The decisive soundness rule
(forced by the adversarial pass): a test may be classified flaky — and thus excuse a
candidate — ONLY from its flip history on **PR-ABSENT** trees (trunk / prior landed
greens, which contain no candidate under judgement). A flip seen only with a candidate
present is NEVER credited here; it is attributed to that candidate by bisection. This is
the independent signal that closes the self-referential masking hole, where a PR's own
red could excuse the PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field

N_OBS = 3   # minimum PR-absent observations before a test_id can excuse / be called stable


def ev_test_outcome(stable_test_id, base_tree, result, pr_absent, rep_index=0):
    return {"type": "test_outcome", "test_id": stable_test_id, "base_tree": base_tree,
            "result": result, "pr_absent": bool(pr_absent), "rep": rep_index}


def ev_quarantine(stable_test_id, flake_score, R, action):
    return {"type": "quarantine", "test_id": stable_test_id, "flake_score": flake_score,
            "R": R, "action": action}


@dataclass
class FlakyState:
    tests: dict = field(default_factory=dict)   # test_id -> {pa_pass, pa_fail, quarantined}

    def _e(self, tid):
        return self.tests.get(tid, {"pa_pass": 0, "pa_fail": 0, "quarantined": False})

    def observations(self, tid):
        e = self._e(tid)
        return e["pa_pass"] + e["pa_fail"]

    def established_flaky(self, tid, n_obs=N_OBS):
        """Flips BOTH ways on PR-absent trees, with enough observations."""
        e = self._e(tid)
        return e["pa_pass"] >= 1 and e["pa_fail"] >= 1 and self.observations(tid) >= n_obs

    def stable_pr_absent(self, tid, n_obs=N_OBS):
        """Only ever passed on PR-absent trees (with enough observations) — so an
        UNRESOLVED only-with-the-batch is a candidate-introduced regression, not noise."""
        e = self._e(tid)
        return self.observations(tid) >= n_obs and e["pa_fail"] == 0

    def quarantined(self, tid):
        return self._e(tid)["quarantined"]


def reduce_flaky(events) -> FlakyState:
    tests: dict = {}
    for e in events:
        t = e.get("type")
        if t == "test_outcome" and e.get("pr_absent"):
            rec = tests.setdefault(e["test_id"], {"pa_pass": 0, "pa_fail": 0, "quarantined": False})
            if e["result"] == "pass":
                rec["pa_pass"] += 1
            else:
                rec["pa_fail"] += 1
        elif t == "quarantine":
            rec = tests.setdefault(e["test_id"], {"pa_pass": 0, "pa_fail": 0, "quarantined": False})
            rec["quarantined"] = (e["action"] == "add")
    return FlakyState(tests)
