"""verifier — the required, deterministic, NO-LLM correctness gate (P4, ADR-0008).

V1 semver >= inferred public-API bump; V2 changeset well-formed; V3 Closes-#N verified-linked +
OPEN; V4 footprint corroborated; V5 no conflict markers + required trailers. All checks run on the
REALIZED merge tree (diff base->merged-tree), not a 2-dot author diff, so the verified name-set ==
the landed name-set. The fold: any FAIL => FAIL; else any UNCERTAIN/NEEDS_HUMAN/risk-tier =>
NEEDS_HUMAN; else PASS. record_digest excludes wall-clock, so re-verifying the same (base, head,
ruleset) is byte-identical. A FAIL injects a reserved `_mechanical` DET_FAIL into the verdict seam
so it rides the SAME fail-closed path as a real regression — quarantine can never excuse it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field

import apidiff
from changeset import parse_fragment, ChangesetError
from issues import recheck_at_land
from risktier import is_risk

PASS, FAIL, NEEDS_HUMAN = "PASS", "FAIL", "NEEDS_HUMAN"


@dataclass
class VerdictRecord:
    status: str
    checks: dict = field(default_factory=dict)
    base_sha: str = ""
    head_sha: str = ""
    ruleset_version: str = "1"
    record_digest: str = ""


def _footprint(globs, paths) -> str:
    if not globs:
        return "FAIL:no-footprint" if paths else PASS
    for g in globs:
        if not any(fnmatch.fnmatch(p, g) for p in paths):
            return f"FAIL:glob {g!r} matches no changed file"
    for p in paths:
        if not any(fnmatch.fnmatch(p, g) for g in globs):
            return f"FAIL:undeclared changed path {p!r}"
    return PASS


def _v5(repo, merged_tree, changed, trailers) -> str:
    for status, path in changed:
        if status == "D":
            continue
        content = repo.read_blob(merged_tree, path) or ""
        if "<<<<<<< " in content and ">>>>>>> " in content:
            return f"FAIL:conflict marker in {path}"
    for t in ("change_id", "agent"):
        if not trailers.get(t):
            return f"FAIL:missing {t} trailer"
    return PASS


def _digest(base, head, ruleset, checks) -> str:
    return hashlib.sha256(
        json.dumps([base, head, ruleset, checks], sort_keys=True).encode()).hexdigest()


def verify(repo, base, merged_tree, head_sha, fragment_text, trailers, footprint, oracle, pr,
           ruleset_version="1") -> VerdictRecord:
    checks = {}
    changed = repo.diff_name_status(base, merged_tree)     # the REALIZED diff
    paths = [p for _, p in changed]

    # V2 changeset well-formed
    try:
        frag = parse_fragment(fragment_text)
        checks["V2"] = PASS
    except ChangesetError as e:
        frag, checks["V2"] = None, f"FAIL:{e}"

    # V1 semver >= inferred
    inferred, uncertain = apidiff.infer_bump(repo, base, merged_tree, changed)
    declared = max(frag.bumps.values()) if (frag and frag.bumps) else apidiff.NONE
    if frag is None:
        checks["V1"] = "FAIL:no-changeset"
    elif declared < inferred:
        checks["V1"] = f"FAIL:declared {apidiff.NAMES[declared]} < inferred {apidiff.NAMES[inferred]}"
    elif uncertain:
        checks["V1"] = "UNCERTAIN"
    else:
        checks["V1"] = PASS

    # V3 issue verified-linked + open (precheck)
    closes = trailers.get("closes")
    issues = [int(closes)] if closes else (frag.issues if frag else [])
    v3 = PASS
    for n in issues:
        if n not in oracle.closing_links(pr):
            v3 = f"FAIL:#{n} not closing-linked to PR"
            break
        st = oracle.is_open(n)
        if st is None:
            v3 = "NEEDS_HUMAN:issue-oracle-unknown"
            break
        if st is False:
            v3 = f"FAIL:#{n} closed"
            break
    checks["V3"] = v3

    checks["V4"] = _footprint(footprint, paths)
    checks["V5"] = _v5(repo, merged_tree, changed, trailers)

    vals = list(checks.values())
    if any(v.startswith("FAIL") for v in vals):
        status = FAIL
    elif any(v.startswith("NEEDS_HUMAN") or v == "UNCERTAIN" for v in vals) or is_risk(paths):
        status = NEEDS_HUMAN
    else:
        status = PASS
    return VerdictRecord(status, checks, base, merged_tree, ruleset_version,
                         _digest(base, head_sha, ruleset_version, checks))


def mechanical_overlay(status) -> dict:
    """Per-test entries to MERGE into the CI BatchVerdict so a mechanical FAIL becomes a DET_FAIL
    (verdict.decide -> red), and a NEEDS_HUMAN becomes a held gate that quarantine can't excuse."""
    if status == FAIL:
        return {"_mechanical": [False]}            # DET_FAIL -> decide() red (quarantine can't excuse)
    if status == NEEDS_HUMAN:
        return {"_mechanical_human": [True, False]}  # UNRESOLVED + unestablished -> decide() held
    return {}
