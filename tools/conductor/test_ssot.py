#!/usr/bin/env python3
"""#9 tests: the provenance projection. Trailer emit/parse are inverse; the ledger is deterministic
and rebuildable; the issue↔changeset join uses the authoritative journal map (never guessed); trace
and verify are sound and fail closed; attestation is honestly self-asserted. Real git for the walk."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitutil  # noqa: E402
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from reducer import PRRecord  # noqa: E402
from land import land_message  # noqa: E402
from trailers import parse_trailers  # noqa: E402
from release_txn import (ev_release_started, ev_pkg_tagged, ev_pkg_released, ev_release_done,
                         ev_issue_closed)  # noqa: E402
import ssot  # noqa: E402
from ssot import (build_ledger, serialize, rebuild, trace, verify, DeterministicFakeReader)  # noqa: E402
import attest  # noqa: E402
from verifier import VerdictRecord  # noqa: E402


def _bare():
    d = tempfile.mkdtemp(prefix="conductor-bare-")
    subprocess.run(["git", "init", "--bare", "-q", d], check=True)
    return d


def _commit(w, parent, pr, change_id, closes, fname):
    rec = PRRecord(pr, f"refs/heads/pr{pr}", "h", "b", (), pr,
                   {"change_id": change_id, "closes": closes, "agent": "a", "model": "opus"})
    return w.write_commit({fname: f"{pr}\n"}, parent=parent, message=land_message(rec, "main", f"b{pr}"))


class AllResolved(DeterministicFakeReader):
    def pr_for_commit(self, sha):
        return 1


class TrailerTests(unittest.TestCase):
    def test_land_message_roundtrips(self):
        rec = PRRecord(7, "r", "h", "b", (), 7, {"closes": [12, 13], "change_id": "I7",
                                                 "agent": "a3", "model": "opus", "subject": "x"})
        t = parse_trailers(land_message(rec, "auth", "b1"))
        self.assertEqual(t["closes"], [12, 13])              # scalar->list cardinality fix
        self.assertEqual((t["change_id"], t["agent"], t["model"], t["batch"]), ("I7", "a3", "opus", "auth/b1"))

    def test_scalar_closes_still_one_line(self):
        rec = PRRecord(1, "r", "h", "b", (), 1, {"closes": 12, "agent": "a"})
        self.assertEqual(parse_trailers(land_message(rec))["closes"], [12])

    def test_untrusted_dropped_and_accumulated(self):
        msg = "Land #1\n\nCloses #12\nCloses #notanumber\nCloses #13\nX-Evil: 1\nChange-Id: I1\n"
        t = parse_trailers(msg)
        self.assertEqual(t["closes"], [12, 13])
        self.assertEqual(t["change_id"], "I1")


@unittest.skipUnless(gitutil.git_available(), "git required")
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.bare = _bare()
        self.w = WorkRepo(self.bare)
        self.t0 = self.w.write_commit({"x.txt": "0\n"}, parent=None, message="root")
        self.c1 = _commit(self.w, self.t0, 1, "I1", [12], "f1.txt")
        self.c2 = _commit(self.w, self.c1, 2, "I2", [13], "f2.txt")
        self.w.ff_push("refs/heads/main", self.c2)
        self.rel = StateLog(self.bare, "refs/conductor/release/main")

    def _release(self, ph, ulid, cid, issue=None):
        self.rel.append(ev_release_started(ph, "tok", [ulid], {ulid: cid}))
        self.rel.append(ev_pkg_tagged("web", "1.1.0", "tok"))
        self.rel.append(ev_pkg_released("web", "1.1.0"))
        if issue is not None:
            self.rel.append(ev_issue_closed("web", "1.1.0", issue))
        self.rel.append(ev_release_done(ph))

    def test_build_ledger_deterministic_first_parent(self):
        self._release("h1", "u1", "I1")
        recs, outage = build_ledger(self.w, "", "refs/heads/main", self.rel, AllResolved())
        self.assertEqual([r["commit_sha"] for r in recs], [self.t0, self.c1, self.c2])  # oldest first
        self.assertEqual(serialize(recs), serialize(build_ledger(self.w, "", "refs/heads/main", self.rel, AllResolved())[0]))

    def test_join_via_journal_only(self):
        self._release("h1", "u1", "I1")                      # I1 mapped in the journal, I2 is not
        recs, _ = build_ledger(self.w, "", "refs/heads/main", self.rel, AllResolved())
        by = {r["change_id"]: r for r in recs}
        self.assertEqual(by["I1"]["consumed_ulids"], ["u1"])
        self.assertEqual(by["I1"]["plan_hash"], "h1")
        self.assertEqual(by["I2"]["consumed_ulids"], [])     # unresolved -> NOT guessed
        self.assertIn("unresolved:no-changeset-for-change_id", by["I2"]["reasons"])

    def test_rebuild_idempotent(self):
        self._release("h1", "u1", "I1")
        c1, r1, _ = rebuild(self.w, "", "refs/heads/main", self.rel, AllResolved())
        c2, r2, _ = rebuild(self.w, "", "refs/heads/main", self.rel, AllResolved())
        self.assertEqual((c1, r1), (True, "rebuilt"))
        self.assertEqual((c2, r2), (False, "unchanged"))

    def test_rebuild_reader_outage_does_not_thin(self):
        outage_reader = DeterministicFakeReader(pr_by_commit={})   # pr_for_commit -> None everywhere
        changed, reason, _ = rebuild(self.w, "", "refs/heads/main", self.rel, outage_reader)
        self.assertFalse(changed)
        self.assertEqual(reason, "reader-outage")

    def test_trace_released(self):
        self._release("h1", "u1", "I1", issue=12)
        reader = DeterministicFakeReader(states={12: "closed"}, closing={12: {1}},
                                         commits={1: [self.c1]}, pr_by_commit={self.c1: 1})
        out = trace(12, reader, self.w, "", "refs/heads/main", self.rel)
        self.assertEqual(out["links"][0]["released"], True)
        self.assertEqual(out["links"][0]["plan_hash"], "h1")
        self.assertEqual(out["broken"], [])

    def test_trace_broken_links(self):
        reader = DeterministicFakeReader(states={12: "closed"}, closing={12: {1}}, commits={1: []})
        self.assertIn("no-commit-for-pr", trace(12, reader, self.w, "", "refs/heads/main", self.rel)["broken"])

    def test_verify_revert_not_on_trunk(self):
        # journal releases a change I9 that no trunk commit carries (reverted/force-pushed out)
        self.rel.append(ev_release_started("h9", "tok", ["u9"], {"u9": "I9"}))
        self.rel.append(ev_release_done("h9"))
        rep = verify(DeterministicFakeReader(), self.w, "", "refs/heads/main", self.rel)
        self.assertFalse(rep["ok"])
        self.assertIn("released-change-not-on-trunk", [v["kind"] for v in rep["violations"]])

    def test_verify_fail_closed_on_outage(self):
        self._release("h1", "u1", "I1", issue=12)            # closes 12, but reader is UNKNOWN
        rep = verify(DeterministicFakeReader(states={}), self.w, "", "refs/heads/main", self.rel)
        self.assertFalse(rep["ok"])
        self.assertIn("ledger-stale", [v["kind"] for v in rep["violations"]])

    def test_verify_github_open_is_unbacked(self):
        self._release("h1", "u1", "I1", issue=12)
        rep = verify(DeterministicFakeReader(states={12: "open"}), self.w, "", "refs/heads/main", self.rel)
        self.assertIn("unbacked-closed-issue", [v["kind"] for v in rep["violations"]])

    def test_verify_clean_passes(self):
        self._release("h1", "u1", "I1", issue=12)
        rep = verify(DeterministicFakeReader(states={12: "closed"}), self.w, "", "refs/heads/main", self.rel)
        self.assertTrue(rep["ok"], rep["violations"])


class AttestTests(unittest.TestCase):
    def _verdict(self):
        return VerdictRecord("PASS", {}, "base", "HEADSHA", "1", "digest-abc")

    def test_self_asserted_idempotent_head_bound(self):
        v = self._verdict()
        claims = {"sha": "HEADSHA", "run_id": "5", "repo": "o/r"}
        a1, a2 = attest.attest(v, claims), attest.attest(v, claims)
        self.assertEqual(a1["attest_digest"], a2["attest_digest"])       # wall-clock excluded
        self.assertFalse(a1["signed"])
        self.assertTrue(a1["self_asserted"])
        self.assertTrue(attest.verify_attestation(a1, v))
        bad = attest.attest(v, {"sha": "OTHER"})
        self.assertFalse(attest.verify_attestation(bad, v))              # sha mismatch -> not bound

    def test_sigstore_optin_absent_raises(self):
        with self.assertRaises(attest.SigstoreUnavailable):
            attest.sign_with_sigstore({"attest_digest": "x"}, lambda argv: (1, "", "not found"))


class AuthorityGuardTests(unittest.TestCase):
    def test_ssot_not_imported_by_blocking_path(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for mod in ("land.py", "release_txn.py", "verifier.py", "batch_engine.py"):
            src = open(os.path.join(here, mod)).read()
            self.assertNotIn("import ssot", src, f"{mod} must not import ssot (no authority split-brain)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
