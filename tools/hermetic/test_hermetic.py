#!/usr/bin/env python3
"""Unit tests for the hermetic resolution gate.

These use FAKE adapters (no real package managers needed) to prove the gate's
core guarantees: deterministic regeneration passes, nondeterministic regeneration
fails, and every missing precondition fails CLOSED.
"""

import os
import tempfile
import unittest

import hermetic as H


def _epoch(version="1.0.0", platform=None, snapshot=None, formats=None, runtimes=None):
    res = {"tool": "fake", "version": version}
    if runtimes:
        res["runtimes"] = runtimes
    return H.ResolutionEpoch(
        epoch_id="test",
        platform=platform if platform is not None else H.current_platform(),
        resolvers={"fake": res},
        registry_snapshot={"fake": snapshot if snapshot is not None else {}},
        formats=formats or {},
    )


def _workdir() -> str:
    d = tempfile.mkdtemp(prefix="hermetic-test-")
    with open(os.path.join(d, "fake.manifest"), "w") as fh:
        fh.write("dep-a@1\ndep-b@2\n")
    return d


class FakeDeterministic(H.Adapter):
    ecosystem = "fake"
    lockfile_name = "fake.lock"
    manifest_names = ("fake.manifest",)
    required_epoch_paths = ("resolvers.fake.version",)

    def installed_version(self):
        return "1.0.0"

    def regenerate(self, workdir, epoch):
        with open(os.path.join(workdir, "fake.manifest"), "rb") as fh:
            return b"lock:" + fh.read()


class FakeNondeterministic(FakeDeterministic):
    def __init__(self):
        self._n = 0

    def regenerate(self, workdir, epoch):
        self._n += 1
        return b"lock:run=" + str(self._n).encode()  # different every call


class FakePlatformSensitive(FakeDeterministic):
    platform_sensitive = True


class FakeNeedsSnapshot(FakeDeterministic):
    required_epoch_paths = ("resolvers.fake.version", "registry_snapshot.fake.before")


class GateTests(unittest.TestCase):
    def test_deterministic_passes(self):
        res = H.determinism_gate(FakeDeterministic(), _workdir(), _epoch(), runs=3)
        self.assertTrue(res.passed, res.reason)
        self.assertEqual(res.runs, 3)
        self.assertIsNotNone(res.digest)

    def test_nondeterministic_fails_closed(self):
        res = H.determinism_gate(FakeNondeterministic(), _workdir(), _epoch())
        self.assertFalse(res.passed)
        self.assertIn("NONDETERMINISTIC", res.reason)
        self.assertEqual(len(set(res.digests)), 2)

    def test_missing_epoch_field_fails_closed(self):
        # FakeNeedsSnapshot requires registry_snapshot.fake.before, which is absent.
        res = H.determinism_gate(FakeNeedsSnapshot(), _workdir(), _epoch())
        self.assertFalse(res.passed)
        self.assertIn("missing frozen epoch field", res.reason)

    def test_missing_field_satisfied_passes(self):
        res = H.determinism_gate(
            FakeNeedsSnapshot(), _workdir(),
            _epoch(snapshot={"before": "2026-06-24T00:00:00Z"}))
        self.assertTrue(res.passed, res.reason)

    def test_version_drift_fails_closed(self):
        res = H.determinism_gate(FakeDeterministic(), _workdir(), _epoch(version="9.9.9"))
        self.assertFalse(res.passed)
        self.assertIn("version drift", res.reason)

    def test_version_check_can_be_disabled(self):
        res = H.determinism_gate(FakeDeterministic(), _workdir(),
                                 _epoch(version="9.9.9"), enforce_version=False)
        self.assertTrue(res.passed, res.reason)

    def test_platform_drift_fails_closed(self):
        bad = {"os": "plan9", "arch": "pdp11", "libc": "none"}
        res = H.determinism_gate(FakePlatformSensitive(), _workdir(),
                                 _epoch(platform=bad))
        self.assertFalse(res.passed)
        self.assertIn("platform drift", res.reason)

    def test_resolver_error_fails_closed(self):
        class Boom(FakeDeterministic):
            def regenerate(self, workdir, epoch):
                raise H.HermeticError("resolver exploded")
        res = H.determinism_gate(Boom(), _workdir(), _epoch())
        self.assertFalse(res.passed)
        self.assertIn("resolver exploded", res.reason)

    def test_gateresult_is_falsy_on_fail(self):
        res = H.determinism_gate(FakeNondeterministic(), _workdir(), _epoch())
        self.assertFalse(bool(res))


