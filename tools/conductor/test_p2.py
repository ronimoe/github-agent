#!/usr/bin/env python3
"""P2 tests against REAL git. The CI is simulated by `FakeCI`, which returns per-test
outcomes across reps so the soundness logic (fail-closed on intermittent, independent
PR-absent flake signal, deterministic-red ejection, interaction-not-misattributed) is
exercised exactly. Land mechanics (merge, linear advance, lease) are real git."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from reducer import reduce, ev_window_tuned, ev_unhold  # noqa: E402
from flaky import reduce_flaky, ev_test_outcome  # noqa: E402
from verdict import BatchVerdict, from_green  # noqa: E402
from batch_engine import BatchEngine  # noqa: E402

LANE, FLK = "refs/conductor/lane/x", "refs/conductor/flaky/0"


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


def _git(d, *a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True).stdout


class FakeCI:
    """Simulated hermetic CI. `members` are PR numbers in the spec.
       poison: PRs that make t_poison DET_FAIL.    racy: PRs that make t_race UNRESOLVED.
       interaction=(a,b): t_iface DET_FAIL iff both present.
       flaky_tests: tests that flip regardless (used with pre-seeded PR-absent evidence).
       barrier_fail: t_regression DET_FAIL on the PR-absent (barrier) run."""
    def __init__(self, reps=3, poison=(), racy=(), interaction=None, flaky_tests=(),
                 barrier_fail=False):
        self.reps = reps
        self.poison, self.racy = set(poison), set(racy)
        self.interaction = tuple(interaction) if interaction else None
        self.flaky_tests, self.barrier_fail = set(flaky_tests), barrier_fail

    def __call__(self, spec_tree, members, reps=None, pr_absent=False):
        reps = reps or self.reps
        ms = set(members)
        per = {"suite": [True] * reps}
        if pr_absent and self.barrier_fail:
            per["t_regression"] = [False] * reps
        if self.poison & ms:
            per["t_poison"] = [False] * reps
        if self.racy & ms:
            per["t_race"] = [i % 2 == 0 for i in range(reps)]
        if self.interaction and set(self.interaction) <= ms:
            per["t_iface"] = [False] * reps
        for t in self.flaky_tests:
            per[t] = [i % 2 == 0 for i in range(reps)]
        return BatchVerdict(per)


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(),
                     "git >= 2.38 required")
class P2Tests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.w = WorkRepo(self.bare)
        self.t0 = self.w.write_commit({"x.txt": "line0\n"}, parent=None)
        self.assertTrue(self.w.ff_push("refs/heads/main", self.t0)[0])
        self.lane = StateLog(self.bare, LANE)
        self.flk = StateLog(self.bare, FLK)

    def _engine(self, **kw):
        return BatchEngine(self.w, self.lane, self.flk, reps=3, **kw)

    def _pr(self, n, mod_x=None):
        files = {"x.txt": mod_x or "line0\n", f"f{n}.txt": "c\n"}
        head = self.w.write_commit(files, parent=self.t0)
        self.w.ff_push(f"refs/heads/pr{n}", head)
        return head

    def _seed_flake(self, test_id, passes, fails):
        for i in range(passes):
            self.flk.append(ev_test_outcome(test_id, "trunk", "pass", True, i))
        for i in range(fails):
            self.flk.append(ev_test_outcome(test_id, "trunk", "fail", True, i))

    def _lane(self):
        return reduce(self.lane.read()[0])

    def _parents(self, c):
        return _git(self.w.dir, "rev-list", "--parents", "-n1", c).split()[1:]

    # --- m=1 equivalence + batching -----------------------------------------

    def test_p1_equivalence_via_adapter(self):
        h = self._pr(1)
        eng = self._engine()
        eng.enqueue(1, "refs/heads/pr1", h, self.t0, ["root"],
                    {"closes": 1, "agent": "agent-7", "subject": "add f1"})
        res = eng.tick(from_green(lambda pr, tree: True))
        self.assertEqual(res.action, "batch_landed")
        trunk = eng.adv.current()
        self.assertEqual(self._parents(trunk), [self.t0])           # linear
        body = _git(self.w.dir, "log", "-1", "--format=%B", trunk)
        self.assertIn("Closes #1", body)
        self.assertEqual([x["pr"] for x in self._lane().landed], [1])

    def test_atomic_batch_land(self):
        for n in (1, 2, 3):
            self._pr(n)
        self.lane.append(ev_window_tuned(3))                        # allow m=3
        eng = self._engine()
        for n in (1, 2, 3):
            eng.enqueue(n, f"refs/heads/pr{n}", self.w.ls_remote(f"refs/heads/pr{n}"),
                        self.t0, ["root"], {"agent": f"a{n}"})
        res = eng.tick(FakeCI())
        self.assertEqual(res.action, "batch_landed")
        trunk = eng.adv.current()
        for n in (1, 2, 3):
            self.assertEqual(self.w.read_blob(trunk, f"f{n}.txt"), "c\n")   # all three landed
        self.assertEqual(len(self._parents(trunk)), 1)              # linear chain
        self.assertEqual(sorted(x["pr"] for x in self._lane().landed), [1, 2, 3])
        self.assertEqual(self._lane().pending, [])

    def test_validity_guard_on_batch(self):
        for n in (1, 2):
            self._pr(n)
        self.lane.append(ev_window_tuned(2))
        eng = self._engine()
        for n in (1, 2):
            eng.enqueue(n, f"refs/heads/pr{n}", self.w.ls_remote(f"refs/heads/pr{n}"),
                        self.t0, ["root"], {"agent": f"a{n}"})

        def inject():
            s = self.w.write_commit({"x.txt": "line0\n", "y.txt": "z\n"}, parent=self.t0)
            self.assertTrue(self.w.lease_push("refs/heads/main", s, self.t0)[0])

        res = eng.tick(FakeCI(), _after_build=inject)
        self.assertEqual(res.action, "retry")
        self.assertEqual(self._lane().landed, [])                   # atomic: nothing landed

    # --- soundness ----------------------------------------------------------

    def test_deterministic_red_ejects(self):
        self._pr(1); self._pr(2)
        self.lane.append(ev_window_tuned(2))
        eng = self._engine()
        for n in (1, 2):
            eng.enqueue(n, f"refs/heads/pr{n}", self.w.ls_remote(f"refs/heads/pr{n}"),
                        self.t0, ["root"], {"agent": f"a{n}"})
        res = eng.tick(FakeCI(poison={2}))                          # PR2 deterministically red
        self.assertEqual(res.action, "culprit_ejected")
        self.assertEqual(res.pr, 2)
        self.assertEqual([x["pr"] for x in self._lane().ejected], [2])
        # survivor lands next tick
        self.assertEqual(eng.tick(FakeCI(poison={2})).action, "batch_landed")
        self.assertEqual([x["pr"] for x in self._lane().landed], [1])

    def test_real_race_not_masked(self):
        # t_race is STABLE on PR-absent trees, but PR2 makes it flip => candidate-introduced.
        self._seed_flake("t_race", passes=4, fails=0)
        self._pr(1); self._pr(2)
        self.lane.append(ev_window_tuned(2))
        eng = self._engine()
        for n in (1, 2):
            eng.enqueue(n, f"refs/heads/pr{n}", self.w.ls_remote(f"refs/heads/pr{n}"),
                        self.t0, ["root"], {"agent": f"a{n}"})
        res = eng.tick(FakeCI(racy={2}))
        self.assertEqual(res.action, "held")                       # fail closed, NOT landed/excused
        self.assertEqual(res.pr, 2)
        self.assertEqual(self._lane().landed, [])                  # the racy PR never lands in this batch
        self.assertIn(2, [r.pr for r in self._lane().held])

    def test_flake_excused_only_from_independent_signal(self):
        self._seed_flake("t_flaky", passes=2, fails=3)             # established flaky (PR-absent)
        self._pr(1)
        eng = self._engine()
        eng.enqueue(1, "refs/heads/pr1", self.w.ls_remote("refs/heads/pr1"),
                    self.t0, ["root"], {"agent": "a1"})
        res = eng.tick(FakeCI(flaky_tests={"t_flaky"}))
        self.assertEqual(res.action, "batch_landed")               # excused by the independent signal

    def test_thin_evidence_holds_not_lands(self):
        self._pr(1)                                                # t_thin has NO PR-absent evidence
        eng = self._engine()
        eng.enqueue(1, "refs/heads/pr1", self.w.ls_remote("refs/heads/pr1"),
                    self.t0, ["root"], {"agent": "a1"})
        res = eng.tick(FakeCI(flaky_tests={"t_thin"}))
        self.assertEqual(res.action, "held")
        self.assertEqual(self._lane().landed, [])

    def test_interaction_not_misattributed(self):
        for n in (1, 2, 3):
            self._pr(n)
        self.lane.append(ev_window_tuned(3))
        eng = self._engine()
        for n in (1, 2, 3):
            eng.enqueue(n, f"refs/heads/pr{n}", self.w.ls_remote(f"refs/heads/pr{n}"),
                        self.t0, ["root"], {"agent": f"a{n}"})
        res = eng.tick(FakeCI(interaction=(1, 2)))                 # green alone, red together
        self.assertEqual(res.action, "interaction_split")
        self.assertEqual(res.pr, 2)                               # later seq ejected, #1 kept
        ej = self._lane().ejected
        self.assertEqual([x["pr"] for x in ej], [2])
        self.assertIn("incompatible-with-#1", ej[0]["reason"])    # NOT tagged a real regression

    def test_no_permanent_loss_requeue(self):
        self._pr(1)
        eng = self._engine()
        eng.enqueue(1, "refs/heads/pr1", self.w.ls_remote("refs/heads/pr1"),
                    self.t0, ["root"], {"agent": "a1"})
        self.assertEqual(eng.tick(FakeCI(flaky_tests={"t_late"})).action, "held")   # thin -> held
        # the test later becomes established flaky from PR-absent evidence, and triage re-admits it
        self._seed_flake("t_late", passes=2, fails=3)
        self.lane.append(ev_unhold(1))
        self.assertEqual(eng.tick(FakeCI(flaky_tests={"t_late"})).action, "batch_landed")

    # --- liveness -----------------------------------------------------------

    def test_aging_forces_solo(self):
        from reducer import ev_aged
        self._pr(1); self._pr(2)
        self.lane.append(ev_window_tuned(5))
        eng = self._engine()
        eng.enqueue(1, "refs/heads/pr1", self.w.ls_remote("refs/heads/pr1"), self.t0, ["root"], {"agent": "g"})
        eng.enqueue(2, "refs/heads/pr2", self.w.ls_remote("refs/heads/pr2"), self.t0, ["root"], {"agent": "h"})
        for _ in range(3):
            self.lane.append(ev_aged([1]))                        # PR1 aged out
        self.assertTrue(self._lane().pending[0].forced_solo)
        eng.tick(FakeCI())                                        # head forced solo -> batch is [1] alone
        self.assertEqual([x["pr"] for x in self._lane().landed], [1])   # only PR1 landed
        self.assertEqual([r.pr for r in self._lane().pending], [2])     # PR2 not co-batched

    def test_adaptive_window_aimd(self):
        self._pr(1)
        eng = self._engine()
        eng.enqueue(1, "refs/heads/pr1", self.w.ls_remote("refs/heads/pr1"), self.t0, ["root"], {"agent": "a"})
        eng.tick(FakeCI())                                        # green -> additive increase
        self.assertEqual(self._lane().window["m"], 2)
        self._pr(2)
        eng.enqueue(2, "refs/heads/pr2", self.w.ls_remote("refs/heads/pr2"), self.t0, ["root"], {"agent": "b"})
        eng.tick(FakeCI(flaky_tests={"t_thin"}))                  # hold -> multiplicative decrease
        self.assertEqual(self._lane().window["m"], 1)

    def test_barrier_catches_regression(self):
        for n in (1, 2, 3):
            self._pr(n)
        eng = self._engine(n_barrier=2, budget=1)   # budget=1 -> one land per tick, barrier fires on tick 3
        for n in (1, 2, 3):
            eng.enqueue(n, f"refs/heads/pr{n}", self.w.ls_remote(f"refs/heads/pr{n}"),
                        self.t0, ["root"], {"agent": f"a{n}"})
        self.assertEqual(eng.tick(FakeCI()).action, "batch_landed")   # pr1, at_risk=1
        self.assertEqual(eng.tick(FakeCI()).action, "batch_landed")   # pr2, at_risk=2
        res = eng.tick(FakeCI(barrier_fail=True))                     # barrier fires, full suite red
        self.assertEqual(res.action, "barrier_red")
        self.assertEqual(sorted(x["pr"] for x in self._lane().landed), [1, 2])   # pr3 did NOT land


if __name__ == "__main__":
    unittest.main(verbosity=2)
