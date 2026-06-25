#!/usr/bin/env python3
"""P5 tests: the nonce-tag mutex, the commutative fold, the dependent-bump graph + cycle
detection, the pure-text adapters, collision-free fragment paths, and the exactly-once /
crash-recoverable release transaction (real git for tags + releases)."""

import itertools
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
import semver  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from apidiff import NONE, PATCH, MINOR, MAJOR  # noqa: E402
from changeset import Fragment, fragment_path  # noqa: E402
from versionplan import fold, VersionGraph, ReleasePlan  # noqa: E402
from adapters import adapter_for, GomodAdapter  # noqa: E402
from release_planner import build_plan, in_flight_consumed  # noqa: E402
from release_txn import (run_release, RegistrySim, IssueSim, reduce_release,
                         ev_release_started, ev_pkg_tagged)  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


class FoldGraphTests(unittest.TestCase):
    def test_fold_commutative_and_idempotent(self):
        f = [Fragment(bumps={"web": PATCH}, body="a"),
             Fragment(bumps={"web": MAJOR}, body="b"),
             Fragment(bumps={"web": MINOR}, body="c")]
        for perm in itertools.permutations(f):
            self.assertEqual(fold(list(perm)), {"web": MAJOR})
        self.assertEqual(fold(f + [f[1]]), {"web": MAJOR})       # duplicating changes nothing

    def test_dependent_bump_propagates(self):
        g = VersionGraph(versions={"app": "1.0.0", "lib": "2.0.0"}, deps={"app": ["lib"]})
        bumps = g.propagate({"lib": MINOR})
        self.assertEqual(bumps["lib"], MINOR)
        self.assertEqual(bumps["app"], PATCH)                    # dependent bumped >= patch

    def test_cycle_surfaces_error(self):
        g = VersionGraph(versions={"a": "1.0.0", "b": "1.0.0"}, deps={"a": ["b"], "b": ["a"]})
        with self.assertRaises(ValueError):
            g.propagate({"a": MINOR})

    def test_single_version_collapses(self):
        plan = build_plan([("u1", Fragment(bumps={"web": MAJOR}, body="x")),
                           ("u2", Fragment(bumps={"api": PATCH}, body="y"))],
                          {"_default": "1.4.2"}, single_version=True)
        self.assertEqual(plan.bumps, {"_default": MAJOR})
        self.assertEqual(plan.versions, {"_default": "2.0.0"})

    def test_zero_major_policy(self):
        self.assertEqual(semver.bump_version("0.3.1", MAJOR), "0.4.0")              # 0.x default
        self.assertEqual(semver.bump_version("0.3.1", MAJOR, zero_major_bumps_major=True), "1.0.0")


class AdapterTests(unittest.TestCase):
    def test_roundtrip_pure_text(self):
        cases = [("package.json", '{"name":"x","version":"1.2.3"}', "npm"),
                 ("Cargo.toml", '[package]\nname = "x"\nversion = "1.2.3"\n', "cargo"),
                 ("pyproject.toml", '[project]\nname = "x"\nversion = "1.2.3"\n', "pep621"),
                 ("VERSION", "1.2.3\n", "plain")]
        for fn, content, name in cases:
            a = adapter_for(fn)
            self.assertEqual(a.name, name)
            self.assertEqual(a.read_version(content), "1.2.3")
            out = a.write_version(content, semver.bump_version("1.2.3", MINOR))
            self.assertEqual(a.read_version(out), "1.3.0")
            self.assertEqual(out.count("1.3.0"), 1)              # only the version changed
        self.assertEqual(GomodAdapter().tag_template("m", "1.2.3"), "v1.2.3")

    def test_fragment_paths_never_collide(self):
        paths = {fragment_path(1_700_000_000_000, f"agent{i}", f"{i:08x}", bytes([i]) * 10)
                 for i in range(50)}                              # same millisecond, 50 agents
        self.assertEqual(len(paths), 50)


