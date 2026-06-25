#!/usr/bin/env python3
"""Tests for the real GitHub-backed ports (#4 issue oracle + at-land guard, #5 release ports).
Command runners are injected, so the GraphQL parsing, idempotent error-mapping, and the at-land
recheck are verified offline; the live network calls need a real repo (see INTEGRATION.md)."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from reducer import reduce  # noqa: E402
from verdict import from_green  # noqa: E402
from batch_engine import BatchEngine  # noqa: E402
from issues import GitHubIssueOracle, recheck_at_land  # noqa: E402
import gh_ports  # noqa: E402
from release_txn import run_release, RegistrySim, IssueSim, reduce_release  # noqa: E402
from versionplan import ReleasePlan  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


# --- #4: GraphQL issue oracle -------------------------------------------------

class OracleTests(unittest.TestCase):
    def _oracle(self, responses):
        calls = {"q": []}

        def run(query, variables):
            calls["q"].append((query, variables))
            r = responses(query, variables)
            if isinstance(r, Exception):
                raise r
            return r
        return GitHubIssueOracle("owner/repo", run=run), calls

    def test_is_open_states(self):
        ok, _ = self._oracle(lambda q, v: {"data": {"repository": {"issue": {"state": "OPEN"}}}})
        self.assertTrue(ok.is_open(12))
        closed, _ = self._oracle(lambda q, v: {"data": {"repository": {"issue": {"state": "CLOSED"}}}})
        self.assertFalse(closed.is_open(12))
        missing, _ = self._oracle(lambda q, v: {"data": {"repository": {"issue": None}}})
        self.assertFalse(missing.is_open(99))
        outage, _ = self._oracle(lambda q, v: RuntimeError("rate limited"))
        self.assertIsNone(outage.is_open(12))                 # UNKNOWN -> NEEDS_HUMAN

    def test_closing_links_verified(self):
        o, _ = self._oracle(lambda q, v: {"data": {"repository": {"pullRequest": {
            "closingIssuesReferences": {"nodes": [{"number": 12}, {"number": 13}]}}}}})
        self.assertEqual(o.closing_links(7), {12, 13})
        outage, _ = self._oracle(lambda q, v: RuntimeError("boom"))
        self.assertEqual(outage.closing_links(7), set())      # no verified links -> V3 fails

    def test_recheck_at_land_composes(self):
        o, _ = self._oracle(lambda q, v: ({"data": {"repository": {"issue": {"state": "CLOSED"}}}}
                                          if "issue(" in q else
                                          {"data": {"repository": {"pullRequest": {
                                              "closingIssuesReferences": {"nodes": [{"number": 12}]}}}}}))
        self.assertEqual(recheck_at_land(o, 7, [12]), "closed")   # linked but closed -> abort


@unittest.skipUnless(gitutil.git_available() and gitutil.has_write_tree_merge(), "git >= 2.38")
class AtLandGuardTests(unittest.TestCase):
    def _seed(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"x.txt": "0\n"}, parent=None)
        w.ff_push("refs/heads/main", t0)
        head = w.write_commit({"x.txt": "0\n", "f.txt": "c\n"}, parent=t0)
        w.ff_push("refs/heads/pr1", head)
        return bare, w, t0, head

    def test_issue_closed_at_land_holds(self):
        bare, w, t0, head = self._seed()
        eng = BatchEngine(w, StateLog(bare, "refs/conductor/lane/x"),
                          StateLog(bare, "refs/conductor/flaky/x"), lane_id="x",
                          at_land_guard=lambda members: (False, "issue #12 closed"))
        eng.enqueue(1, "refs/heads/pr1", head, t0, ["root"], {"agent": "a", "closes": 12})
        res = eng.tick(from_green(lambda pr, tree: True))
        self.assertEqual(res.action, "held")                 # green CI, but at-land gate aborts
        self.assertEqual(eng.adv.current(), t0)              # trunk NOT advanced
        self.assertEqual(reduce(eng.lane.read()[0]).landed, [])

    def test_issue_open_at_land_lands(self):
        bare, w, t0, head = self._seed()
        eng = BatchEngine(w, StateLog(bare, "refs/conductor/lane/x"),
                          StateLog(bare, "refs/conductor/flaky/x"), lane_id="x",
                          at_land_guard=lambda members: (True, "ok"))
        eng.enqueue(1, "refs/heads/pr1", head, t0, ["root"], {"agent": "a", "closes": 12})
        self.assertEqual(eng.tick(from_green(lambda pr, tree: True)).action, "batch_landed")


# --- #5: real release ports ---------------------------------------------------

class IssuePortTests(unittest.TestCase):
    def test_close_if_open_idempotent(self):
        seq = []

        def run(argv, cwd=None):
            seq.append(argv)
            if argv[:3] == ["gh", "issue", "view"] and "state" in argv:
                return (0, '{"state":"OPEN"}', "")
            return (0, "", "")
        self.assertEqual(gh_ports.GitHubIssuePort("o/r", run=run).close_if_open(12), "closed")
        run2 = lambda argv, cwd=None: (0, '{"state":"CLOSED"}', "") if "state" in argv else (0, "", "")
        self.assertEqual(gh_ports.GitHubIssuePort("o/r", run=run2).close_if_open(12), "already-closed")

    def test_comment_once_dedupes(self):
        dup = lambda argv, cwd=None: (0, '{"comments":[{"body":"Released in web@1.1.0."}]}', "") \
            if "comments" in argv else (0, "", "")
        self.assertEqual(gh_ports.GitHubIssuePort("o/r", run=dup).comment_once(12, "web@1.1.0"), "dup")
        fresh = lambda argv, cwd=None: (0, '{"comments":[]}', "") if "comments" in argv else (0, "", "")
        self.assertEqual(gh_ports.GitHubIssuePort("o/r", run=fresh).comment_once(12, "web@1.1.0"), "commented")


class ReleasePortTests(unittest.TestCase):
    def test_create_if_absent(self):
        exists = lambda argv, cwd=None: (0, "", "")           # view succeeds -> exists
        self.assertEqual(gh_ports.GitHubReleasePort("o/r", run=exists).create_if_absent("t", "T", "b"), "exists")

        def absent(argv, cwd=None):
            return (1, "", "release not found") if argv[2] == "view" else (0, "", "")
        self.assertEqual(gh_ports.GitHubReleasePort("o/r", run=absent).create_if_absent("t", "T", "b"), "created")

        def race(argv, cwd=None):
            return (1, "", "release not found") if argv[2] == "view" else (1, "", "tag already exists")
        self.assertEqual(gh_ports.GitHubReleasePort("o/r", run=race).create_if_absent("t", "T", "b"), "exists")


class RegistryPortTests(unittest.TestCase):
    def test_publish_idempotent_and_errors(self):
        ok = lambda argv, cwd=None: (0, "", "")
        self.assertEqual(gh_ports.npm_registry(run=ok).publish_if_absent("web", "1.1.0"), "published")
        dup = lambda argv, cwd=None: (1, "", "npm ERR! You cannot publish over the previously published versions")
        self.assertEqual(gh_ports.npm_registry(run=dup).publish_if_absent("web", "1.1.0"), "exists")
        cargo_dup = lambda argv, cwd=None: (1, "", "error: crate version 1.1.0 is already uploaded")
        self.assertEqual(gh_ports.cargo_registry(run=cargo_dup).publish_if_absent("web", "1.1.0"), "exists")
        boom = lambda argv, cwd=None: (1, "", "network error")
        with self.assertRaises(RuntimeError):
            gh_ports.npm_registry(run=boom).publish_if_absent("web", "1.1.0")


@unittest.skipUnless(gitutil.git_available(), "git required")
class ReleaseWithReleasePortTests(unittest.TestCase):
    def test_github_release_created_exactly_once(self):
        bare = _bare()
        w = WorkRepo(bare)
        t0 = w.write_commit({"x.txt": "0\n"}, parent=None)
        w.ff_push("refs/heads/main", t0)
        rlog = StateLog(bare, "refs/conductor/release/main")
        plan = ReleasePlan(versions={"web": "1.1.0"}, consumed=["u1"], fragment_set_hash="h1",
                           changelog="- (web) add login")

        class FakeReleases:
            def __init__(self):
                self.calls = []
            def create_if_absent(self, tag, title, body):
                self.calls.append(tag)
                return "created"

        rel, reg, iss = FakeReleases(), RegistrySim(), IssueSim(open_issues={12})
        a = run_release(w, rlog, plan, "tokA", reg, iss, t0, verified_issues=[12], releases=rel)
        b = run_release(w, rlog, plan, "tokB", reg, iss, t0, verified_issues=[12], releases=rel)
        self.assertEqual(a, "released")
        self.assertEqual(b, "already-done")
        self.assertEqual(rel.calls, ["web-1.1.0"])            # GitHub Release created exactly once
        self.assertIn("h1", reduce_release(rlog.read()[0])["done"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
