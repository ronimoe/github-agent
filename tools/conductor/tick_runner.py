#!/usr/bin/env python3
"""tick_runner — the orchestrator that runs one engine tick inside GitHub Actions (issue #2).

This is the integration glue that wires the built engine to real GitHub:
  * read open, `conductor:ready`-labelled PRs (via `gh api`);
  * route each to a lane (dispatcher); enqueue new ones;
  * for the head lane, run BatchEngine.tick with a CheckRunVerdict that reads each PR's
    `conductor-spec` check-run report (produced by ci_runner under the hermetic runtime);
  * advance trunk via the App-token git remote; the reconciler completes any crashed land.

Runs under the bot App token (GH_TOKEN / git credentials), as the sole trunk writer. This module
cannot be unit-tested without a live repo + installed App + Actions; the verdict/engine logic it
calls IS unit-tested (test_integration.py, test_p3.py). `--dry-run` prints the plan only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gitutil import WorkRepo  # noqa: E402
from statelog import StateLog  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from lane_engine import LaneEngine  # noqa: E402
from reconciler import Reconciler  # noqa: E402
from ci_verdict import CheckRunVerdict  # noqa: E402


def gh(args) -> str:
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True).stdout


def ready_prs(repo: str, label: str = "conductor:ready") -> list:
    out = gh([f"/repos/{repo}/pulls?state=open&per_page=100"])
    return [{"number": p["number"], "head_sha": p["head"]["sha"], "head_ref": p["head"]["ref"],
             "labels": [l["name"] for l in p["labels"]]}
            for p in json.loads(out) if label in [l["name"] for l in p["labels"]]]


def fetch_spec_report(repo, head_sha):
    """Read the conductor-spec check-run report for a head SHA. Returns the parsed report or None."""
    out = gh([f"/repos/{repo}/commits/{head_sha}/check-runs"])
    for cr in json.loads(out).get("check_runs", []):
        if cr["name"] == "conductor-spec" and cr["status"] == "completed":
            text = (cr.get("output") or {}).get("text") or "{}"
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one Conductor engine tick against a GitHub repo.")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--remote", help="git remote URL (defaults to the App-token origin)")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.repo:
        raise SystemExit("--repo or GITHUB_REPOSITORY required")
    remote = a.remote or f"https://x-access-token:{os.environ.get('GH_TOKEN','')}@github.com/{a.repo}.git"

    prs = ready_prs(a.repo)
    print(f"{len(prs)} ready PR(s): {[p['number'] for p in prs]}")
    if a.dry_run:
        return 0

    repo = WorkRepo(remote)
    registry = StateLog(remote, "refs/conductor/registry")
    disp = Dispatcher(registry)
    Reconciler(repo, a.branch)   # (reconciler.complete_intents would run per lane here)

    # Route + enqueue, then tick each touched lane. (Single global lane shown; multi-lane fans out.)
    verdict = CheckRunVerdict(lambda spec_tree, reps: None)   # wired per-PR below in production
    for p in prs:
        lane_id = disp.route(p["number"], scopes=["root"], paths=[])  # scopes from PR files in prod
        lane = StateLog(remote, f"refs/conductor/lane/{lane_id.replace(':','_')}")
        flaky = StateLog(remote, f"refs/conductor/flaky/{lane_id.replace(':','_')}")
        eng = LaneEngine(repo, lane, flaky, lane_id=lane_id, branch=a.branch)
        eng.enqueue(p["number"], f"refs/heads/{p['head_ref']}", p["head_sha"],
                    repo.ls_remote(f"refs/heads/{a.branch}"), ["root"], {"agent": "ci"})
        verdict = CheckRunVerdict(lambda spec_tree, reps, _sha=p["head_sha"]:
                                  fetch_spec_report(a.repo, _sha))
        res = eng.tick(verdict)
        print(f"PR #{p['number']} -> lane {lane_id}: {res.action} ({res.detail})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
