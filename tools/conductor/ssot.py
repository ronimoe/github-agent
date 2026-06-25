#!/usr/bin/env python3
"""ssot — the rebuildable provenance projection + `conductor ssot trace/verify/rebuild` (issue #9).

GitHub is the single source of truth. This is a PROJECTION/CACHE only: every field is derived from
git commit trailers + the release journal + GitHub (via an injectable reader). If it disagrees with
GitHub, GitHub wins and we regenerate — authority is the head-SHA required-check status, NEVER ledger
content (ADR-0010). This module is imported by NO blocking-path module (a grep-guard test enforces it).

Key correctness fixes from the adversarial pass:
  * the issue↔changeset join uses the AUTHORITATIVE `change_id` map carried in the release journal —
    never a guessed `agent`+`sha8` match. An unresolved change_id is marked, never fabricated.
  * `verify` is anchored on the JOURNAL (squash-rewritable trailers are advisory), walks the current
    first-parent set (a reverted/force-pushed change does not back a closed issue), and FAILS CLOSED
    on any reader outage — never a silent PASS.
  * `rebuild` is deterministic (first-parent order + canonical JSON) and refuses to thin a ledger
    during a reader flap.
"""

from __future__ import annotations

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trailers import parse_trailers  # noqa: E402
from release_txn import reduce_release  # noqa: E402

LEDGER_REF = "refs/conductor/ledger"


# --- GitHub reader (injectable, fail-closed) ---------------------------------

class GitHubReader:
    """Mirrors issues.GitHubIssueOracle: a `run(query, variables)->dict` is injectable; the default
    shells `gh api graphql`. EVERY exception maps to unknown/empty/None (fail closed)."""

    _ISSUE = "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){state}}}"
    _CLOSERS = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n)"
                "{closedByPullRequestsReferences(first:50){nodes{number}}}}}")
    _PRCOMMITS = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n)"
                  "{commits(first:250){nodes{commit{oid}}}}}}")
    _PRFORSHA = ("query($o:String!,$r:String!,$s:GitObjectID!){repository(owner:$o,name:$r){object(oid:$s)"
                 "{... on Commit{associatedPullRequests(first:1){nodes{number}}}}}}")

    def __init__(self, repo: str, run=None):
        self.owner, self.name = repo.split("/", 1)
        if run is None:
            from issues import _gh_graphql
            run = _gh_graphql
        self._run = run

    def _q(self, query, **vars):
        return self._run(query, {"o": self.owner, "r": self.name, **vars})

    def issue_state(self, n) -> str:
        try:
            st = (((self._q(self._ISSUE, n=int(n)).get("data") or {}).get("repository") or {})
                  .get("issue") or {}).get("state")
        except Exception:
            return "unknown"
        return {"OPEN": "open", "CLOSED": "closed"}.get(st, "unknown")

    def closing_prs(self, issue) -> set:
        try:
            nodes = (((((self._q(self._CLOSERS, n=int(issue)).get("data") or {}).get("repository") or {})
                      .get("issue") or {}).get("closedByPullRequestsReferences") or {}).get("nodes") or [])
            return {nd["number"] for nd in nodes if "number" in nd}
        except Exception:
            return set()

    def pr_commits(self, pr) -> list:
        try:
            nodes = (((((self._q(self._PRCOMMITS, n=int(pr)).get("data") or {}).get("repository") or {})
                      .get("pullRequest") or {}).get("commits") or {}).get("nodes") or [])
            return [nd["commit"]["oid"] for nd in nodes if nd.get("commit", {}).get("oid")]
        except Exception:
            return []

    def pr_for_commit(self, sha) -> int | None:
        try:
            nodes = (((((self._q(self._PRFORSHA, s=sha).get("data") or {}).get("repository") or {})
                      .get("object") or {}).get("associatedPullRequests") or {}).get("nodes") or [])
            return nodes[0]["number"] if nodes else None
        except Exception:
            return None


