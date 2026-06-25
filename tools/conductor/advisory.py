"""advisory — the cross-model reviewer (P4, ADR-0008). ADVISORY and comment-only: it NEVER
reaches a required status check. On outage it is simply non-blocking. No LLM-derived field is
allowed into the merge-blocking path; the only hard gate on risk-tier paths is the human approval."""

from __future__ import annotations


def ev_advisory(head_sha, summary):
    return {"type": "advisory_review", "head_sha": head_sha, "summary": summary}


def ev_advisory_unavailable(head_sha, reason):
    return {"type": "advisory_review_unavailable", "head_sha": head_sha, "reason": reason}


def run_advisory(reviewer, head_sha, diff) -> dict:
    """Run a different-model-family reviewer for a non-blocking comment. Any failure/timeout is
    swallowed into a non-blocking 'unavailable' event — it changes NO required check."""
    try:
        return ev_advisory(head_sha, reviewer(diff))
    except Exception as e:                            # noqa: BLE001 — advisory must never raise into the gate
        return ev_advisory_unavailable(head_sha, str(e))


def ev_human_approval(head_sha, approver):
    return {"type": "human_approval", "head_sha": head_sha, "approver": approver}


def approval_satisfied(events, head_sha) -> bool:
    """A human approval satisfies the gate ONLY for the exact head it was given on — a force-push
    or rebase that moves the head voids it."""
    return any(e.get("type") == "human_approval" and e.get("head_sha") == head_sha for e in events)
