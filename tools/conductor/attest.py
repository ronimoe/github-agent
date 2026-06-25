"""attest — self-asserted run-binding for a verdict (issue #9).

Binds a `VerdictRecord.record_digest` to the run claims it was produced under (run id, repo, sha,
workflow). HONEST SCOPE: `claims_from_env` reads caller-controlled `GITHUB_*` env strings — this is
NOT a verified OIDC JWT and the digest is content-addressed, not signed. The object is labeled
`signed=false, self_asserted=true`. Real non-repudiation needs BOTH the opt-in Sigstore path AND
OIDC-token signature verification, neither of which the core provides. Lives on
`refs/conductor/verdicts/<head_sha>` — an audit projection, never an authorization token.
"""

from __future__ import annotations

import hashlib
import json

_CLAIM_KEYS = ("run_id", "repo", "sha", "workflow", "ref", "actor", "run_attempt")


class SigstoreUnavailable(Exception):
    pass


def claims_from_env(env) -> dict:
    g = env.get
    return {
        "run_id": g("GITHUB_RUN_ID"), "repo": g("GITHUB_REPOSITORY"), "sha": g("GITHUB_SHA"),
        "workflow": g("GITHUB_WORKFLOW"), "ref": g("GITHUB_REF"), "actor": g("GITHUB_ACTOR"),
        "run_attempt": g("GITHUB_RUN_ATTEMPT"),
    }


def attest(verdict, claims: dict) -> dict:
    """Content-addressed, wall-clock-free => idempotent. `verdict` exposes record_digest + head_sha."""
    norm = {k: claims.get(k) for k in _CLAIM_KEYS}
    digest = hashlib.sha256(
        json.dumps([verdict.record_digest, sorted(norm.items())], sort_keys=True).encode()).hexdigest()
    return {"schema": "conductor.attest/v1", "record_digest": verdict.record_digest,
            "head_sha": verdict.head_sha, "claims": norm, "attest_digest": digest,
            "signed": False, "self_asserted": True}


def verify_attestation(att: dict, verdict) -> bool:
    """The attestation binds to THIS verdict only if its claimed sha equals the verdict head."""
    return (att.get("record_digest") == verdict.record_digest
            and att.get("head_sha") == verdict.head_sha
            and (att.get("claims") or {}).get("sha") == verdict.head_sha)


def sign_with_sigstore(att: dict, runner) -> dict:
    """Opt-in cryptographic signature via cosign/rekor. `runner(argv)->(rc,out,err)` injected.
    Raises SigstoreUnavailable if the toolchain is absent — core never imports cosign."""
    rc, out, err = runner(["cosign", "version"])
    if rc != 0:
        raise SigstoreUnavailable("cosign/rekor not available")
    rc, out, err = runner(["cosign", "attest-blob", "--predicate", "-", att["attest_digest"]])
    if rc != 0:
        raise SigstoreUnavailable(f"sigstore signing failed: {err.strip()[:200]}")
    return {**att, "signed": True, "self_asserted": False, "sigstore": out.strip()}