class DeterministicFakeReader:
    def __init__(self, states=None, closing=None, commits=None, pr_by_commit=None):
        self.states = states or {}
        self.closing = closing or {}
        self.commits = commits or {}
        self.pr_by_commit = pr_by_commit or {}

    def issue_state(self, n):
        return self.states.get(int(n), "unknown")

    def closing_prs(self, issue):
        return set(self.closing.get(int(issue), set()))

    def pr_commits(self, pr):
        return list(self.commits.get(int(pr), []))

    def pr_for_commit(self, sha):
        return self.pr_by_commit.get(sha)


# --- projection ---------------------------------------------------------------

def _resolve_tip(repo, trunk_ref):
    """Resolve a trunk ref to a locally-walkable OID. A `refs/...` ref is fetched from the remote
    (the engine's WorkRepo pushes trunk but keeps no local branch); an OID passes through."""
    if trunk_ref.startswith("refs/"):
        tip = repo.fetch_ref(trunk_ref)
        if tip:
            return tip
    return trunk_ref


def build_ledger(repo, base, trunk_ref, release_log, reader):
    """Return (records, reader_outage). One record per landed first-parent commit, all fields derived."""
    rel = reduce_release(release_log.read()[0])
    cid_index = rel["change_id_index"]
    records = []
    for sha in repo.first_parent_list(base, _resolve_tip(repo, trunk_ref)):
        t = parse_trailers(repo.commit_message(sha))
        reasons, consumed, plan_hash = [], [], None
        cid = t["change_id"]
        if cid and cid in cid_index:
            consumed = [cid_index[cid]["ulid"]]
            plan_hash = cid_index[cid]["plan_hash"]
        elif cid:
            reasons.append("unresolved:no-changeset-for-change_id")   # marked, NEVER guessed
        else:
            reasons.append("unresolved:no-change-id")
        pr = reader.pr_for_commit(sha)
        reader_resolved = pr is not None
        if not reader_resolved:
            reasons.append("reader-unresolved")
        records.append({
            "schema": "conductor.ledger/v1", "commit_sha": sha, "tree_sha": repo.tree_of(sha),
            "closes": t["closes"], "change_id": cid, "agent": t["agent"], "model": t["model"],
            "batch": t["batch"], "pr": pr, "consumed_ulids": consumed, "plan_hash": plan_hash,
            "reader_resolved": reader_resolved, "source": "trailers", "reasons": reasons,
        })
    return records, any(not r["reader_resolved"] for r in records)


def serialize(records) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)


def _ledger_blob(repo, ledger_ref):
    tip = repo.fetch_ref(ledger_ref)
    return repo.read_blob(tip, "ledger.jsonl") if tip else None


def rebuild(repo, base, trunk_ref, release_log, reader, ledger_ref=LEDGER_REF):
    """Recompute the ledger and write it iff changed. Deterministic; a reader outage returns
    (False, 'reader-outage', n) rather than overwriting a thicker ledger with a thinner one."""
    records, outage = build_ledger(repo, base, trunk_ref, release_log, reader)
    content = serialize(records)
    if outage:
        return (False, "reader-outage", len(records))
    if _ledger_blob(repo, ledger_ref) == content:
        return (False, "unchanged", len(records))
    tip = repo.fetch_ref(ledger_ref)
    commit = repo.write_commit({"ledger.jsonl": content}, parent=tip)
    ok, _ = repo.lease_push(ledger_ref, commit, tip)
    return (ok, "rebuilt" if ok else "push-failed", len(records))


