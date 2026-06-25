"""ci_verdict — bridge real per-PR CI results into the engine's verdict seam (issue #1).

`BatchEngine.tick(verdict, ...)` calls `verdict(spec_tree, members, reps) -> BatchVerdict`. This
module turns the per-rep reports produced by the hermetic CI runner (`ci_runner.py`, run under the
hermetic test runtime) into that BatchVerdict, so the engine's DET_PASS / DET_FAIL / UNRESOLVED
classification is driven by REAL repeated runs — not a bool. `CheckRunVerdict` is the seam: the
fetch callback is injected (tested offline with a fake), and the production fetch reads the
`conductor-spec` check-run output for the spec's head SHA via `gh api`.
"""

from __future__ import annotations

from verdict import BatchVerdict


def verdict_from_rep_reports(reports) -> BatchVerdict:
    """reports: list of per-rep dicts {test_id: passed_bool}. -> BatchVerdict (per_test across reps)."""
    tests: dict = {}
    for rep in reports:
        for tid, ok in rep.items():
            tests.setdefault(tid, []).append(bool(ok))
    return BatchVerdict(tests)


def verdict_from_suite(rep_pass) -> BatchVerdict:
    """rep_pass: list[bool] overall pass/fail per rep -> a single `_suite` test."""
    return BatchVerdict({"_suite": [bool(x) for x in rep_pass]})


def verdict_from_report(report: dict) -> BatchVerdict:
    """Parse a `conductor-spec` report (as written by ci_runner). Prefers per-test data; falls back
    to the suite pass/fail per rep. A failed drift probe forces a hard fail (non-hermetic runtime)."""
    if not report.get("deterministic", True):
        return BatchVerdict({"_hermetic": [False]})         # runtime not hermetic => DET_FAIL, fail closed
    if "per_test_reps" in report:
        return verdict_from_rep_reports(report["per_test_reps"])
    return verdict_from_suite(report.get("suite_reps", []))


class CheckRunVerdict:
    """A verdict(spec_tree, members, reps) seam backed by `fetch(spec_tree, reps) -> report dict`.
    Inject a fake fetch for tests; in production fetch reads the conductor-spec check-run."""

    def __init__(self, fetch):
        self.fetch = fetch

    def __call__(self, spec_tree, members, reps=3, pr_absent=False) -> BatchVerdict:
        report = self.fetch(spec_tree, reps)
        if report is None:
            return BatchVerdict({"_ci": [True, False]})     # no result yet => UNRESOLVED => held
        return verdict_from_report(report)
