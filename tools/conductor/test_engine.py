#!/usr/bin/env python3
"""P1 tests: the reducer (pure), scope mapping (pure), and the engine end to end
against REAL git — a green PR lands linearly with provenance, a conflict is ejected,
the speculation-validity guard retries on a moved trunk, and a landed PR never
double-lands."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from reducer import reduce, ev_enqueue, ev_landed, ev_ejected  # noqa: E402
from scope import ScopeMap, ROOT_SCOPE  # noqa: E402
from statelog import StateLog  # noqa: E402
from engine import Engine  # noqa: E402

LANE = "refs/conductor/lane/x"


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


def _git(dir, *args):
    return subprocess.run(["git", "-C", dir, *args], capture_output=True, text=True, check=True).stdout


class ReducerTests(unittest.TestCase):
    def _enq(self, pr):
        return ev_enqueue(pr, f"refs/heads/pr{pr}", f"head{pr}", "base", ["s"], pr)

    def test_pending_order_and_landed(self):
        state = reduce([self._enq(1), self._enq(2), self._enq(3), ev_landed(2, "m2", "b", "a")])
        self.assertEqual([p.pr for p in state.pending], [1, 3])
        self.assertEqual([x["pr"] for x in state.landed], [2])
        self.assertEqual(state.head().pr, 1)

    def test_ejected_removes_from_pending(self):
        state = reduce([self._enq(1), self._enq(2), ev_ejected(1, "conflict")])
        self.assertEqual([p.pr for p in state.pending], [2])
        self.assertEqual(state.ejected[0]["reason"], "conflict")

    def test_replay_is_idempotent(self):
        evs = [self._enq(1), self._enq(2), ev_landed(1, "m", "b", "a")]
        a, b = reduce(evs), reduce(evs)
        self.assertEqual([p.pr for p in a.pending], [p.pr for p in b.pending])
        self.assertEqual([p.pr for p in a.pending], [2])


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.m = ScopeMap.from_dict({"src/auth/*": "auth", "src/api/*": "api", "*.md": "docs"})

    def test_single_scope(self):
        self.assertEqual(self.m.scopes_for(["src/auth/login.py"]), ["auth"])

    def test_multi_scope_sorted(self):
        self.assertEqual(self.m.scopes_for(["src/api/x.py", "README.md"]), ["api", "docs"])

    def test_unmatched_is_root(self):
        self.assertEqual(self.m.scopes_for(["misc/x.txt"]), [ROOT_SCOPE])

    def test_overlap(self):
        self.assertTrue(self.m.overlap(["src/auth/a.py"], ["src/auth/b.py"]))
        self.assertFalse(self.m.overlap(["src/auth/a.py"], ["src/api/b.py"]))


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(),
                     "git >= 2.38 with merge-tree --write-tree required")
class EngineTests(unittest.TestCase):
    def _seed(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"x.txt": "line0\n"}, parent=None)
        self.assertTrue(w.ff_push("refs/heads/main", t0)[0])
        return bare, w, t0

    def _parents(self, dir, commit):
        return _git(dir, "rev-list", "--parents", "-n1", commit).split()[1:]

    def test_clean_pr_lands_linearly_with_provenance(self):
        bare, w, t0 = self._seed()
        head1 = w.write_commit({"x.txt": "line0\n", "f1.txt": "a\n"}, parent=t0)
        w.ff_push("refs/heads/pr1", head1)
        eng = Engine(w, StateLog(bare, LANE))
        eng.enqueue(1, "refs/heads/pr1", head1, t0, ["root"],
                    {"closes": 1, "change_id": "Iabc", "agent": "agent-7",
                     "model": "opus-4-8", "subject": "add f1"})

        res = eng.tick(lambda pr, tree: True)
        self.assertEqual(res.action, "landed")
        trunk = eng.adv.current()
        self.assertEqual(trunk, res.detail)
        # linear: exactly one parent, == t0
        self.assertEqual(self._parents(w.dir, trunk), [t0])
        # the PR's content landed
        self.assertEqual(w.read_blob(trunk, "f1.txt"), "a\n")
        # provenance preserved
        body = _git(w.dir, "log", "-1", "--format=%B", trunk)
        self.assertIn("Closes #1", body)
        self.assertIn("Agent-Id: agent-7", body)
        self.assertEqual(_git(w.dir, "log", "-1", "--format=%an", trunk).strip(), "agent-7")
        # lane state: landed, queue drained
        state = reduce(eng.lane.read()[0])
        self.assertEqual([x["pr"] for x in state.landed], [1])
        self.assertEqual(state.pending, [])
        # idempotent: a landed PR does not land again
        self.assertEqual(eng.tick(lambda *a: True).action, "idle")
        w.close()

    def test_conflicting_pr_is_ejected(self):
        bare, w, t0 = self._seed()
        head1 = w.write_commit({"x.txt": "line1\n"}, parent=t0)   # both edit x.txt
        head2 = w.write_commit({"x.txt": "line2\n"}, parent=t0)
        for r, h in (("pr1", head1), ("pr2", head2)):
            w.ff_push(f"refs/heads/{r}", h)
        eng = Engine(w, StateLog(bare, LANE))
        eng.enqueue(1, "refs/heads/pr1", head1, t0, ["root"], {"agent": "a1"})
        eng.enqueue(2, "refs/heads/pr2", head2, t0, ["root"], {"agent": "a2"})

        self.assertEqual(eng.tick(lambda *a: True).action, "landed")     # pr1 lands
        ejected = eng.tick(lambda *a: True)                              # pr2 conflicts
        self.assertEqual(ejected.action, "ejected")
        self.assertEqual(ejected.pr, 2)
        state = reduce(eng.lane.read()[0])
        self.assertEqual([x["pr"] for x in state.landed], [1])
        self.assertEqual([x["pr"] for x in state.ejected], [2])
        self.assertEqual(state.pending, [])
        w.close()

    def test_validity_guard_retries_on_moved_trunk(self):
        bare, w, t0 = self._seed()
        head1 = w.write_commit({"x.txt": "line0\n", "f1.txt": "a\n"}, parent=t0)
        w.ff_push("refs/heads/pr1", head1)
        eng = Engine(w, StateLog(bare, LANE))
        eng.enqueue(1, "refs/heads/pr1", head1, t0, ["root"], {"agent": "a1"})

        side = {}

        def inject():
            # A concurrent lane lands a non-conflicting change (adds y.txt) onto trunk,
            # AFTER we built our land commit but BEFORE we advance.
            s = w.write_commit({"x.txt": "line0\n", "y.txt": "z\n"}, parent=t0)
            self.assertTrue(w.lease_push("refs/heads/main", s, t0)[0])
            side["oid"] = s

        res = eng.tick(lambda *a: True, _after_build=inject)
        self.assertEqual(res.action, "retry")                       # lease failed -> not landed
        self.assertEqual(eng.adv.current(), side["oid"])            # trunk is the other land
        self.assertEqual(reduce(eng.lane.read()[0]).landed, [])     # nothing recorded

        res2 = eng.tick(lambda *a: True)                            # rebuild on the new trunk
        self.assertEqual(res2.action, "landed")
        trunk = eng.adv.current()
        self.assertEqual(self._parents(w.dir, trunk), [side["oid"]])   # linear on the new tip
        self.assertEqual(w.read_blob(trunk, "f1.txt"), "a\n")          # our change
        self.assertEqual(w.read_blob(trunk, "y.txt"), "z\n")          # the other change too
        self.assertEqual([x["pr"] for x in reduce(eng.lane.read()[0]).landed], [1])
        w.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
