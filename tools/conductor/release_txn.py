"""release_txn — the atomic, exactly-once release transaction (P5, ADR-0005).

Verified-critical fixes baked in:
  * NONCE-TAG MUTEX (P5-1): each releaser pushes an annotated tag whose message embeds a random
    token, so two releasers produce DISTINCT tag OIDs and exactly one create-only push wins; an
    'Everything up-to-date' is a LOSER, not a winner.
  * SIDE-EFFECTS GATED ON WINNING (P5-2): publish / issue-close only if I won the (pkg,version)
    election, recorded as ev_pkg_tagged keyed by (pkg,version) carrying the WINNER token. Any
    runner (winner / loser / fresh recovery) converges via the journal.
  * JOURNALLED prepared/advanced/recorded (P5-3) with create-if-absent treated as success, so a
    crash mid-transaction recovers idempotently. release_done is recorded only when ALL packages
    are released, so a winner that crashes before publishing is completed by its recovery, never
    falsely finalized by a loser.
  * CONSUMED SET from the JOURNAL (P5-4), not the (squash-rewritable) commit message.
"""

from __future__ import annotations


def ev_release_started(plan_hash, token, consumed):
    return {"type": "release_started", "plan_hash": plan_hash, "token": token, "consumed": list(consumed)}


def ev_pkg_tagged(pkg, version, winner):
    return {"type": "pkg_tagged", "pkg": pkg, "version": version, "winner": winner}


def ev_pkg_released(pkg, version):
    return {"type": "pkg_released", "pkg": pkg, "version": version}


def ev_issue_closed(pkg, version, issue):
    return {"type": "issue_closed", "pkg": pkg, "version": version, "issue": issue}


def ev_release_done(plan_hash):
    return {"type": "release_done", "plan_hash": plan_hash}


def reduce_release(events) -> dict:
    consumed_by_plan, tagged, released, closed, done, started = {}, {}, set(), set(), set(), set()
    for e in events:
        t = e.get("type")
        if t == "release_started":
            started.add(e["token"])
            consumed_by_plan[e["plan_hash"]] = e.get("consumed", [])
        elif t == "pkg_tagged":
            tagged[(e["pkg"], e["version"])] = e["winner"]
        elif t == "pkg_released":
            released.add((e["pkg"], e["version"]))
        elif t == "issue_closed":
            closed.add((e["pkg"], e["version"], e["issue"]))
        elif t == "release_done":
            done.add(e["plan_hash"])
    return {"consumed_by_plan": consumed_by_plan, "tagged": tagged, "released": released,
            "closed": closed, "done": done, "started": started}


class RegistrySim:
    """A registry where publishing an existing version is a no-op SUCCESS (idempotent)."""
    def __init__(self):
        self.published = set()

    def publish_if_absent(self, pkg, version) -> str:
        key = (pkg, version)
        if key in self.published:
            return "exists"
        self.published.add(key)
        return "published"


class IssueSim:
    def __init__(self, open_issues=()):
        self.open = set(open_issues)
        self.comments = {}

    def close_if_open(self, n) -> str:
        if n in self.open:
            self.open.discard(n)
            return "closed"
        return "already-closed"

    def comment_once(self, n, marker) -> str:
        s = self.comments.setdefault(n, set())
        if marker in s:
            return "dup"
        s.add(marker)
        return "commented"


def run_release(repo, rlog, plan, token, registry, issues, trunk_to, verified_issues=()) -> str:
    """Run (or recover) the release for `plan`. Idempotent and exactly-once across concurrency +
    crash. `verified_issues` must already be confirmed closing-linked to a contributing PR."""
    ph = plan.fragment_set_hash
    st = reduce_release(rlog.read()[0])
    if ph in st["done"]:
        return "already-done"
    if token not in st["started"]:
        rlog.append(ev_release_started(ph, token, plan.consumed))

    for pkg, version in sorted(plan.versions.items()):
        key = (pkg, version)
        st = reduce_release(rlog.read()[0])
        winner = st["tagged"].get(key)
        if winner is None:                                    # contest the election
            name = f"{pkg}-{version}".replace("@", "-").replace("/", "-")
            repo.make_annotated_tag(name, trunk_to, f"release {pkg}@{version} nonce={token}")
            if repo.push_tag_create_only(name) == "won":
                rlog.append(ev_pkg_tagged(pkg, version, token))
                winner = token
            else:                                             # lost; the winner records its own tag
                winner = reduce_release(rlog.read()[0])["tagged"].get(key, "__other__")
        if winner != token:
            continue                                          # someone else owns this package's effects
        st = reduce_release(rlog.read()[0])
        if key not in st["released"]:
            registry.publish_if_absent(pkg, version)          # create-if-absent
            rlog.append(ev_pkg_released(pkg, version))
        for issue in verified_issues:
            if (pkg, version, issue) in st["closed"]:
                continue
            issues.close_if_open(issue)                        # close-if-open
            issues.comment_once(issue, f"{pkg}@{version}")     # idempotent comment
            rlog.append(ev_issue_closed(pkg, version, issue))

    # Finalize ONLY when every package is released (so a crashed winner is completed by recovery,
    # never falsely finalized by a loser).
    st = reduce_release(rlog.read()[0])
    if all(k in st["released"] for k in ((p, v) for p, v in plan.versions.items())):
        rlog.append(ev_release_done(ph))
        return "released"
    return "partial"
