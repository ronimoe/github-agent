"""issues — the issue oracle for V3 (P4) + the real GitHub GraphQL backing (issue #4).

`Closes #N` is untrusted author input: an issue may only be actioned if it is (a) genuinely
closing-linked to the PR (verified via GraphQL `closingIssuesReferences`) AND (b) OPEN — re-checked
at the merge moment. UNKNOWN (oracle outage) fails closed to human review, never a silent land."""

from __future__ import annotations

import json
import subprocess

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"

_ISSUE_STATE = ("query($owner:String!,$repo:String!,$num:Int!)"
                "{repository(owner:$owner,name:$repo){issue(number:$num){state}}}")
_CLOSING_REFS = ("query($owner:String!,$repo:String!,$pr:Int!)"
                 "{repository(owner:$owner,name:$repo){pullRequest(number:$pr)"
                 "{closingIssuesReferences(first:50){nodes{number}}}}}")


def _gh_graphql(query: str, variables: dict) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += (["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"])
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


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


class GitHubIssueOracle(IssueOracle):
    """Real oracle backed by GitHub GraphQL. `run(query, variables) -> dict` is injectable so the
    parsing is testable offline; the default shells to `gh api graphql`. Any failure (network /
    auth / rate limit) maps to UNKNOWN for open-state and to NO links for the link check — both
    fail closed (NEEDS_HUMAN / V3 FAIL), never a silent pass."""

    def __init__(self, repo: str, run=None):
        self.owner, self.name = repo.split("/", 1)
        self._run = run or _gh_graphql

    def is_open(self, n):
        try:
            data = self._run(_ISSUE_STATE, {"owner": self.owner, "repo": self.name, "num": int(n)})
        except Exception:
            return None                                  # outage -> UNKNOWN -> NEEDS_HUMAN
        issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue")
        if not issue:
            return False                                 # missing/inaccessible issue is not open
        return issue.get("state") == "OPEN"

    def closing_links(self, pr):
        try:
            data = self._run(_CLOSING_REFS, {"owner": self.owner, "repo": self.name, "pr": int(pr)})
        except Exception:
            return set()                                 # no verified links -> V3 fails (untrusted)
        pr_node = (((data or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        nodes = (pr_node.get("closingIssuesReferences") or {}).get("nodes") or []
        return {nd["number"] for nd in nodes if "number" in nd}


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