def trace(issue, reader, repo, base, trunk_ref, release_log) -> dict:
    rel = reduce_release(release_log.read()[0])
    cid_index = rel["change_id_index"]
    landed = set(repo.first_parent_list(base, _resolve_tip(repo, trunk_ref)))
    links, broken = [], []
    for pr in sorted(reader.closing_prs(issue)):
        commits = [c for c in reader.pr_commits(pr) if c in landed]
        if not commits:
            broken.append("no-commit-for-pr")
            continue
        for sha in commits:
            t = parse_trailers(repo.commit_message(sha))
            cid = t["change_id"]
            entry = {"pr": pr, "commit": sha, "change_id": cid, "consumed_ulids": [],
                     "plan_hash": None, "released": False}
            if cid and cid in cid_index:
                ph = cid_index[cid]["plan_hash"]
                entry["consumed_ulids"] = [cid_index[cid]["ulid"]]
                entry["plan_hash"] = ph
                entry["released"] = ph in rel["done"]
                if not entry["released"]:
                    broken.append("plan-not-released")
            else:
                broken.append("no-changeset-for-change_id")
            links.append(entry)
    return {"issue": int(issue), "issue_state": reader.issue_state(issue),
            "links": links, "broken": sorted(set(broken)), "authority": "github"}


def verify(reader, repo, base, trunk_ref, release_log) -> dict:
    """Sound, fail-closed audit. Authority is always GitHub."""
    rel = reduce_release(release_log.read()[0])
    landed = list(repo.first_parent_list(base, _resolve_tip(repo, trunk_ref)))
    trailers_by_cid = {}
    for sha in landed:
        t = parse_trailers(repo.commit_message(sha))
        if t["change_id"]:
            trailers_by_cid[t["change_id"]] = (sha, t["closes"])

    violations, advisories = [], []
    # INV-1: every consumed change of a RELEASED plan must be backed by a commit on the current trunk.
    for ph, cids in rel["change_ids_by_plan"].items():
        if ph not in rel["done"]:
            continue
        for cid in cids.values():                            # cids is {ulid: change_id} — join on change_id
            if cid not in trailers_by_cid:
                violations.append({"kind": "released-change-not-on-trunk", "change_id": cid,
                                   "plan_hash": ph, "authority": "github"})
            elif not trailers_by_cid[cid][1]:
                advisories.append({"kind": "trailer-stripped-by-squash", "change_id": cid})

    # INV-2: every issue closed by Conductor must agree with GitHub (fail closed on UNKNOWN).
    for (_pkg, _ver, issue) in rel["closed"]:
        st = reader.issue_state(issue)
        if st == "unknown":
            violations.append({"kind": "ledger-stale", "issue": issue, "authority": "github"})
        elif st != "closed":
            violations.append({"kind": "unbacked-closed-issue", "issue": issue,
                               "reason": "github-open", "authority": "github"})
    return {"ok": not violations, "violations": violations, "advisories": advisories,
            "authority": "github"}


# --- CLI ---------------------------------------------------------------------

def _repo_reader(args):
    from gitutil import WorkRepo
    from statelog import StateLog
    repo = WorkRepo(args.remote)
    rel = StateLog(args.remote, args.release_ref)
    reader = GitHubReader(args.repo) if args.repo else DeterministicFakeReader()
    return repo, rel, reader


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Conductor SSOT projection (trace/verify/rebuild).")
    ap.add_argument("--remote", required=True)
    ap.add_argument("--repo", help="owner/name for the GitHub reader (else a fail-closed fake)")
    ap.add_argument("--base", default="")
    ap.add_argument("--trunk-ref", default="refs/heads/main")
    ap.add_argument("--release-ref", default="refs/conductor/release/main")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pt = sub.add_parser("trace"); pt.add_argument("issue", type=int)
    sub.add_parser("verify")
    sub.add_parser("rebuild")
    args = ap.parse_args(argv)

    repo, rel, reader = _repo_reader(args)
    base = args.base                                     # "" => all of trunk
    if args.cmd == "trace":
        print(json.dumps(trace(args.issue, reader, repo, base, args.trunk_ref, rel), indent=2))
        return 0
    if args.cmd == "verify":
        rep = verify(reader, repo, base, args.trunk_ref, rel)
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 1
    changed, reason, n = rebuild(repo, base, args.trunk_ref, rel, reader)
    print(json.dumps({"changed": changed, "reason": reason, "records": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
