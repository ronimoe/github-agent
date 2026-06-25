"""advisory — the cross-model reviewer + the required human/reviewer-App approval gate (P4 / #10).

Two distinct things, deliberately kept apart:

  * The CROSS-MODEL REVIEWER is ADVISORY and comment-only. It runs a DIFFERENT model family than the
    author (to decorrelate blind spots) and NEVER reaches a required check. Any failure/timeout is
    swallowed into a non-blocking "unavailable" event — no LLM-derived field ever blocks a merge.
  * The APPROVAL GATE is the real, blocking control on NEEDS_HUMAN / risk-tier paths. An approval
    only counts if it is bound to the exact head SHA (a force-push/rebase voids it) AND comes from a
    human or a designated reviewer-App — an AGENT can never self-approve. A `deadlock_guard` flags an
    all-agent CODEOWNERS, which would otherwise wedge `require_code_owner_review` for an agent fleet.
"""

from __future__ import annotations

# Approver types that may satisfy a required human approval. An "agent" never can.
ELIGIBLE_APPROVERS = {"human", "reviewer-app"}


# --- advisory cross-model reviewer (never blocks) ----------------------------

def ev_advisory(head_sha, summary, reviewer_model=None, decorrelated=None):
    return {"type": "advisory_review", "head_sha": head_sha, "summary": summary,
            "reviewer_model": reviewer_model, "decorrelated": decorrelated}


def ev_advisory_unavailable(head_sha, reason):
    return {"type": "advisory_review_unavailable", "head_sha": head_sha, "reason": reason}


def run_advisory(reviewer, head_sha, diff) -> dict:
    """Run a reviewer callable for a non-blocking comment; any failure -> non-blocking 'unavailable'."""
    try:
        return ev_advisory(head_sha, reviewer(diff))
    except Exception as e:                            # noqa: BLE001 — advisory must never raise into the gate
        return ev_advisory_unavailable(head_sha, str(e))


class CrossModelReviewer:
    """A different-model-family reviewer. `infer(prompt) -> str` is injected (fail-open, comment-only).
    `decorrelated` is False when the reviewer family equals the author family — an advisory note, never
    a block."""

    def __init__(self, infer, model: str, family: str):
        self.infer = infer
        self.model = model
        self.family = family

    def _prompt(self, diff: str) -> str:
        return ("Review this change for correctness and security risks. Be terse; flag only real "
                f"issues.\n\n{diff}")

    def review(self, head_sha, diff, author_family=None) -> dict:
        decorrelated = author_family is None or self.family != author_family
        try:
            summary = self.infer(self._prompt(diff))
        except Exception as e:                        # noqa: BLE001 — fail open to non-blocking
            return ev_advisory_unavailable(head_sha, str(e))
        return ev_advisory(head_sha, summary, reviewer_model=self.model, decorrelated=decorrelated)


# --- required human / reviewer-App approval (blocks NEEDS_HUMAN) --------------

def ev_human_approval(head_sha, approver, approver_type="human"):
    return {"type": "human_approval", "head_sha": head_sha, "approver": approver,
            "approver_type": approver_type}


def approval_satisfied(events, head_sha) -> bool:
    """A required approval is satisfied only by an ELIGIBLE approver bound to THIS exact head (so a
    force-push/rebase that moves the head voids it, and an agent can never self-approve)."""
    return any(e.get("type") == "human_approval"
               and e.get("head_sha") == head_sha
               and e.get("approver_type", "human") in ELIGIBLE_APPROVERS
               for e in events)


def deadlock_guard(codeowner_types) -> tuple[bool, str]:
    """codeowner_types: the approver types present in CODEOWNERS. An all-agent set would deadlock
    `require_code_owner_review` for an agent fleet."""
    if any(t in ELIGIBLE_APPROVERS for t in codeowner_types):
        return True, "ok"
    return False, "no human/reviewer-app code owner: require_code_owner_review would deadlock"


def approval_aware_overlay(status, head_sha, approval_events) -> dict:
    """Bridge a verifier status (PASS|FAIL|NEEDS_HUMAN) to the verdict seam, accounting for approvals.
    A mechanical FAIL is a DET_FAIL (red). A NEEDS_HUMAN holds (UNRESOLVED) until a valid head-bound
    eligible approval arrives, then clears to green."""
    if status == "FAIL":
        return {"_mechanical": [False]}              # DET_FAIL -> decide() red
    if status == "NEEDS_HUMAN":
        if approval_satisfied(approval_events, head_sha):
            return {}                                # approved -> green
        return {"_mechanical_human": [True, False]}  # UNRESOLVED -> decide() held, awaiting approval
    return {}
