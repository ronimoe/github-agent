"""verdict — per-test determinism classification and the fail-closed land decision (P2).

The P1 seam `green(pr, tree) -> bool` is replaced by `verdict(spec_tree, members, reps)
-> BatchVerdict`, which reports each test's outcome across `reps` byte-identical runs
under the hermetic harness (DESIGN.md §9 extended to the test runtime). This is the
soundness pivot the adversarial pass forced:

  * a spec TREE hashes source content, NOT the execution environment — so "same tree,
    different outcome" is NOT a flake, it is UNRESOLVED;
  * landing requires a clean DET_PASS (or excusal by an INDEPENDENT signal — a test's
    flip history on PR-ABSENT trees); an intermittent red FAILS CLOSED (holds);
  * a DET_FAIL is a real deterministic regression and can NEVER be excused by quarantine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DET_PASS = "DET_PASS"
DET_FAIL = "DET_FAIL"
UNRESOLVED = "UNRESOLVED"


@dataclass
class BatchVerdict:
    # per_test: test_id -> list[bool] (one pass/fail per byte-identical rep)
    per_test: dict = field(default_factory=dict)


def classify(per_test: dict) -> dict:
    out = {}
    for tid, reps in per_test.items():
        if all(reps):
            out[tid] = DET_PASS
        elif not any(reps):
            out[tid] = DET_FAIL
        else:
            out[tid] = UNRESOLVED
    return out


@dataclass
class Decision:
    kind: str            # "green" | "red" | "hold"
    tests: list = field(default_factory=list)
    detail: str = ""


def decide(classified: dict, flaky) -> Decision:
    """Map per-test determinism statuses + the independent flake signal to a land decision.
    `flaky` is a FlakyState exposing established_flaky(t) and stable_pr_absent(t)."""
    det_fail = [t for t, c in classified.items() if c == DET_FAIL]
    if det_fail:
        # A deterministic regression. Quarantine never excuses it.
        return Decision("red", det_fail, "deterministic")

    unresolved = [t for t, c in classified.items() if c == UNRESOLVED]
    if not unresolved:
        return Decision("green")

    candidate_race, held = [], []
    for t in unresolved:
        if flaky.established_flaky(t):
            continue                       # excused: established flaky from PR-ABSENT evidence
        elif flaky.stable_pr_absent(t):
            candidate_race.append(t)       # stable without the batch => a member introduced non-determinism
        else:
            held.append(t)                 # thin evidence => fail closed
    if candidate_race:
        return Decision("red", candidate_race, "candidate-race")
    if held:
        return Decision("hold", held, "unresolved-thin-evidence")
    return Decision("green", detail="all-unresolved-excused")


def from_green(green):
    """Adapter: lift a P1 `green(pr, tree) -> bool` into the P2 verdict seam, so the m=1
    path is byte-for-byte the P1 land decision (one suite test, reps collapse to the bool)."""
    def verdict(spec_tree, members, reps=1, pr_absent=False):
        pr = members[-1] if members else None
        ok = bool(green(pr, spec_tree))
        return BatchVerdict({"_suite": [ok] * max(1, reps)})
    return verdict
