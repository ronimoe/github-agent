#!/usr/bin/env python3
"""Integration tests for Conductor's P0 serialization primitive, run against REAL git.

A local bare repo is a faithful substrate: git's fast-forward and `--force-with-lease`
ref-update semantics are identical to GitHub's. These tests prove the properties
ADR-0003 depends on:

  * sequential appends form a single linear chain;
  * a stale-tip update is REJECTED, not clobbered (lost-update-free);
  * the append retry loop converges under a mid-flight concurrent advance, losing no
    event;
  * trunk advance succeeds under a held lease and is rejected when trunk has moved.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from trunk import TrunkAdvancer  # noqa: E402

REF = "refs/conductor/lane/x"


def _bare() -> str:
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


@unittest.skipUnless(gitutil.git_available(), "git not available")
class StateLogTests(unittest.TestCase):
    def test_write_commit_is_deterministic(self):
        with WorkRepo(_bare()) as w:
            a = w.write_commit({"f": "x"}, parent=None)
            b = w.write_commit({"f": "x"}, parent=None)
            self.assertEqual(a, b)  # fixed identity+date => stable OID

    def test_sequential_appends_form_linear_log(self):
        log = StateLog(_bare(), REF)
        try:
            for i in (1, 2, 3):
                res = log.append({"e": i})
                self.assertTrue(res.ok)
                self.assertEqual(res.attempts, 1)  # no contention
            events, tip = log.read()
            self.assertEqual(events, [{"e": 1}, {"e": 2}, {"e": 3}])
            self.assertIsNotNone(tip)
        finally:
            log.close()

    def test_stale_update_is_rejected_not_clobbered(self):
        bare = _bare()
        seed = StateLog(bare, REF)
        seed.append({"w": "a"})              # ref = [a] @ ca

        # Two writers both read the same tip `ca` (read() fetches it locally) and build
        # competing children from it.
        lb, lc = StateLog(bare, REF), StateLog(bare, REF)
        eb, tb = lb.read()
        ec, tc = lc.read()
        self.assertEqual(tb, tc)             # both saw the same tip
        cb = lb.repo.write_commit(
            {"log.jsonl": "".join(json.dumps(e) + "\n" for e in eb + [{"w": "b"}])}, parent=tb)
        cc = lc.repo.write_commit(
            {"log.jsonl": "".join(json.dumps(e) + "\n" for e in ec + [{"w": "c"}])}, parent=tc)

        ok_b, _ = lb.repo.lease_push(REF, cb, expected=tb)
        ok_c, _ = lc.repo.lease_push(REF, cc, expected=tc)   # ref is cb now => lease fails

        self.assertTrue(ok_b)
        self.assertFalse(ok_c, "stale update must be rejected")
        self.assertEqual(seed.repo.ls_remote(REF), cb, "winner not clobbered")
        for r in (seed, lb, lc):
            r.close()

    def test_append_retry_loop_loses_no_event(self):
        bare = _bare()
        seed = StateLog(bare, REF)
        seed.append({"w": "a"})              # ref = [a]

        injector = StateLog(bare, REF)       # a concurrent writer

        class RacyLog(StateLog):
            """Advances the ref once, AFTER our read and BEFORE our push, to force the
            first append attempt to lose the race."""
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._fired = False

            def read(self):
                events, tip = super().read()
                if not self._fired:
                    self._fired = True
                    injector.append({"w": "x"})   # ref: [a] -> [a, x]
                return events, tip

        racy = RacyLog(bare, REF)
        res = racy.append({"w": "b"})        # attempt1 parented on [a] -> rejected; attempt2 on [a,x] -> ok
        self.assertTrue(res.ok)
        self.assertGreaterEqual(res.attempts, 2, "should have retried after the injected advance")

        events, _ = seed.read()
        self.assertEqual(events, [{"w": "a"}, {"w": "x"}, {"w": "b"}], "no event lost or duplicated")
        for r in (seed, injector, racy):
            r.close()


@unittest.skipUnless(gitutil.git_available(), "git not available")
class TrunkTests(unittest.TestCase):
    def test_advance_holds_lease_and_rejects_concurrent(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"f": "0"}, parent=None)
        self.assertTrue(w.ff_push("refs/heads/main", t0)[0])

        adv = TrunkAdvancer(bare, "main", repo=w)   # engine builds the spec AND advances
        a1 = w.write_commit({"f": "1"}, parent=t0)
        a2 = w.write_commit({"f": "2"}, parent=t0)   # sibling of a1, both descend from t0

        ok, _ = adv.advance(a1, expected_old=t0)
        self.assertTrue(ok)
        self.assertEqual(adv.current(), a1)

        # a2 was built on the now-stale t0: advancing with the old lease must fail.
        ok, reason = adv.advance(a2, expected_old=t0)
        self.assertFalse(ok, reason)
        self.assertEqual(adv.current(), a1, "trunk not clobbered by the lost race")

        # Rebuild a2 on the new trunk a1 and retry — keeps trunk linear.
        a2b = w.write_commit({"f": "2"}, parent=a1)
        ok, _ = adv.advance(a2b, expected_old=a1)
        self.assertTrue(ok)
        self.assertEqual(adv.current(), a2b)

        # Linear: a2b -> a1 -> t0 (a2b's first-parent chain reaches t0).
        log = subprocess.run(
            ["git", "-C", w.dir, "rev-list", "--first-parent", a2b],
            capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(log, [a2b, a1, t0])
        w.close()
        adv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
