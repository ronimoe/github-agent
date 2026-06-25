"""issues — the issue oracle for V3 (P4). `Closes #N` is untrusted author input: an issue may
only be actioned if it is (a) genuinely closing-linked to the PR (verified, e.g. GraphQL
closingIssuesReferences) AND (b) OPEN — re-checked at the merge moment. UNKNOWN (oracle outage)
fails closed to human review, never a silent land."""

from __future__ import annotations

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"


class IssueOracle:
    def is_open(self, n) -> bool | None:
        """True (open), False (closed), or None (UNKNOWN / oracle outage)."""
        raise NotImplementedError

    def closing_links(self, pr) -> set:
        """Issue numbers the PR is verified to close (closingIssuesReferences)."""
        raise NotImplementedError


class DeterministicFakeOracle(IssueOracle):
    def __init__(self, open_issues=(), unknown=(), links=None):
        self.open = set(open_issues)
        self.unknown = set(unknown)
        self.links = links or {}

    def is_open(self, n):
        if n in self.unknown:
            return None
        return n in self.open

    def closing_links(self, pr):
        return set(self.links.get(pr, []))


def recheck_at_land(oracle: IssueOracle, pr, issues) -> str:
    """The LAST gate before adv.advance. 'ok' iff every issue is verified-linked AND open;
    'closed'/'unknown' abort the land. A precheck PASS can NEVER substitute for this call."""
    for n in issues:
        if n not in oracle.closing_links(pr):
            return "unlinked"
        st = oracle.is_open(n)
        if st is None:
            return "unknown"
        if st is False:
            return "closed"
    return "ok"