class FakeFormatted(FakeDeterministic):
    def format_version(self, data):
        return "3"


class FakeTimeFreeze(FakeDeterministic):
    time_freeze_field = "registry_snapshot.fake.before"
    required_epoch_paths = ("resolvers.fake.version", "registry_snapshot.fake.before")


class FakeHeaderNoise(FakeDeterministic):
    """Regen emits a changing header line but a stable body; normalize strips the header."""
    def __init__(self):
        self._n = 0

    def regenerate(self, workdir, epoch):
        self._n += 1
        return b"# generated at run " + str(self._n).encode() + b"\nlock:stable\n"

    def normalize(self, data):
        lines = data.splitlines(keepends=True)
        i = 0
        while i < len(lines) and lines[i].lstrip().startswith(b"#"):
            i += 1
        return b"".join(lines[i:])


class FakeRuntime(FakeDeterministic):
    runtime_cmds = {"node": ("true",)}

    def runtimes(self):
        return {"node": "1.0.0"}


class NewDimensionTests(unittest.TestCase):
    def test_format_version_match_passes(self):
        res = H.determinism_gate(FakeFormatted(), _workdir(), _epoch(formats={"fake": "3"}))
        self.assertTrue(res.passed, res.reason)
        self.assertEqual(res.lockfile_format, "3")

    def test_format_version_drift_fails_closed(self):
        res = H.determinism_gate(FakeFormatted(), _workdir(), _epoch(formats={"fake": "9"}))
        self.assertFalse(res.passed)
        self.assertIn("format drift", res.reason)

    def test_relative_time_freeze_fails_closed(self):
        res = H.determinism_gate(FakeTimeFreeze(), _workdir(),
                                 _epoch(snapshot={"before": "P7D"}))
        self.assertFalse(res.passed)
        self.assertIn("relative", res.reason)

    def test_absolute_time_freeze_passes(self):
        res = H.determinism_gate(FakeTimeFreeze(), _workdir(),
                                 _epoch(snapshot={"before": "2026-06-24T00:00:00Z"}))
        self.assertTrue(res.passed, res.reason)

    def test_normalize_strips_volatile_header(self):
        # Raw bytes differ every run (changing header); normalize makes them identical.
        res = H.determinism_gate(FakeHeaderNoise(), _workdir(), _epoch(), runs=3)
        self.assertTrue(res.passed, res.reason)

    def test_runtime_drift_fails_closed(self):
        res = H.determinism_gate(FakeRuntime(), _workdir(),
                                 _epoch(runtimes={"node": "99.0.0"}))
        self.assertFalse(res.passed)
        self.assertIn("runtime drift", res.reason)

    def test_is_absolute_timestamp(self):
        self.assertTrue(H.is_absolute_timestamp("2026-06-24"))
        self.assertTrue(H.is_absolute_timestamp("2026-06-24T00:00:00Z"))
        self.assertFalse(H.is_absolute_timestamp("P7D"))
        self.assertFalse(H.is_absolute_timestamp("PT4H"))
        self.assertFalse(H.is_absolute_timestamp(""))


class EpochTests(unittest.TestCase):
    def test_get_dotted_path(self):
        e = _epoch(snapshot={"before": "X"})
        self.assertEqual(e.get("registry_snapshot.fake.before"), "X")
        self.assertIsNone(e.get("registry_snapshot.fake.nope"))
        self.assertIsNone(e.get("a.b.c.d"))

    def test_roundtrip(self):
        e = _epoch()
        d = tempfile.mkdtemp()
        p = os.path.join(d, "epoch.json")
        with open(p, "w") as fh:
            fh.write(e.to_json())
        e2 = H.ResolutionEpoch.load(p)
        self.assertEqual(e2.resolvers["fake"]["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
