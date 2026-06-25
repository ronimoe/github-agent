#!/usr/bin/env python3
"""P3 tests: lane routing + global lane, the write governor, exactly-once land recovery,
cross-lane trunk serialization, and the hermetic land-gate. Against real git where land
mechanics are involved; pure-logic where not."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from reducer import reduce, PRRecord, ev_intent_prepared  # noqa: E402
from land import land_message  # noqa: E402
from verdict import from_green  # noqa: E402
from batch_engine import BatchEngine  # noqa: E402
from dispatcher import Dispatcher, GLOBAL_LANE, touches_global  # noqa: E402
from governor import WriteGovernor  # noqa: E402
from reconciler import Reconciler  # noqa: E402
from lane_engine import LaneEngine  # noqa: E402
from hermetic_land import Regenerator  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


def _git(d, *a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True).stdout


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.d = Dispatcher(StateLog(self.bare, "refs/conductor/registry"))

    def test_global_path_detection(self):
        self.assertTrue(touches_global(["package-lock.json"]))
        self.assertTrue(touches_global(["src/x.py", "go.sum"]))
        self.assertTrue(touches_global([".github/workflows/ci.yml"]))
        self.assertFalse(touches_global(["src/auth/login.py"]))

    def test_scoped_lanes_disjoint_then_merge(self):
        self.assertEqual(self.d.route(1, ["auth"], ["src/auth/a.py"]), "L:auth")
        self.assertEqual(self.d.route(2, ["api"], ["src/api/b.py"]), "L:api")     # parallel lane
        self.assertEqual(self.d.route(3, ["x"], ["package-lock.json"]), GLOBAL_LANE)
        self.assertEqual(self.d.route(4, ["auth"], ["package.json"]), GLOBAL_LANE)  # global path wins
        # a bridging PR merges the auth+api components; both re-resolve to the survivor.
        self.d.route(5, ["auth", "api"], ["src/auth/c.py", "src/api/c.py"])
        self.assertEqual(self.d.current_lane(1), "L:api")
        self.assertEqual(self.d.current_lane(2), "L:api")


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.clock = [1000]
        self.log = StateLog(self.bare, "refs/conductor/governor")

    def _gov(self, budget):
        return WriteGovernor(self.log, budget, ttl_ms=500, clock=lambda: self.clock[0])

    def test_budget_never_exceeded(self):
        g = self._gov(2)
        t1, t2 = g.acquire("laneA"), g.acquire("laneB")
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        self.assertIsNone(g.acquire("laneC"))       # budget exhausted
        self.assertEqual(g.active_count(), 2)
        g.release(t1)
        self.assertIsNotNone(g.acquire("laneC"))     # slot freed

    def test_ttl_reaps_dead_holder(self):
        g = self._gov(1)
        dead = g.acquire("crashed")
        self.assertIsNone(g.acquire("other"))        # budget full
        self.clock[0] += 600                          # past ttl
        live = g.acquire("other")                     # reaps the dead holder, then acquires
        self.assertIsNotNone(live)
        self.assertTrue(g.is_reaped(dead))


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(),
                     "git >= 2.38 required")
class IntentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.w = WorkRepo(self.bare)
        self.t0 = self.w.write_commit({"x.txt": "0\n"}, parent=None)
        self.w.ff_push("refs/heads/main", self.t0)
        self.lane = StateLog(self.bare, "refs/conductor/lane/x")
        self.flk = StateLog(self.bare, "refs/conductor/flaky/x")
        self.eng = BatchEngine(self.w, self.lane, self.flk, lane_id="x")
        self.rec = Reconciler(self.w)

    def _prepare_and_maybe_advance(self, pr, advance: bool):
        head = self.w.write_commit({"x.txt": "0\n", f"f{pr}.txt": "c\n"}, parent=self.t0)
        self.w.ff_push(f"refs/heads/pr{pr}", head)
        self.eng.enqueue(pr, f"refs/heads/pr{pr}", head, self.t0, ["root"], {"agent": "a"})
        rec = PRRecord(pr, f"refs/heads/pr{pr}", head, self.t0, (), 1, {"agent": "a"})
        bid = "x-b1"
        _, tree = self.w.merge_tree(self.t0, head)
        land_commit = self.w.commit_onto(tree, self.t0, land_message(rec, "x", bid),
                                         author={"name": "a"})
        spec_tree = self.w.tree_of(land_commit)
        self.lane.append(ev_intent_prepared("x", bid, [pr], self.t0, land_commit, spec_tree))
        if advance:
            self.eng.adv.advance(land_commit, expected_old=self.t0)   # crash before ev_batch_landed
        return land_commit

    def test_exactly_once_replay_after_advance_crash(self):
        self._prepare_and_maybe_advance(1, advance=True)
        self.assertEqual(reduce(self.lane.read()[0]).landed, [])        # crash window
        out = self.rec.complete_intents(self.lane, self.eng.adv)
        self.assertEqual(out, {"x-b1": "landed"})
        self.assertEqual([x["pr"] for x in reduce(self.lane.read()[0]).landed], [1])
        self.assertEqual(self.rec.complete_intents(self.lane, self.eng.adv), {})  # idempotent no-op

    def test_abandoned_intent_when_advance_never_happened(self):
        self._prepare_and_maybe_advance(1, advance=False)
        out = self.rec.complete_intents(self.lane, self.eng.adv)
        self.assertEqual(out, {"x-b1": "abandoned"})
        self.assertEqual(reduce(self.lane.read()[0]).landed, [])         # nothing landed
        self.assertEqual([r.pr for r in reduce(self.lane.read()[0]).pending], [1])  # still pending


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(),
                     "git >= 2.38 required")
class CrossLaneTests(unittest.TestCase):
    def test_two_lanes_serialize_at_trunk(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"x.txt": "0\n"}, parent=None)
        w.ff_push("refs/heads/main", t0)
        ha = w.write_commit({"x.txt": "0\n", "a.txt": "a\n"}, parent=t0)
        hb = w.write_commit({"x.txt": "0\n", "b.txt": "b\n"}, parent=t0)
        w.ff_push("refs/heads/pra", ha)
        w.ff_push("refs/heads/prb", hb)
        engA = BatchEngine(w, StateLog(bare, "refs/conductor/lane/a"),
                           StateLog(bare, "refs/conductor/flaky/a"), lane_id="a")
        engB = BatchEngine(w, StateLog(bare, "refs/conductor/lane/b"),
                           StateLog(bare, "refs/conductor/flaky/b"), lane_id="b")
        engA.enqueue(1, "refs/heads/pra", ha, t0, ["a"], {"agent": "a"})
        engB.enqueue(2, "refs/heads/prb", hb, t0, ["b"], {"agent": "b"})

        # B lands during A's build/advance window -> A's lease fails, A re-speculates onto B's trunk.
        resA = engA.tick(from_green(lambda *a: True), _after_build=lambda: engB.tick(from_green(lambda *a: True)))
        self.assertEqual(resA.action, "retry")
        resA2 = engA.tick(from_green(lambda *a: True))
        self.assertEqual(resA2.action, "batch_landed")

        trunk = engA.adv.current()
        self.assertEqual(w.read_blob(trunk, "a.txt"), "a\n")        # both changes present
        self.assertEqual(w.read_blob(trunk, "b.txt"), "b\n")
        self.assertEqual(len(_git(w.dir, "rev-list", "--parents", "-n1", trunk).split()) - 1, 1)  # linear
        self.assertEqual([x["pr"] for x in reduce(engA.lane.read()[0]).landed], [1])
        self.assertEqual([x["pr"] for x in reduce(engB.lane.read()[0]).landed], [2])


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(),
                     "git >= 2.38 required")
class HermeticLandTests(unittest.TestCase):
    def test_hermetic_gate_blocks_then_allows_land(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"x.txt": "0\n"}, parent=None)
        w.ff_push("refs/heads/main", t0)
        head = w.write_commit({"x.txt": "0\n", "f.txt": "c\n"}, parent=t0)
        w.ff_push("refs/heads/pr1", head)

        class FakeRegen(Regenerator):
            def __init__(self):
                self.ok = False
            def regen(self, tree):
                return tree, "digest-" + tree[:8]
            def gate_ok(self, tree):
                return self.ok

        reg = FakeRegen()
        le = LaneEngine(w, StateLog(bare, "refs/conductor/lane/g"),
                        StateLog(bare, "refs/conductor/flaky/g"), lane_id="g",
                        is_global=True, regenerator=reg)
        le.enqueue(1, "refs/heads/pr1", head, t0, ["root"], {"agent": "a"})

        blocked = le.tick(from_green(lambda *a: True))             # gate not green -> blocked
        self.assertEqual(blocked.action, "held")
        self.assertEqual(reduce(le.lane.read()[0]).landed, [])
        self.assertEqual(le.adv.current(), t0)                     # trunk unchanged

        reg.ok = True                                              # gate now green
        landed = le.tick(from_green(lambda *a: True))
        self.assertEqual(landed.action, "batch_landed")
        self.assertEqual([x["pr"] for x in reduce(le.lane.read()[0]).landed], [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
