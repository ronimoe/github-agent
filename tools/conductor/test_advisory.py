#!/usr/bin/env python3
"""#10 tests: the cross-model reviewer is advisory/fail-open; the approval gate is head-bound and
agent-proof; the all-agent CODEOWNERS deadlock is flagged; and a NEEDS_HUMAN batch holds until a
valid head-bound approval, then lands (real git)."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from reducer import reduce, ev_unhold  # noqa: E402
from verdict import BatchVerdict, classify, decide  # noqa: E402
from flaky import FlakyState  # noqa: E402
from batch_engine import BatchEngine  # noqa: E402
import advisory  # noqa: E402
from advisory import (CrossModelReviewer, ev_human_approval, approval_satisfied,
                      deadlock_guard, approval_aware_overlay)  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


class ReviewerTests(unittest.TestCase):
    def test_review_comments_and_decorrelates(self):
        r = CrossModelReviewer(lambda p: "looks fine", model="sonnet", family="anthropic")
        ev = r.review("H1", "diff", author_family="openai")
        self.assertEqual(ev["type"], "advisory_review")
        self.assertTrue(ev["decorrelated"])                  # different family
        self.assertEqual(ev["summary"], "looks fine")

    def test_same_family_flagged_not_decorrelated(self):
        r = CrossModelReviewer(lambda p: "ok", model="opus", family="anthropic")
        self.assertFalse(r.review("H1", "diff", author_family="anthropic")["decorrelated"])

    def test_reviewer_outage_is_non_blocking(self):
        def boom(p):
            raise RuntimeError("model down")
        ev = CrossModelReviewer(boom, "x", "y").review("H1", "diff")
        self.assertEqual(ev["type"], "advisory_review_unavailable")   # never raises into the gate


class ApprovalTests(unittest.TestCase):
    def test_head_bound(self):
        evs = [ev_human_approval("H1", "alice", "human")]
        self.assertTrue(approval_satisfied(evs, "H1"))
        self.assertFalse(approval_satisfied(evs, "H2"))      # voided on head change

    def test_agent_cannot_self_approve(self):
        evs = [ev_human_approval("H1", "agent-7", "agent")]
        self.assertFalse(approval_satisfied(evs, "H1"))      # agent type never satisfies
        ra = [ev_human_approval("H1", "review-bot", "reviewer-app")]
        self.assertTrue(approval_satisfied(ra, "H1"))        # designated reviewer-App does

    def test_deadlock_guard(self):
        self.assertFalse(deadlock_guard({"agent"})[0])       # all-agent CODEOWNERS would deadlock
        self.assertTrue(deadlock_guard({"agent", "human"})[0])
        self.assertTrue(deadlock_guard({"reviewer-app"})[0])


class OverlayTests(unittest.TestCase):
    def _decide(self, overlay):
        return decide(classify({"suite": [True, True], **overlay}), FlakyState()).kind

    def test_pass_is_green(self):
        self.assertEqual(self._decide(approval_aware_overlay("PASS", "H1", [])), "green")

    def test_fail_is_red(self):
        self.assertEqual(self._decide(approval_aware_overlay("FAIL", "H1", [])), "red")

    def test_needs_human_holds_until_approved(self):
        self.assertEqual(self._decide(approval_aware_overlay("NEEDS_HUMAN", "H1", [])), "hold")
        approved = [ev_human_approval("H1", "alice", "human")]
        self.assertEqual(self._decide(approval_aware_overlay("NEEDS_HUMAN", "H1", approved)), "green")
        stale = [ev_human_approval("H2", "alice", "human")]   # approval for a different head
        self.assertEqual(self._decide(approval_aware_overlay("NEEDS_HUMAN", "H1", stale)), "hold")
        agent = [ev_human_approval("H1", "agent-7", "agent")]
        self.assertEqual(self._decide(approval_aware_overlay("NEEDS_HUMAN", "H1", agent)), "hold")


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(), "git >= 2.38")
class EngineApprovalTests(unittest.TestCase):
    def test_needs_human_batch_holds_then_lands_on_approval(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"x.txt": "0\n"}, parent=None)
        w.ff_push("refs/heads/main", t0)
        head = w.write_commit({"x.txt": "0\n", "auth.py": "def login(): ...\n"}, parent=t0)
        w.ff_push("refs/heads/pr1", head)
        lane = StateLog(bare, "refs/conductor/lane/x")
        approvals = StateLog(bare, "refs/conductor/approvals")
        eng = BatchEngine(w, lane, StateLog(bare, "refs/conductor/flaky/x"), lane_id="x")
        eng.enqueue(1, "refs/heads/pr1", head, t0, ["auth"], {"agent": "a", "closes": 12})

        # a risk-tier (auth) change => verifier NEEDS_HUMAN; CI green, but the gate holds.
        def verdict(spec_tree, members, reps=3, pr_absent=False):
            bv = BatchVerdict({"suite": [True] * reps})
            bv.per_test.update(approval_aware_overlay("NEEDS_HUMAN", head, approvals.read()[0]))
            return bv

        self.assertEqual(eng.tick(verdict).action, "held")
        self.assertEqual(reduce(lane.read()[0]).landed, [])          # not landed without approval

        approvals.append(ev_human_approval(head, "alice", "human"))  # head-bound human approval
        lane.append(ev_unhold(1))                                     # triage re-admits the held PR
        self.assertEqual(eng.tick(verdict).action, "batch_landed")
        self.assertEqual([x["pr"] for x in reduce(lane.read()[0]).landed], [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
