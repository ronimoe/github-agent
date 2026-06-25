#!/usr/bin/env python3
"""P4 tests: the mechanical correctness verifier (V1..V5), the issue oracle, head-bound
approvals, the advisory non-blocking reviewer, and the mechanical->verdict integration."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
import apidiff  # noqa: E402
from changeset import parse_fragment, ChangesetError  # noqa: E402
from issues import DeterministicFakeOracle, recheck_at_land  # noqa: E402
from advisory import run_advisory, ev_human_approval, approval_satisfied  # noqa: E402
from verifier import verify, mechanical_overlay, PASS, FAIL, NEEDS_HUMAN  # noqa: E402
from verdict import classify, decide  # noqa: E402
from flaky import FlakyState  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


CS = "bumps:\n  web: {lvl}\nissues: [{iss}]\nagent: a1\n---\nchangelog body\n"


class ApiDiffTests(unittest.TestCase):
    def test_removed_export_is_major(self):
        old = "def foo():\n    pass\ndef bar():\n    pass\n"
        new = "def foo():\n    pass\n"            # bar removed
        lvl, unc = apidiff.infer_file(old, new, ".py")
        self.assertEqual(lvl, apidiff.MAJOR)
        self.assertFalse(unc)

    def test_added_export_is_minor(self):
        lvl, _ = apidiff.infer_file("def foo():\n pass\n", "def foo():\n pass\ndef baz():\n pass\n", ".py")
        self.assertEqual(lvl, apidiff.MINOR)

    def test_private_churn_no_false_major(self):
        old = "def _helper():\n    return 1\ndef foo():\n    return _helper()\n"
        new = "def _helper():\n    return 2\ndef foo():\n    return _helper()\n"
        lvl, _ = apidiff.infer_file(old, new, ".py")
        self.assertEqual(lvl, apidiff.PATCH)       # exported surface unchanged

    def test_signature_change_is_major(self):
        lvl, _ = apidiff.infer_file("def foo(a):\n pass\n", "def foo(a, b):\n pass\n", ".py")
        self.assertEqual(lvl, apidiff.MAJOR)

    def test_unsupported_language_uncertain(self):
        lvl, unc = apidiff.infer_file("x", "y", ".rb")
        self.assertEqual(lvl, apidiff.MAJOR)
        self.assertTrue(unc)

    def test_reexport_routes_uncertain(self):
        old = "export const a = 1\n"
        new = "export const a = 1\nexport * from './other'\n"
        lvl, unc = apidiff.infer_file(old, new, ".ts")
        self.assertTrue(unc)


class ChangesetTests(unittest.TestCase):
    def test_valid_fragment(self):
        f = parse_fragment(CS.format(lvl="minor", iss="12"))
        self.assertEqual(f.bumps, {"web": apidiff.MINOR})
        self.assertEqual(f.issues, [12])

    def test_malformed_rejected(self):
        for bad in ["no separator\nbumps:\n  web: minor",
                    "bump: massive\n---\nbody",
                    "bumps:\n  web: minor\nevil: {a: b}\n---\nbody",
                    "bumps:\n  web: minor\n---\n",
                    "bumps:\n\tweb: minor\n---\nbody"]:
            with self.assertRaises(ChangesetError):
                parse_fragment(bad)


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(), "git >= 2.38")
class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.w = WorkRepo(self.bare)

    def _tree(self, files):
        base = self.w.write_commit({"src/api/x.py": "def foo():\n pass\n"}, parent=None)
        head = self.w.write_commit(files, parent=base)
        clean, tree = self.w.merge_tree(base, head)
        return base, tree

    def test_removed_export_forces_major_fails_low_bump(self):
        base, tree = self._tree({"src/api/x.py": "def bar():\n pass\n"})   # foo removed
        oracle = DeterministicFakeOracle(open_issues={12}, links={1: {12}})
        v = verify(self.w, base, tree, tree, CS.format(lvl="minor", iss="12"),
                   {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertEqual(v.status, FAIL)
        self.assertTrue(v.checks["V1"].startswith("FAIL"))
        # mechanical FAIL -> DET_FAIL -> decide() red, even with a quarantined flaky test present
        merged = {"suite": [True, True], "_mechanical": [False]}
        self.assertEqual(decide(classify(merged), FlakyState()).kind, "red")

    def test_clean_patch_passes(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n", "src/api/y.py": "z = 1\n"})
        oracle = DeterministicFakeOracle(open_issues={12}, links={1: {12}})
        v = verify(self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                   {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertEqual(v.status, PASS, v.checks)

    def test_footprint_undeclared_path_fails(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n", "src/other.py": "q = 1\n"})
        oracle = DeterministicFakeOracle(open_issues={12}, links={1: {12}})
        v = verify(self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                   {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertTrue(v.checks["V4"].startswith("FAIL"))      # src/other.py undeclared on realized tree

    def test_issue_closed_fails(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n"})
        oracle = DeterministicFakeOracle(open_issues=set(), links={1: {12}})   # #12 closed
        v = verify(self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                   {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertTrue(v.checks["V3"].startswith("FAIL"))

    def test_issue_oracle_unknown_needs_human(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n"})
        oracle = DeterministicFakeOracle(unknown={12}, links={1: {12}})
        v = verify(self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                   {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertEqual(v.status, NEEDS_HUMAN)

    def test_unlinked_issue_rejected(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n"})
        oracle = DeterministicFakeOracle(open_issues={12}, links={1: set()})   # #12 NOT linked to PR 1
        v = verify(self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                   {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertTrue(v.checks["V3"].startswith("FAIL"))

    def test_verdict_record_idempotent(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n"})
        oracle = DeterministicFakeOracle(open_issues={12}, links={1: {12}})
        args = (self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                {"change_id": "I1", "agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)
        self.assertEqual(verify(*args).record_digest, verify(*args).record_digest)

    def test_missing_trailers_v5_fails(self):
        base, tree = self._tree({"src/api/x.py": "def foo():\n return 1\n"})
        oracle = DeterministicFakeOracle(open_issues={12}, links={1: {12}})
        v = verify(self.w, base, tree, tree, CS.format(lvl="patch", iss="12"),
                   {"agent": "a1", "closes": 12}, ["src/api/*"], oracle, 1)   # no change_id
        self.assertTrue(v.checks["V5"].startswith("FAIL"))


class IssueLandTests(unittest.TestCase):
    def test_recheck_at_land_aborts_on_close(self):
        precheck = DeterministicFakeOracle(open_issues={12}, links={1: {12}})
        self.assertEqual(recheck_at_land(precheck, 1, [12]), "ok")           # precheck open
        atland = DeterministicFakeOracle(open_issues=set(), links={1: {12}})  # closed by merge time
        self.assertEqual(recheck_at_land(atland, 1, [12]), "closed")          # precheck PASS can't substitute

    def test_recheck_unknown_needs_human(self):
        self.assertEqual(recheck_at_land(DeterministicFakeOracle(unknown={12}, links={1: {12}}), 1, [12]),
                         "unknown")


class ApprovalAdvisoryTests(unittest.TestCase):
    def test_human_approval_bound_to_head(self):
        events = [ev_human_approval("H1", "alice")]
        self.assertTrue(approval_satisfied(events, "H1"))
        self.assertFalse(approval_satisfied(events, "H2"))   # voided by force-push/rebase

    def test_advisory_outage_does_not_raise(self):
        def boom(diff):
            raise RuntimeError("model down")
        out = run_advisory(boom, "H1", "diff")
        self.assertEqual(out["type"], "advisory_review_unavailable")   # non-blocking


if __name__ == "__main__":
    unittest.main(verbosity=2)
