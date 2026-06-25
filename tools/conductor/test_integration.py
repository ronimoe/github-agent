#!/usr/bin/env python3
"""Slice-1 tests: the engine-side GitHub-integration pieces that ARE unit-testable offline —
the hermetic test runtime + drift probe (#6), the CI-report → verdict bridge (#1), and the App
JWT assembly/signing (#3). The Actions workflows and tick/ci runner glue need a live repo + App
to validate end-to-end (see INTEGRATION.md)."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hermetic_test  # noqa: E402
import ghapp  # noqa: E402
from ci_verdict import (verdict_from_report, verdict_from_rep_reports, CheckRunVerdict)  # noqa: E402
from verdict import classify, DET_PASS, DET_FAIL, UNRESOLVED  # noqa: E402


class HermeticRuntimeTests(unittest.TestCase):
    def test_drift_probe_passes_for_deterministic(self):
        det, digs = hermetic_test.drift_probe(["python3", "-c", "print('hello')"], ".", runs=3)
        self.assertTrue(det)
        self.assertEqual(len(set(digs)), 1)

    def test_drift_probe_flags_nondeterministic(self):
        det, _ = hermetic_test.drift_probe(
            ["python3", "-c", "import random; print(random.random())"], ".", runs=3)
        self.assertFalse(det)               # not hermetic -> caught by the probe

    def test_run_reps_pass_and_fail(self):
        self.assertEqual(hermetic_test.run_reps(["true"], ".", reps=3), [True, True, True])
        self.assertEqual(hermetic_test.run_reps(["false"], ".", reps=2), [False, False])

    def test_frozen_env_is_pinned(self):
        env = hermetic_test.frozen_env()
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["PYTHONHASHSEED"], "0")
        self.assertEqual(env["CONDUCTOR_NETWORK"], "deny")


class CiVerdictTests(unittest.TestCase):
    def test_suite_report_classifies(self):
        self.assertEqual(classify(verdict_from_report({"deterministic": True, "suite_reps": [True, True, True]}).per_test),
                         {"_suite": DET_PASS})
        self.assertEqual(classify(verdict_from_report({"deterministic": True, "suite_reps": [False, False]}).per_test),
                         {"_suite": DET_FAIL})
        self.assertEqual(classify(verdict_from_report({"deterministic": True, "suite_reps": [True, False, True]}).per_test),
                         {"_suite": UNRESOLVED})

    def test_per_test_report(self):
        v = verdict_from_rep_reports([{"a": True, "b": True}, {"a": True, "b": False}])
        self.assertEqual(classify(v.per_test), {"a": DET_PASS, "b": UNRESOLVED})

    def test_non_hermetic_fails_closed(self):
        v = verdict_from_report({"deterministic": False, "suite_reps": [True, True, True]})
        self.assertEqual(classify(v.per_test), {"_hermetic": DET_FAIL})   # green reps don't excuse drift

    def test_check_run_verdict_seam(self):
        cv = CheckRunVerdict(lambda spec_tree, reps: {"deterministic": True, "suite_reps": [True, True]})
        self.assertEqual(classify(cv("tree", [1], 2).per_test), {"_suite": DET_PASS})
        pending = CheckRunVerdict(lambda spec_tree, reps: None)           # no result yet -> UNRESOLVED -> held
        self.assertEqual(classify(pending("tree", [1], 2).per_test), {"_ci": UNRESOLVED})


class GhAppTests(unittest.TestCase):
    def _b64url_decode(self, s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def test_signing_input_shape(self):
        si = ghapp.build_signing_input(12345, 1000, 1540)
        header_b64, payload_b64 = si.split(".")
        self.assertEqual(json.loads(self._b64url_decode(header_b64)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(self._b64url_decode(payload_b64)),
                         {"iat": 1000, "exp": 1540, "iss": "12345"})

    @unittest.skipUnless(shutil.which("openssl"), "openssl required")
    def test_rs256_sign_verifies(self):
        d = tempfile.mkdtemp()
        try:
            key = os.path.join(d, "key.pem")
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", key,
                            "-pkeyopt", "rsa_keygen_bits:2048"], check=True, capture_output=True)
            jwt = ghapp.app_jwt(42, key, now=1_700_000_000)
            self.assertEqual(jwt.count("."), 2)
            si, sig_b64 = jwt.rsplit(".", 1)
            # verify the signature with the public key
            pub = os.path.join(d, "pub.pem")
            subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
            sig = os.path.join(d, "sig.bin")
            with open(sig, "wb") as fh:
                fh.write(self._b64url_decode(sig_b64))
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sig],
                               input=si.encode(), capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"Verified OK", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