class PlannerFenceTests(unittest.TestCase):
    def test_replanning_fenced_against_inflight_consumption(self):
        bare = _bare()
        rlog = StateLog(bare, "refs/conductor/release/main")
        rlog.append(ev_release_started("h1", "tok", ["u1", "u2"]))   # landed, not yet done
        infl = in_flight_consumed(rlog)
        self.assertEqual(infl, {"u1", "u2"})
        frags = [("u1", Fragment(bumps={"web": MINOR}, body="a")),
                 ("u2", Fragment(bumps={"web": MAJOR}, body="b"))]
        fenced = build_plan(frags, {"web": "1.0.0"}, exclude_ulids=infl)
        self.assertEqual(fenced.versions, {})                    # all fragments fenced -> no bump
        self.assertEqual(fenced.consumed, [])
        unfenced = build_plan(frags, {"web": "1.0.0"})
        self.assertEqual(unfenced.versions, {"web": "2.0.0"})    # major wins the fold


@unittest.skipUnless(gitutil.git_available(), "git required")
class ReleaseTxnTests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.w = WorkRepo(self.bare)
        self.t0 = self.w.write_commit({"x.txt": "0\n"}, parent=None)
        self.w.ff_push("refs/heads/main", self.t0)
        self.rlog = StateLog(self.bare, "refs/conductor/release/main")
        self.plan = ReleasePlan(versions={"web": "1.1.0"}, consumed=["u1"], fragment_set_hash="h1")

    def test_tag_mutex_nonce_unique_winner(self):
        wa, wb = WorkRepo(self.bare), WorkRepo(self.bare)
        wa.fetch_ref("refs/heads/main")
        wb.fetch_ref("refs/heads/main")
        ta = wa.make_annotated_tag("web-1.1.0", self.t0, "nonce=AAA")
        tb = wb.make_annotated_tag("web-1.1.0", self.t0, "nonce=BBB")
        self.assertNotEqual(ta, tb)                              # distinct tag OIDs
        results = [wa.push_tag_create_only("web-1.1.0"), wb.push_tag_create_only("web-1.1.0")]
        self.assertEqual(results.count("won"), 1)                # exactly one winner
        self.assertEqual(results.count("lost"), 1)

    def test_exactly_once_under_concurrency(self):
        reg, iss = RegistrySim(), IssueSim(open_issues={12})
        ra = run_release(self.w, self.rlog, self.plan, "tokA", reg, iss, self.t0, verified_issues=[12])
        rb = run_release(self.w, self.rlog, self.plan, "tokB", reg, iss, self.t0, verified_issues=[12])
        self.assertEqual(ra, "released")
        self.assertEqual(rb, "already-done")                    # second runner is a no-op
        self.assertEqual(reg.published, {("web", "1.1.0")})     # exactly one publish
        self.assertNotIn(12, iss.open)                          # closed exactly once
        self.assertEqual(len(iss.comments[12]), 1)              # one comment, not two

    def test_crash_recovery_idempotent(self):
        # Simulate a crash AFTER the tag election but BEFORE publish.
        self.rlog.append(ev_release_started("h1", "tokA", ["u1"]))
        self.rlog.append(ev_pkg_tagged("web", "1.1.0", "tokA"))
        reg, iss = RegistrySim(), IssueSim(open_issues={12})
        out = run_release(self.w, self.rlog, self.plan, "tokA", reg, iss, self.t0, verified_issues=[12])
        self.assertEqual(out, "released")
        self.assertEqual(reg.published, {("web", "1.1.0")})
        self.assertNotIn(12, iss.open)
        self.assertIn("h1", reduce_release(self.rlog.read()[0])["done"])
        # re-run is a pure no-op
        self.assertEqual(run_release(self.w, self.rlog, self.plan, "tokA", reg, iss, self.t0, [12]),
                         "already-done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
